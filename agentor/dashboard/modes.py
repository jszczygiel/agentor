import subprocess
import time
from pathlib import Path

from ..config import Config
from ..daemon import Daemon
from ..git_ops import diff_vs_base
from ..models import ItemStatus
from ..slug import slugify
from ..store import Store, StoredItem
from ..watcher import scan_once

from .formatters import (
    _build_commit_message,
    _elapsed_for,
    _fmt_elapsed,
    _fmt_relative_age,
    _fmt_tokens,
    _progress_data,
    _result_data,
    _token_breakdown,
    _tokens_total,
)
from .render import (
    REFRESH_MS,
    _flash,
    _prompt_text,
    _prompt_yn,
    _run_with_progress,
    _scroll_key,
    _show_item_screen,
    _view_text_in_curses,
    _wrap,
)
from .transcript import (
    _session_activity,
    _tail_lines,
    _transcript_path_for,
)


def _pickup_one_screen(stdscr, cfg: Config, store: Store, daemon: Daemon,
                       fresh: StoredItem) -> str:
    """Single pickup screen for one DEFERRED item. Returns "quit" on q,
    else "" when the user advances (a/s/x/n). Caller owns nodelay state.
    Approve restores the item to its previous settled status; legacy items
    whose history leads back to BACKLOG are promoted directly to QUEUED so
    they don't get stuck in a dead bucket."""
    from ..committer import defer, delete_idea, restore_deferred
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        header = [
            f"  pickup · {fresh.status.value} · {fresh.title}",
            f"  id {fresh.id[:8]}  source {fresh.source_file}:"
            f"{fresh.source_line}  attempts "
            f"{fresh.attempts}/{cfg.agent.max_attempts}",
        ]
        body = _wrap(fresh.body or "(no description)", w - 2)
        _show_item_screen(
            stdscr, header, body,
            " [a]approve  [f]approve+feedback  [s]defer  [x]delete  "
            "[n]leave  [q]uit · [j/k]scroll ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(body), max(1, h - 6))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q":
            return "quit"
        if k in ("a", "f"):
            f = store.get(fresh.id)
            if f is None:
                return ""
            feedback: str | None = None
            if k == "f":
                feedback = _prompt_text(
                    stdscr, "feedback for agent (empty = cancel): "
                ) or None
                if feedback is None:
                    continue
            if f.status == ItemStatus.DEFERRED:
                restored = restore_deferred(store, f)
                if restored == ItemStatus.BACKLOG:
                    # Legacy rows whose history leads back to BACKLOG — the
                    # gate no longer exists, so skip it straight to QUEUED.
                    fields: dict[str, object] = {}
                    if feedback:
                        fields["feedback"] = feedback
                    store.transition(
                        f.id, ItemStatus.QUEUED,
                        note="approved by user (legacy backlog → queued)"
                        + (" with feedback" if feedback else ""),
                        **fields,
                    )
            daemon.try_fill_pool()
            return ""
        if k == "s":
            f = store.get(fresh.id)
            if f and f.status != ItemStatus.DEFERRED:
                defer(store, f)
            return ""
        if k == "x":
            f = store.get(fresh.id)
            if f and _prompt_yn(stdscr, "delete this idea?"):
                delete_idea(store, f)
            return ""
        if k in ("n", ""):
            return ""


_ENTER_ROUTES = {
    ItemStatus.DEFERRED: "pickup",
    ItemStatus.AWAITING_PLAN_REVIEW: "plan_review",
    ItemStatus.AWAITING_REVIEW: "code_review",
}


def _enter_route(status: ItemStatus) -> str:
    """Pure router: dashboard status → action key for enter.
    QUEUED is past pickup (already claimed by the scheduler), so it falls
    through to inspect along with WORKING/MERGED/ERRORED/CONFLICTED/etc."""
    return _ENTER_ROUTES.get(status, "inspect")


def _enter_action(stdscr, cfg: Config, store: Store, daemon: Daemon,
                  item: StoredItem) -> None:
    """Handle enter on the selected row. Re-fetches the item before acting
    (status may have changed since the last render) and dispatches to the
    matching single-item screen."""
    fresh = store.get(item.id)
    if fresh is None:
        _flash(stdscr, "item no longer exists.")
        return
    route = _enter_route(fresh.status)
    stdscr.nodelay(False)
    try:
        if route == "pickup":
            _pickup_one_screen(stdscr, cfg, store, daemon, fresh)
        elif route == "plan_review":
            _review_plan_curses(stdscr, cfg, store, daemon, fresh)
        elif route == "code_review":
            _review_code_curses(stdscr, cfg, store, daemon, fresh)
        else:
            _inspect_render(stdscr, cfg, store, fresh)
    finally:
        stdscr.nodelay(True)


def _deferred_mode(stdscr, cfg: Config, store: Store) -> None:
    """Walk DEFERRED items as curses-native screens. r=restore, n=leave, q=quit."""
    from ..committer import restore_deferred
    items = store.list_by_status(ItemStatus.DEFERRED)
    if not items:
        _flash(stdscr, "no deferred items.")
        return
    stdscr.nodelay(False)
    try:
        for it in items:
            fresh = store.get(it.id)
            if fresh is None:
                continue
            h, w = stdscr.getmaxyx()
            header = [
                f"  deferred · {fresh.title}",
                f"  id {fresh.id[:8]}  source {fresh.source_file}",
            ]
            body = _wrap(fresh.body or "(no description)", w - 2)
            scroll = 0
            while True:
                _show_item_screen(
                    stdscr, header, body,
                    " [r]estore  [n]leave  [q]uit · [j/k]scroll ",
                    content_scroll=scroll,
                )
                ch = stdscr.getch()
                new_scroll = _scroll_key(ch, scroll, len(body), max(1, h - 6))
                if new_scroll >= 0:
                    scroll = new_scroll
                    continue
                k = chr(ch).lower() if 0 < ch < 256 else ""
                if k == "q":
                    return
                if k == "r":
                    restore_deferred(store, fresh)
                    break
                if k in ("n", ""):
                    break
    finally:
        stdscr.nodelay(True)


def _inspect_mode(stdscr, cfg: Config, store: Store) -> None:
    """Prompt (in-curses) for an item id prefix, then render the full detail
    view in a scrollable single-item screen. Blank input = first WORKING item."""
    stdscr.nodelay(False)
    try:
        prefix = _prompt_text(stdscr, "item id prefix (blank = working): ")
        target = None
        if not prefix:
            working = store.list_by_status(ItemStatus.WORKING)
            target = working[0] if working else None
        else:
            for st in ItemStatus:
                for it in store.list_by_status(st):
                    if it.id.startswith(prefix):
                        target = it
                        break
                if target:
                    break
        if target is None:
            _flash(stdscr, f"no item matching {prefix!r}")
            return
        _inspect_render(stdscr, cfg, store, target)
    finally:
        stdscr.nodelay(True)


def _inspect_render(stdscr, cfg: Config, store: Store, item: StoredItem) -> None:
    """Render inspect as a live view. Re-fetches the item and rebuilds the
    detail block on every tick so an in-flight agent's activity feed updates
    in place. Uses `timeout(1000)` so getch returns -1 once per second and
    drives a redraw even without keypresses."""
    scroll = 0
    stdscr.timeout(1000)
    try:
        while True:
            fresh = store.get(item.id) or item
            item = fresh
            lines = _build_detail_lines(cfg, store, item)
            h, w = stdscr.getmaxyx()
            header = [
                f"  inspect · {item.title}",
                f"  id {item.id[:8]}  status {item.status.value}",
            ]
            footer = (
                " [q/enter]close · [j/k]scroll · [space/pgdn]page · "
                "auto-refresh 1s "
                + ("· [m]retry merge · [e]resubmit to agent "
                   if item.status == ItemStatus.CONFLICTED else "")
                + ("· [r]retry " if item.status == ItemStatus.ERRORED
                   else "")
            )
            _show_item_screen(
                stdscr, header, lines, footer, content_scroll=scroll,
            )
            ch = stdscr.getch()
            if ch == -1:
                continue
            new_scroll = _scroll_key(ch, scroll, len(lines), max(1, h - 4))
            if new_scroll >= 0:
                scroll = new_scroll
                continue
            k = chr(ch).lower() if 0 < ch < 256 else ""
            if k == "q" or ch in (10, 13, 27):
                return
            if k == "m" and item.status == ItemStatus.CONFLICTED:
                from ..committer import retry_merge
                try:
                    ok_msg = _run_with_progress(
                        stdscr, f"  retry merge · {item.title}",
                        lambda p: retry_merge(cfg, store, item, progress=p),
                        hint="git worktree add + merge/rebase runs here.",
                    )
                    _, msg = ok_msg  # type: ignore[misc]
                except Exception as e:  # git or state errors
                    msg = f"retry failed: {e}"
                _flash(stdscr, msg)
                # If it resolved, drop back to the list — the item is no
                # longer conflicted so there's nothing more to inspect.
                refreshed = store.get(item.id)
                if refreshed and refreshed.status != ItemStatus.CONFLICTED:
                    return
            if k == "e" and item.status == ItemStatus.CONFLICTED:
                from ..committer import resubmit_conflicted
                try:
                    resubmit_conflicted(cfg, store, item)
                    msg = (f"resubmitted: {item.id[:8]} → queued "
                           f"(agent will resolve)")
                except Exception as e:
                    msg = f"resubmit failed: {e}"
                _flash(stdscr, msg)
                refreshed = store.get(item.id)
                if refreshed and refreshed.status != ItemStatus.CONFLICTED:
                    return
            if k == "r" and item.status == ItemStatus.ERRORED:
                from ..committer import retry
                try:
                    retry(store, item)
                    msg = f"retry: {item.id[:8]} → queued"
                except Exception as e:
                    msg = f"retry failed: {e}"
                _flash(stdscr, msg)
                refreshed = store.get(item.id)
                if refreshed and refreshed.status != ItemStatus.ERRORED:
                    return
    finally:
        # Restore the main loop's refresh cadence.
        stdscr.timeout(REFRESH_MS)


def _build_detail_lines(cfg: Config, store: Store, item: StoredItem) -> list[str]:
    out: list[str] = []
    data = _result_data(item)
    progress = _progress_data(item)
    transcript_path = _transcript_path_for(cfg, item)
    out.append(f"id:       {item.id}")
    out.append(f"title:    {item.title}")
    out.append(f"state:    {item.status.value}")
    out.append(f"source:   {item.source_file}:{item.source_line}")
    out.append(f"branch:   {item.branch or '—'}")
    out.append(f"worktree: {item.worktree_path or '—'}")
    out.append(f"session:  {item.session_id or '—'}")
    out.append(f"attempts: {item.attempts} / {cfg.agent.max_attempts}")
    out.append(f"agentor:  {item.agentor_version or '—'}")
    elapsed = _elapsed_for(store, item.id)
    if elapsed is not None:
        out.append(f"elapsed:  {_fmt_elapsed(elapsed)} (since enter WORKING)")
    if progress:
        last_event_at = progress.get("last_event_at")
        age = None
        if isinstance(last_event_at, (int, float)):
            age = max(0.0, time.time() - float(last_event_at))
        activity = progress.get("activity")
        event_type = progress.get("last_event_type")
        live_state = "stalled" if item.status == ItemStatus.WORKING and age is not None and age >= 60 else "active"
        out.append(f"live:     {live_state} ({_fmt_relative_age(age)})")
        if isinstance(activity, str) and activity:
            out.append(f"doing:    {activity}")
        if isinstance(event_type, str) and event_type:
            out.append(f"event:    {event_type}")
    if transcript_path.exists():
        out.append(f"log:      {transcript_path}")
    if not data:
        out.append("")
        out.append("(no agent result yet — no token data)")
        activity = _session_activity(transcript_path)
        if activity:
            out.append("")
            out.append("── session activity ──")
            out.extend(activity)
        else:
            tail = _tail_lines(transcript_path)
            if tail:
                out.append("")
                out.append("── transcript tail ──")
                out.extend(tail)
        return out
    out.append("")
    out.append("── agent run ──")
    if data.get("live"):
        out.append("stream:   live")
    if data.get("phase"):
        out.append(f"phase:    {data['phase']}")
    if "num_turns" in data:
        out.append(f"turns:    {data['num_turns']}")
    if "duration_ms" in data:
        out.append(f"wall:     {data['duration_ms'] / 1000:.1f}s "
                   f"(api: {data.get('duration_api_ms', 0) / 1000:.1f}s)")
    if "stop_reason" in data:
        out.append(f"stop:     {data['stop_reason']}")
    rows = _token_breakdown(item)
    if rows:
        out.append("")
        out.append("── per-model tokens ──")
        out.append(f"{'MODEL':<36} {'IN':>10} {'OUT':>10} "
                   f"{'CACHE_R':>12} {'CACHE_W':>10}")
        for r in rows:
            out.append(f"{r['model']:<36} "
                       f"{_fmt_tokens(r['input']):>10} "
                       f"{_fmt_tokens(r['output']):>10} "
                       f"{_fmt_tokens(r['cache_read']):>12} "
                       f"{_fmt_tokens(r['cache_create']):>10}")
    summary = data.get("result") or data.get("summary")
    if summary:
        out.append("")
        out.append("── summary ──")
        out.extend(summary[:4000].splitlines())
    if item.status == ItemStatus.CONFLICTED and item.last_error:
        # Dedicated block for merge conflicts — keep the full summary (file
        # list + git output) visible since the short `last_error:` line
        # truncation hides exactly the part the user needs.
        out.append("")
        out.append("── merge conflict ──")
        out.extend(item.last_error[:4000].splitlines())
    elif item.last_error:
        out.append("")
        out.append(f"last_error: {item.last_error[:500]}")
    failures = store.list_failures(item.id, limit=10)
    if failures:
        out.append("")
        out.append(f"── failure history ({store.count_failures(item.id)} "
                   f"total, last {len(failures)} shown) ──")
        for f in failures:
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(float(f["at"])))
            dur_ms = f["duration_ms"]
            dur = f"{dur_ms/1000:.1f}s" if dur_ms else "—"
            header = (f"#{f['attempt']} {f['phase'] or '—'}  {when}"
                      f"  turns={f['num_turns'] or '—'}"
                      f"  dur={dur}")
            out.append(header)
            err = (f["error"] or "").strip()
            # Keep each failure compact: first line, up to 3 wrapped lines.
            for ln in err.splitlines()[:3]:
                out.append(f"  {ln[:300]}")
            if f.get("transcript_path"):
                out.append(f"  transcript: {f['transcript_path']}")
    activity = _session_activity(transcript_path)
    if activity:
        out.append("")
        out.append("── session activity ──")
        out.extend(activity)
    else:
        tail = _tail_lines(transcript_path)
        if tail:
            out.append("")
            out.append("── transcript tail ──")
            out.extend(tail)
    return out


