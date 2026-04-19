import subprocess
import time
from pathlib import Path
from typing import Callable

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
    _prompt_multiline,
    _prompt_text,
    _prompt_yn,
    _run_with_progress,
    _scroll_key,
    _show_item_screen,
    _view_text_in_curses,
)
from .transcript import (
    _session_activity,
    _tail_lines,
    _transcript_path_for,
)


# Unified action keymap per item status. Each entry is (key, label).
# The inspect view renders these as footer hints and gates keystrokes
# against the set so a key only fires when the status allows it.
# Terminal states (MERGED, CANCELLED, APPROVED) and mid-flight states
# (WORKING, QUEUED) intentionally have no entries — view-only.
_ACTION_KEYS_BY_STATUS: dict[ItemStatus, list[tuple[str, str]]] = {
    ItemStatus.AWAITING_PLAN_REVIEW: [
        ("a", "[a]approve→execute"),
        ("f", "[f]approve+feedback"),
        ("r", "[r]eject+feedback"),
        ("s", "[s]defer"),
    ],
    ItemStatus.AWAITING_REVIEW: [
        ("a", "[a]approve+merge"),
        ("r", "[r]eject+feedback"),
        ("s", "[s]defer"),
        ("v", "[v]diff"),
    ],
    ItemStatus.CONFLICTED: [
        ("m", "[m]retry merge"),
        ("e", "[e]resubmit to agent"),
        ("s", "[s]defer"),
    ],
    ItemStatus.ERRORED: [
        ("a", "[a]retry"),
        ("s", "[s]defer"),
    ],
    ItemStatus.REJECTED: [
        ("a", "[a]retry"),
    ],
    ItemStatus.DEFERRED: [
        ("a", "[a]restore"),
        ("x", "[x]delete"),
    ],
}


def _inspect_action_label(status: ItemStatus) -> str:
    """Footer-ready label string for the actions available at `status`.
    Empty string when there are no actions — caller renders a view-only
    footer."""
    pairs = _ACTION_KEYS_BY_STATUS.get(status, [])
    return "  ".join(label for _, label in pairs)


def _enter_action(stdscr, cfg: Config, store: Store, daemon: Daemon,
                  item: StoredItem) -> None:
    """Handle enter on the selected row. Always opens the unified inspect
    detail view — the view itself exposes the action set appropriate for
    the item's current status, so pickup/review/deferred actions are all
    reachable directly from the main table."""
    fresh = store.get(item.id)
    if fresh is None:
        _flash(stdscr, "item no longer exists.")
        return
    stdscr.nodelay(False)
    try:
        _inspect_render(stdscr, cfg, store, fresh, daemon)
    finally:
        stdscr.nodelay(True)


def _deferred_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Walk DEFERRED items as unified inspect screens. Each iteration
    exposes the full deferred action set (restore/delete) plus [n]ext and
    [q]uit. Items whose status drifted out of DEFERRED between the initial
    snapshot and render are skipped."""
    items = store.list_by_status(ItemStatus.DEFERRED)
    if not items:
        _flash(stdscr, "no deferred items.")
        return
    stdscr.nodelay(False)
    try:
        seen: set[str] = set()
        for it in items:
            fresh = store.get(it.id)
            if fresh is None or fresh.status != ItemStatus.DEFERRED:
                continue
            seen.add(it.id)
            remaining = sum(
                1 for d in store.list_by_status(ItemStatus.DEFERRED)
                if d.id not in seen
            )
            signal = _inspect_render(
                stdscr, cfg, store, fresh, daemon,
                cycle=True, remaining=remaining,
            )
            if signal == "quit":
                return
    finally:
        stdscr.nodelay(True)


def _inspect_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Prompt (in-curses) for an item id prefix, then render the unified
    detail view. Blank input = first WORKING item."""
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
        _inspect_render(stdscr, cfg, store, target, daemon)
    finally:
        stdscr.nodelay(True)


