import time
from pathlib import Path

from ..config import Config
from ..daemon import Daemon
from ..git_ops import diff_vs_base
from ..models import ItemStatus
from ..store import Store, StoredItem

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


def _pickup_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Curses-native, one-item-per-screen pickup over BACKLOG + DEFERRED.
    Approving promotes BACKLOG → QUEUED; the daemon dispatches on its own."""
    from ..committer import approve_backlog, defer, delete_idea, restore_deferred
    items = (store.list_by_status(ItemStatus.BACKLOG)
             + store.list_by_status(ItemStatus.DEFERRED))
    if not items:
        _flash(stdscr, "no backlog or deferred items.")
        return
    stdscr.nodelay(False)
    try:
        for it in items:
            fresh = store.get(it.id)
            if fresh is None:
                continue
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
                    " [a]approve  [s]defer  [x]delete  [n]leave  [q]uit "
                    " · [j/k]scroll ",
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
                if k == "a":
                    f = store.get(fresh.id)
                    if f is None:
                        break
                    feedback = _prompt_text(
                        stdscr, "feedback for agent (empty = none): "
                    ) or None
                    if f.status == ItemStatus.DEFERRED:
                        restored = restore_deferred(store, f)
                        if restored == ItemStatus.BACKLOG:
                            approve_backlog(
                                store, store.get(f.id), feedback=feedback,
                            )
                    elif f.status == ItemStatus.BACKLOG:
                        approve_backlog(store, f, feedback=feedback)
                    daemon.try_fill_pool()
                    break
                if k == "s":
                    f = store.get(fresh.id)
                    if f and f.status != ItemStatus.DEFERRED:
                        defer(store, f)
                    break
                if k == "x":
                    f = store.get(fresh.id)
                    if f and _prompt_yn(stdscr, "delete this idea?"):
                        delete_idea(store, f)
                    break
                if k in ("n", ""):
                    break
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
                + ("· [m]retry merge " if item.status == ItemStatus.CONFLICTED
                   else "")
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
            header = (f"#{f['attempt']} {f['phase'] or '—'}  {when}"
                      f"  turns={f['num_turns'] or '—'}"
                      f"  dur={f['duration_ms'] and f'{f['duration_ms']/1000:.1f}s' or '—'}")
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
    from ..committer import approve_plan, defer, reject, reject_and_retry
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
            " [a]approve → execute  [r]eject+feedback  [s]defer  "
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
            approve_plan(store, fresh)
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
    from ..committer import approve_and_commit, defer, reject, reject_and_retry
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