def _review_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Walk plan + code reviews as curses-native single-item screens.
    Plan reviews run first (they gate the pipeline)."""
    plan_items = store.list_by_status(ItemStatus.AWAITING_PLAN_REVIEW)
    code_items = store.list_by_status(ItemStatus.AWAITING_REVIEW)
    items = plan_items + code_items
    if not items:
        _flash(stdscr, "no items awaiting review.")
        return
    stdscr.nodelay(False)
    try:
        for item in items:
            fresh = store.get(item.id)
            if fresh is None:
                continue
            if fresh.status == ItemStatus.AWAITING_PLAN_REVIEW:
                if _review_plan_curses(stdscr, cfg, store, daemon, fresh) == "quit":
                    return
            elif fresh.status == ItemStatus.AWAITING_REVIEW:
                if _review_code_curses(stdscr, cfg, store, daemon, fresh) == "quit":
                    return
    finally:
        stdscr.nodelay(True)


def _review_plan_curses(stdscr, cfg: Config, store: Store, daemon: Daemon,
                        item: StoredItem) -> str:
    """Plan review as a single-item curses screen. Returns "quit" if the user
    pressed q, else "" to continue walking."""
    from ..committer import approve_plan, defer
    data = _result_data(item) or {}
    plan_text = data.get("plan") or data.get("summary") or "(no plan text)"
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        header = [
            f"  plan review · {item.title}",
            f"  id {item.id[:8]}  session "
            f"{(item.session_id or '—')[:8]}  source "
            f"{item.source_file}:{item.source_line}",
        ]
        content = _wrap(plan_text, w - 2)
        _show_item_screen(
            stdscr, header, content,
            " [a]approve → execute  [f]approve+feedback  "
            "[r]eject+feedback  [s]defer  [n]leave  [q]uit · [j/k]scroll ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(content), max(1, h - 6))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        fresh = store.get(item.id)
        if fresh is None:
            return ""
        if k == "q":
            return "quit"
        if k == "a":
            approve_plan(store, fresh)
            daemon.try_fill_pool()
            return ""
        if k == "f":
            feedback = _prompt_text(
                stdscr, "feedback for execute phase (empty = cancel): "
            )
            if not feedback:
                continue
            approve_plan(store, fresh, feedback=feedback)
            daemon.try_fill_pool()
            return ""
        if k == "r":
            _handle_reject_flow(stdscr, store, fresh, "plan")
            return ""
        if k == "s":
            defer(store, fresh)
            return ""
        if k in ("n", ""):
            return ""


def _review_code_curses(stdscr, cfg: Config, store: Store, daemon: Daemon,
                        item: StoredItem) -> str:
    """Code review (post-commit) as a single-item curses screen."""
    from ..committer import approve_and_commit, defer
    data = _result_data(item) or {}
    summary = data.get("summary") or "(no summary)"
    files = data.get("files_changed") or []
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        header = [
            f"  code review · {item.title}",
            f"  id {item.id[:8]}  branch {item.branch or '—'}  "
            f"files {len(files)}  tokens {_tokens_total(item)}",
        ]
        content: list[str] = []
        content += _wrap(summary, w - 2)
        content.append("")
        content.append("── files changed ──")
        for f in files[:50]:
            content.append(f"  {f}")
        if len(files) > 50:
            content.append(f"  ... and {len(files) - 50} more")
        _show_item_screen(
            stdscr, header, content,
            " [a]approve+merge  [r]eject+feedback  [s]defer  [v]diff  "
            "[n]leave  [q]uit · [j/k]scroll ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(content), max(1, h - 6))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        fresh = store.get(item.id)
        if fresh is None:
            return ""
        if k == "q":
            return "quit"
        if k == "a":
            msg = _build_commit_message(fresh)
            try:
                _run_with_progress(
                    stdscr, f"  approve + merge · {fresh.title}",
                    lambda p: approve_and_commit(
                        cfg, store, fresh, msg, progress=p),
                    hint="git worktree add + merge/rebase runs here.",
                )
            except Exception as e:  # git/state errors shouldn't kill UI
                _flash(stdscr, f"merge failed: {e}")
            return ""
        if k == "r":
            _handle_reject_flow(stdscr, store, fresh, "code")
            return ""
        if k == "s":
            defer(store, fresh)
            return ""
        if k == "v":
            if item.worktree_path:
                diff = diff_vs_base(Path(item.worktree_path),
                                    cfg.git.base_branch)
                _view_text_in_curses(stdscr, diff or "(empty diff)")
            continue
        if k in ("n", ""):
            return ""


def _handle_reject_flow(stdscr, store: Store, item: StoredItem,
                        kind: str) -> None:
    """Collect feedback inline; enter submits it as a retry (reject_and_retry).
    Empty feedback cancels — a silent rejection is almost always a mis-press.
    `kind` is 'plan' or 'code', used only for the prompt wording."""
    from ..committer import reject_and_retry
    feedback = _prompt_text(stdscr, f"feedback ({kind} retry, empty=cancel): ")
    if not feedback:
        return
    reject_and_retry(store, item, feedback)


_NEW_ISSUE_PROMPT_FRONTMATTER = (
    "You're receiving a quick backlog-capture note from an operator "
    "using the `agentor` tool. Convert it into a single markdown file "
    "whose contents will be written directly to disk as one agentor "
    "work item parsed via frontmatter mode.\n\n"
    "Output format (produce EXACTLY this, nothing before or after, no "
    "code fences):\n\n"
    "---\n"
    "title: <5-10 word imperative title, no quotes>\n"
    "state: available\n"
    "category: <bug | idea | feature | polish | chore>\n"
    "---\n\n"
    "<2-6 sentence body. Expand the note into an actionable item. If "
    "the raw note references a filename or concept visible in this "
    "repo, ground the body in what's actually there — but do NOT "
    "invent file paths or APIs. If something is unclear, say so "
    "rather than making it up.>\n\n"
    "Raw operator note:\n```\n{note}\n```\n"
)


_NEW_ISSUE_PROMPT_CHECKBOX = (
    "You're receiving a quick backlog-capture note from an operator "
    "using the `agentor` tool. Convert it into a single checkbox "
    "item to APPEND to an existing markdown backlog file.\n\n"
    "Output format (produce EXACTLY this, nothing before or after, no "
    "code fences, no heading):\n\n"
    "- [ ] <5-10 word imperative title>\n"
    "  <2-6 sentence body. Expand the note into an actionable item. "
    "If the raw note references a filename or concept visible in this "
    "repo, ground the body in what's actually there — but do NOT "
    "invent file paths or APIs. If something is unclear, say so rather "
    "than making it up. Body lines MUST be indented with exactly two "
    "spaces so the agentor checkbox parser associates them with the "
    "item above.>\n\n"
    "Raw operator note:\n```\n{note}\n```\n"
)


def _new_issue_target(cfg: Config) -> tuple[Path, str] | None:
    """Resolve where a new-issue note should land based on the FIRST
    `sources.watch` entry plus `parsing.mode`. Returns (path, kind):
      (file, "file") — append to this watched markdown file (checkbox/
        heading mode or any non-glob watch entry).
      (dir,  "dir")  — write a new `.md` inside this dir (frontmatter
        mode with a directory-glob watch entry).
    Creates parents on first use. Returns None only when
    `sources.watch` is empty."""
    if not cfg.sources.watch:
        return None
    first = cfg.sources.watch[0]
    p = Path(first)
    is_glob = any(c in first for c in "*?[")
    mode = cfg.parsing.mode
    if mode == "frontmatter" and is_glob:
        parent = p.parent
        full = parent if parent.is_absolute() else (cfg.project_root / parent)
        full.mkdir(parents=True, exist_ok=True)
        return full, "dir"
    full = p if p.is_absolute() else (cfg.project_root / p)
    full.parent.mkdir(parents=True, exist_ok=True)
    return full, "file"


def _expand_note_via_claude(
    note: str, cfg: Config, kind: str, timeout: float,
) -> str:
    """One-shot claude call. `kind` is 'frontmatter' or 'checkbox' and
    selects the output-format prompt. Runs with `cwd=project_root` so
    the model can Read/Grep the repo for grounding. Returns the raw
    text; raises RuntimeError on any failure."""
    tmpl = (_NEW_ISSUE_PROMPT_FRONTMATTER if kind == "frontmatter"
            else _NEW_ISSUE_PROMPT_CHECKBOX)
    prompt = tmpl.format(note=note)
    cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(cfg.project_root),
        )
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {timeout:.0f}s")
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout).strip() or "claude exited nonzero"
        raise RuntimeError(err.splitlines()[-1][:200])
    out = (cp.stdout or "").strip()
    if not out:
        raise RuntimeError("claude returned empty output")
    if out.startswith("```"):
        lines = out.splitlines()
        if lines[0].lstrip("`").strip() in ("", "markdown", "md"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        out = "\n".join(lines).strip()
    if kind == "frontmatter" and not out.startswith("---"):
        raise RuntimeError("response missing frontmatter; refusing to write")
    if kind == "checkbox" and not out.lstrip().startswith("- [ ]"):
        raise RuntimeError("response missing `- [ ]`; refusing to write")
    return out


def _frontmatter_title(md: str) -> str | None:
    """Pull `title:` out of the top frontmatter block so we can slug-name
    the output file. Returns None if the block is malformed or missing."""
    lines = md.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for ln in lines[1:25]:
        if ln.strip() == "---":
            return None
        if ":" in ln:
            k, _, v = ln.partition(":")
            if k.strip().lower() == "title":
                return v.strip().strip('"').strip("'") or None
    return None


def _unique_md_path(dirpath: Path, slug: str) -> Path:
    """`<dir>/<slug>.md`, or `<slug>-2.md`, `<slug>-3.md`, … if it exists.
    Prevents silently overwriting an earlier capture."""
    path = dirpath / f"{slug}.md"
    i = 2
    while path.exists():
        path = dirpath / f"{slug}-{i}.md"
        i += 1
    return path


def _append_checkbox_block(file_path: Path, block: str) -> None:
    """Append `block` to `file_path`, guaranteeing exactly one blank
    line between prior content and the new item. Creates the file if
    missing."""
    existing = file_path.read_text() if file_path.exists() else ""
    prefix = ""
    if existing:
        existing = existing.rstrip() + "\n"
        prefix = "\n"
    file_path.write_text(existing + prefix + block.rstrip() + "\n")


def _new_issue_mode(
    stdscr, cfg: Config, store: Store, daemon: Daemon,
) -> None:
    """Capture a quick bug/idea note, expand it via a one-shot claude
    call, write the result to the first watched source per parsing mode,
    then scan_once so the item shows up in the table immediately.

    Routing:
      - `checkbox`/`heading` mode OR a single-file watch entry → append
        the expanded item to the watched file.
      - `frontmatter` mode with a directory-glob watch entry → write a
        new `<slug>.md` inside the glob's dir."""
    target = _new_issue_target(cfg)
    if target is None:
        _flash(stdscr, "no sources.watch configured")
        return
    dest, kind = target
    mode = cfg.parsing.mode
    expand_kind = "frontmatter" if (mode == "frontmatter" and kind == "dir") \
        else "checkbox"
    note = _prompt_text(
        stdscr, "bug/idea note (enter=submit, empty=cancel): ",
    )
    if not note:
        return
    try:
        content = _run_with_progress(
            stdscr, f"  expanding note → {dest.name}…",
            lambda p: (p("calling claude to expand note"),
                       _expand_note_via_claude(
                           note, cfg, expand_kind, timeout=180.0))[-1],
            hint="one-shot claude call; may take 10-60s.",
        )
    except Exception as e:
        _flash(stdscr, f"expand failed: {e}")
        return
    if not isinstance(content, str):
        _flash(stdscr, "expand returned no text")
        return
    if kind == "dir":
        title = _frontmatter_title(content) or note
        path = _unique_md_path(dest, slugify(title))
        path.write_text(content + ("" if content.endswith("\n") else "\n"))
        saved_msg = path.name
    else:
        _append_checkbox_block(dest, content)
        saved_msg = f"appended to {dest.name}"
    result = scan_once(cfg, store)
    if result.new_items:
        daemon.try_fill_pool()
    _flash(stdscr, f"saved: {saved_msg} ({result.new_items} new)")