def _inspect_render(
    stdscr, cfg: Config, store: Store, item: StoredItem,
    daemon: Daemon | None = None, *,
    cycle: bool = False, remaining: int = 0,
) -> str:
    """Unified single-item detail view. Re-fetches the item on every tick
    so transcript activity and status changes render in place. Actions
    available at the footer are gated by the item's current status
    (see `_ACTION_KEYS_BY_STATUS`) so entry point does not restrict what
    the operator can do.

    Return values:
      "quit" — user pressed q, caller should stop any cycling walk.
      ""     — view closed (non-cycle) or caller may advance to the next
               item (cycle). Also returned when an action changed state;
               the caller re-queries the store for the next item.
    """
    scroll = 0
    stdscr.timeout(1000)
    try:
        while True:
            fresh = store.get(item.id) or item
            item = fresh
            h, w = stdscr.getmaxyx()
            lines = _build_detail_lines(cfg, store, item, width=w)
            queue_suffix = (
                f"  · {remaining} left" if cycle and remaining > 0 else ""
            )
            header = [
                f"  inspect · {item.title}{queue_suffix}",
                f"  id {item.id[:8]}  status {item.status.value}",
            ]
            footer = _inspect_footer(item.status, cycle=cycle)
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
            if k == "q":
                return "quit"
            if ch in (10, 13, 27) or k == "n":
                return ""
            acted, msg = _inspect_dispatch(
                stdscr, cfg, store, daemon, item, k,
            )
            if msg:
                _flash(stdscr, msg)
            if not acted:
                continue
            refreshed = store.get(item.id)
            if cycle:
                return ""
            # Non-cycle: drop back to table once the item left its prior
            # status so the operator sees the row moved. Stay put if the
            # action didn't actually change state (e.g. retry_merge still
            # conflicts) so the updated last_error is visible.
            if refreshed is None or refreshed.status != item.status:
                return ""
    finally:
        stdscr.timeout(REFRESH_MS)


def _inspect_footer(status: ItemStatus, *, cycle: bool) -> str:
    """Compose the inspect-view action hint. Cycle mode adds [n]ext to
    advance without acting; non-cycle mode uses [q/enter]close."""
    action_label = _inspect_action_label(status)
    nav = "[j/k]scroll · [space/pgdn]page · auto-refresh 1s"
    close = "[n]ext  [q]uit" if cycle else "[q/enter]close"
    parts = [action_label, close] if action_label else [close]
    return " " + "  ".join(parts) + " · " + nav + " "


def _inspect_dispatch(
    stdscr, cfg: Config, store: Store, daemon: Daemon | None,
    item: StoredItem, key: str,
) -> tuple[bool, str]:
    """Apply `key` against the item's current status. Returns
    (acted, flash_message). `acted=True` means a state-changing committer
    call was attempted; the caller reacts by advancing or refreshing.
    `acted=False` is returned when the key isn't bound at this status or
    when a prompt was cancelled."""
    if not key:
        return False, ""
    valid = {k for k, _ in _ACTION_KEYS_BY_STATUS.get(item.status, [])}
    if key not in valid:
        return False, ""
    # Lazy imports sidestep the circular committer ↔ store ↔ dashboard
    # chain; see CLAUDE.md "Gotchas from prior runs".
    from ..committer import (
        approve_and_commit,
        approve_plan,
        defer,
        delete_idea,
        reject_and_retry,
        restore_deferred,
        resubmit_conflicted,
        retry,
        retry_merge,
    )

    status = item.status

    if status == ItemStatus.AWAITING_PLAN_REVIEW:
        if key == "a":
            approve_plan(store, item)
            if daemon is not None:
                daemon.try_fill_pool()
            return True, "plan approved → execute queued"
        if key == "f":
            feedback = _prompt_multiline(
                stdscr, "feedback for execute phase"
            )
            if not feedback:
                return False, ""
            approve_plan(store, item, feedback=feedback)
            if daemon is not None:
                daemon.try_fill_pool()
            return True, "plan approved with feedback"
        if key == "r":
            feedback = _prompt_multiline(
                stdscr, "feedback (plan retry)"
            )
            if not feedback:
                return False, ""
            reject_and_retry(store, item, feedback)
            return True, "plan rejected — agent will re-plan"
        if key == "s":
            defer(store, item)
            return True, "deferred"

    if status == ItemStatus.AWAITING_REVIEW:
        if key == "a":
            msg = _build_commit_message(item)
            try:
                _run_with_progress(
                    stdscr, f"  approve + merge · {item.title}",
                    lambda p: approve_and_commit(
                        cfg, store, item, msg, progress=p),
                    hint="git worktree add + merge/rebase runs here.",
                )
            except Exception as e:  # git/state errors
                return True, f"merge failed: {e}"
            return True, "merge complete"
        if key == "r":
            feedback = _prompt_multiline(
                stdscr, "feedback (code retry)"
            )
            if not feedback:
                return False, ""
            reject_and_retry(store, item, feedback)
            return True, "code rejected — agent will re-execute"
        if key == "s":
            defer(store, item)
            return True, "deferred"
        if key == "v":
            if not item.worktree_path:
                return False, "no worktree — nothing to diff"
            wt = Path(item.worktree_path)
            def _diff_work(p: Callable[[str], None]) -> str:
                p("git diff vs base")
                return diff_vs_base(wt, cfg.git.base_branch)
            try:
                diff = _run_with_progress(
                    stdscr, f"  diff · {item.title}",
                    _diff_work,
                    hint="git diff against base branch.",
                )
            except Exception as e:
                return False, f"diff failed: {e}"
            text = diff if isinstance(diff, str) else ""
            _view_text_in_curses(stdscr, text or "(empty diff)")
            return False, ""

    if status == ItemStatus.CONFLICTED:
        if key == "m":
            try:
                ok_msg = _run_with_progress(
                    stdscr, f"  retry merge · {item.title}",
                    lambda p: retry_merge(cfg, store, item, progress=p),
                    hint="git worktree add + merge/rebase runs here.",
                )
                _, msg = ok_msg  # type: ignore[misc]
            except Exception as e:
                msg = f"retry failed: {e}"
            return True, msg
        if key == "e":
            try:
                resubmit_conflicted(cfg, store, item)
                msg = (f"resubmitted: {item.id[:8]} → queued "
                       f"(agent will resolve)")
            except Exception as e:
                msg = f"resubmit failed: {e}"
            return True, msg
        if key == "s":
            defer(store, item)
            return True, "deferred"

    if status in (ItemStatus.ERRORED, ItemStatus.REJECTED):
        if key == "a":
            try:
                retry(store, item)
            except Exception as e:
                return False, f"retry failed: {e}"
            return True, f"retry: {item.id[:8]} → queued"
        if key == "s":
            defer(store, item)
            return True, "deferred"

    if status == ItemStatus.DEFERRED:
        if key == "a":
            restore_deferred(store, item)
            if daemon is not None:
                daemon.try_fill_pool()
            return True, "restored"
        if key == "x":
            if not _prompt_yn(stdscr, "delete this idea?"):
                return False, ""
            delete_idea(store, item)
            return True, "deleted"

    return False, ""


def _is_auto_resolve_chain(store: Store, item: StoredItem) -> bool:
    """True when the item most recently entered QUEUED via the auto-resolve
    chain from `approve_and_commit` — i.e. the last CONFLICTED → QUEUED
    transition's note carries `AUTO_RESOLVE_NOTE_PREFIX`. Also matches the
    still-in-CONFLICTED case after a bounce-back. Scans the tail of the
    transition history to stay cheap on long-lived items."""
    # Lazy import — see CLAUDE.md "Lazy `..committer` imports in dashboard".
    from ..committer import AUTO_RESOLVE_NOTE_PREFIX

    history = store.transitions_for(item.id)
    for t in reversed(history[-10:]):
        if t.from_status == ItemStatus.CONFLICTED \
                and t.to_status == ItemStatus.QUEUED:
            return (t.note or "").startswith(AUTO_RESOLVE_NOTE_PREFIX)
    return False


def _build_detail_lines(
    cfg: Config, store: Store, item: StoredItem, *, width: int = 120,
) -> list[str]:
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
    if item.status in (ItemStatus.QUEUED, ItemStatus.WORKING,
                       ItemStatus.CONFLICTED) \
            and _is_auto_resolve_chain(store, item):
        out.append("flow:     auto-resolve chain (agent resolving own conflict)")
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
    if item.feedback:
        out.append("")
        out.append("── pending feedback ──")
        out.extend(item.feedback[:2000].splitlines())
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
        if item.status == ItemStatus.AWAITING_PLAN_REVIEW:
            out.append("")
            out.append("── plan ──")
            out.append("(no plan text captured)")
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
        # Tabular form needs ~80 cols (36 model + 4 × 10 numbers + pads).
        # 60–79 stacks to a 2-line compact per model; <60 goes fully
        # vertical so no field wraps mid-row.
        if width >= 80:
            out.append(f"{'MODEL':<36} {'IN':>10} {'OUT':>10} "
                       f"{'CACHE_R':>12} {'CACHE_W':>10}")
            for r in rows:
                out.append(f"{r['model']:<36} "
                           f"{_fmt_tokens(r['input']):>10} "
                           f"{_fmt_tokens(r['output']):>10} "
                           f"{_fmt_tokens(r['cache_read']):>12} "
                           f"{_fmt_tokens(r['cache_create']):>10}")
        elif width >= 60:
            for r in rows:
                out.append(f"model: {r['model']}")
                out.append(
                    f"  in={_fmt_tokens(r['input'])} "
                    f"out={_fmt_tokens(r['output'])} "
                    f"cr={_fmt_tokens(r['cache_read'])} "
                    f"cw={_fmt_tokens(r['cache_create'])}"
                )
        else:
            for r in rows:
                out.append(f"model:   {r['model']}")
                out.append(f"  in:      {_fmt_tokens(r['input'])}")
                out.append(f"  out:     {_fmt_tokens(r['output'])}")
                out.append(f"  cache_r: {_fmt_tokens(r['cache_read'])}")
                out.append(f"  cache_w: {_fmt_tokens(r['cache_create'])}")
    if item.status == ItemStatus.AWAITING_PLAN_REVIEW:
        plan_text = data.get("plan") or data.get("summary")
        if plan_text:
            out.append("")
            out.append("── plan ──")
            out.extend(str(plan_text)[:4000].splitlines())
    if item.status == ItemStatus.AWAITING_REVIEW:
        files = data.get("files_changed") or []
        if files:
            out.append("")
            out.append(f"── files changed ({len(files)}) ──")
            for f in files[:50]:
                out.append(f"  {f}")
            if len(files) > 50:
                out.append(f"  ... and {len(files) - 50} more")
        out.append(f"tokens:   {_tokens_total(item)}")
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


def _next_review_item(store: Store, seen_ids: set[str]) -> StoredItem | None:
    """Pure helper: return the next item awaiting review, skipping ids already
    visited in the current cycle. Plan reviews come first (they gate the
    pipeline), then code reviews. Returns None once the queue is empty."""
    for status in (ItemStatus.AWAITING_PLAN_REVIEW, ItemStatus.AWAITING_REVIEW):
        for it in store.list_by_status(status):
            if it.id not in seen_ids:
                return it
    return None


def _review_mode(stdscr, cfg: Config, store: Store, daemon: Daemon,
                 start_item: StoredItem | None = None) -> None:
    """Cycle through plan + code reviews as unified inspect screens. Each
    iteration exposes the full review action set (approve/reject+feedback/
    defer/diff). Rescans the queue each iteration so items that transition
    into AWAITING_* mid-session are visited. Returns to the main list only
    when the queue is empty (or the user presses q)."""
    seen_ids: set[str] = set()
    first: StoredItem | None = None
    if start_item is not None:
        fresh_start = store.get(start_item.id)
        if fresh_start is not None and fresh_start.status in (
            ItemStatus.AWAITING_PLAN_REVIEW, ItemStatus.AWAITING_REVIEW,
        ):
            first = fresh_start
    if first is None and _next_review_item(store, seen_ids) is None:
        _flash(stdscr, "no items awaiting review.")
        return
    stdscr.nodelay(False)
    try:
        while True:
            item = first or _next_review_item(store, seen_ids)
            first = None
            if item is None:
                return
            seen_ids.add(item.id)
            fresh = store.get(item.id)
            if fresh is None:
                continue
            if fresh.status not in (
                ItemStatus.AWAITING_PLAN_REVIEW, ItemStatus.AWAITING_REVIEW,
            ):
                # Item left the review queue between scan and render (e.g.
                # agent retried). Fall through to pick the next one.
                continue
            # Snapshot remaining queue size for the header hint. Counts items
            # not yet visited this cycle, excluding the one about to render.
            remaining = sum(
                1
                for st in (ItemStatus.AWAITING_PLAN_REVIEW,
                           ItemStatus.AWAITING_REVIEW)
                for candidate in store.list_by_status(st)
                if candidate.id not in seen_ids
            )
            signal = _inspect_render(
                stdscr, cfg, store, fresh, daemon,
                cycle=True, remaining=remaining,
            )
            if signal == "quit":
                return
    finally:
        stdscr.nodelay(True)


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
    def _expand_work(p: Callable[[str], None]) -> str:
        p("calling claude to expand note")
        return _expand_note_via_claude(
            note, cfg, expand_kind, timeout=180.0)
    try:
        content = _run_with_progress(
            stdscr, f"  expanding note → {dest.name}…",
            _expand_work,
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
