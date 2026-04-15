import curses
import json
import time
from collections import deque
from pathlib import Path

from .config import Config
from .daemon import Daemon
from .git_ops import diff_vs_base
from .models import ItemStatus
from .store import Store, StoredItem

ACTIONS = "[s]tatus  [l]ist  [p]ickup  [r]eview  [q]uit"
REFRESH_MS = 500


def run_dashboard(
    cfg: Config, store: Store, daemon: Daemon, log_ring: deque
) -> None:
    curses.wrapper(_loop, cfg, store, daemon, log_ring)


def _loop(stdscr, cfg: Config, store: Store, daemon: Daemon, log_ring: deque):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(REFRESH_MS)
    _init_colors()

    view = "main"  # "main" | "list"
    while True:
        _render(stdscr, cfg, store, daemon, log_ring, view)
        ch = stdscr.getch()
        if ch == -1:
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q":
            return
        if k == "s":
            view = "main"
        elif k == "l":
            view = "list"
        elif k == "r":
            _review_mode(stdscr, cfg, store)
            view = "main"
        elif k == "p":
            _pickup_mode(stdscr, cfg, store, daemon)
            view = "main"
        elif k == "\t":
            view = "list" if view == "main" else "main"


def _init_colors():
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    except curses.error:
        pass


def _status_color(status: ItemStatus) -> int:
    return {
        ItemStatus.QUEUED: 3,
        ItemStatus.WORKING: 5,
        ItemStatus.AWAITING_REVIEW: 1,
        ItemStatus.MERGED: 2,
        ItemStatus.REJECTED: 4,
        ItemStatus.CANCELLED: 4,
    }.get(status, 0)


def _render(stdscr, cfg, store, daemon, log_ring, view):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    row = 0

    # header bar
    title = f" agentor — {cfg.project_name} "
    _safe_addstr(stdscr, row, 0, title.ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    row += 1

    # actions bar
    _safe_addstr(stdscr, row, 0, f" {ACTIONS}".ljust(w), w, curses.A_REVERSE)
    row += 1

    # stats line
    s = daemon.stats
    stats_line = (
        f" runner={cfg.agent.runner}  pool={cfg.agent.pool_size}  "
        f"interval={daemon.scan_interval}s  workers={len(daemon.workers)}  "
        f"scans={s.scans}  dispatched={s.dispatched}  done={s.completed}  "
        f"failed={s.failed}"
    )
    _safe_addstr(stdscr, row, 0, stats_line, w)
    row += 1

    # current task panel — always visible
    current = _pick_current(store)
    elapsed = _elapsed_for(store, current.id) if current else None
    if current:
        line = (f" ▶ NOW  {current.id[:8]}  {_fmt_elapsed(elapsed)}  "
                f"{current.title}")
        _safe_addstr(stdscr, row, 0, line.ljust(w), w,
                     curses.color_pair(_status_color(ItemStatus.WORKING))
                     | curses.A_BOLD)
    else:
        _safe_addstr(stdscr, row, 0, " ▶ NOW  (no task running)".ljust(w), w,
                     curses.A_DIM)
    row += 1

    # counts (one line)
    counts = {st: store.count_by_status(st) for st in ItemStatus}
    counts_line = (
        f" queued={counts[ItemStatus.QUEUED]}  "
        f"working={counts[ItemStatus.WORKING]}  "
        f"awaiting={counts[ItemStatus.AWAITING_REVIEW]}  "
        f"merged={counts[ItemStatus.MERGED]}  "
        f"rejected={counts[ItemStatus.REJECTED]}"
    )
    _safe_addstr(stdscr, row, 0, counts_line, w)
    row += 1
    _safe_addstr(stdscr, row, 0, "─" * w, w, curses.A_DIM)
    row += 1

    # body table
    body_top = row
    body_height = h - body_top - 1  # leave 1 line for log
    if view == "main":
        _render_table(stdscr, store, body_top, body_height, w,
                      [ItemStatus.WORKING, ItemStatus.AWAITING_REVIEW,
                       ItemStatus.QUEUED, ItemStatus.REJECTED],
                      cfg.agent.context_window)
    elif view == "list":
        _render_table(stdscr, store, body_top, body_height, w,
                      list(ItemStatus), cfg.agent.context_window)

    # log tail
    log_row = h - 1
    latest = list(log_ring)[-1:] if log_ring else [""]
    _safe_addstr(stdscr, log_row, 0,
                 (f" log: {latest[0]}" if latest[0] else
                  " log: (no events yet)").ljust(w), w, curses.A_DIM)
    stdscr.refresh()


def _safe_addstr(stdscr, y, x, s, w, attr=0):
    try:
        stdscr.addnstr(y, x, s, w, attr)
    except curses.error:
        pass


def _pick_current(store: Store) -> StoredItem | None:
    items = store.list_by_status(ItemStatus.WORKING)
    return items[0] if items else None


def _elapsed_for(store: Store, item_id: str) -> float | None:
    """Seconds since the most recent transition INTO `working` for this item."""
    for t in reversed(store.transitions_for(item_id)):
        if t["to_status"] == ItemStatus.WORKING.value:
            return max(0.0, time.time() - float(t["at"]))
    return None


def _fmt_elapsed(sec: float | None) -> str:
    if sec is None:
        return "—:—"
    m, s = divmod(int(sec), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# Table column layout. The TITLE column gets whatever width remains.
_COL_ID = 9       # 8 chars + 1 pad
_COL_STATE = 18   # widest status name + pad
_COL_ELAPSED = 9
_COL_CTX = 7
_COL_SOURCE = 26


def _ctx_pct(item: StoredItem, context_window: int) -> str:
    """Return a short '12%' string from the agent's reported usage, or '—'
    if no usage is recorded yet."""
    if not item.result_json or context_window <= 0:
        return "—"
    try:
        data = json.loads(item.result_json)
    except json.JSONDecodeError:
        return "—"
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return "—"
    # Prefer total_tokens if the SDK provides it; otherwise sum the parts.
    total = usage.get("total_tokens")
    if total is None:
        total = sum(int(usage.get(k, 0) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "output_tokens",
        ))
    if not total:
        return "—"
    pct = 100.0 * total / context_window
    return f"{pct:.0f}%"


def _render_table(stdscr, store, top, height, w, statuses, context_window):
    if height <= 0:
        return
    header = (f" {'ID':<{_COL_ID-1}}{'STATE':<{_COL_STATE}}"
              f"{'ELAPSED':<{_COL_ELAPSED}}{'CTX':<{_COL_CTX}}"
              f"{'SOURCE':<{_COL_SOURCE}}TITLE")
    _safe_addstr(stdscr, top, 0, header.ljust(w), w,
                 curses.A_BOLD | curses.A_UNDERLINE)
    rows_used = 1
    for st in statuses:
        items = store.list_by_status(st)
        for it in items:
            if rows_used >= height:
                _safe_addstr(stdscr, top + rows_used - 1, 0,
                             f" ... (more not shown)".ljust(w), w,
                             curses.A_DIM)
                return
            elapsed = _elapsed_for(store, it.id) if st == ItemStatus.WORKING else None
            elapsed_s = _fmt_elapsed(elapsed) if elapsed is not None else "—"
            ctx_s = _ctx_pct(it, context_window)
            src = it.source_file
            if len(src) > _COL_SOURCE - 1:
                src = "…" + src[-(_COL_SOURCE - 2):]
            title_max = max(0, w - 1 - _COL_ID - _COL_STATE - _COL_ELAPSED
                              - _COL_CTX - _COL_SOURCE)
            title = it.title[:title_max]
            line = (f" {it.id[:8]:<{_COL_ID-1}}{st.value:<{_COL_STATE}}"
                    f"{elapsed_s:<{_COL_ELAPSED}}{ctx_s:<{_COL_CTX}}"
                    f"{src:<{_COL_SOURCE}}{title}")
            attr = curses.color_pair(_status_color(st))
            if st == ItemStatus.WORKING:
                attr |= curses.A_BOLD
            _safe_addstr(stdscr, top + rows_used, 0, line, w, attr)
            rows_used += 1


def _pickup_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Interactive picker over QUEUED items. For each, prompt y/n/skip/quit.
    On 'y' the daemon dispatches that item."""
    items = store.list_by_status(ItemStatus.QUEUED)
    curses.endwin()
    try:
        if not items:
            print("no queued items.")
            input("(press enter to return)")
            return
        if cfg.agent.pickup_mode != "manual":
            print(f"NOTE: pickup_mode is '{cfg.agent.pickup_mode}'. Daemon "
                  f"may auto-dispatch alongside manual approvals.")
            print()
        print(f"{len(items)} queued item(s). y=dispatch  s=skip  q=quit pickup\n")
        for it in items:
            print("=" * 72)
            print(f"id:     {it.id}")
            print(f"title:  {it.title}")
            print(f"source: {it.source_file}:{it.source_line}")
            if it.body:
                snippet = it.body.strip().splitlines()[0][:200]
                print(f"body:   {snippet}")
            print(f"attempts: {it.attempts} / {cfg.agent.max_attempts}")
            choice = input("[y]es dispatch / [s]kip / [q]uit ? ").strip().lower()
            if choice == "q":
                break
            if choice == "y":
                ok = daemon.dispatch_specific(it.id)
                if not ok:
                    print("(could not dispatch — pool full or item gone)")
                else:
                    print("dispatched.")
                # one-at-a-time when pool=1: stop so user can watch progress
                if cfg.agent.pool_size == 1:
                    print("\npool is 1 — returning to dashboard so you can "
                          "watch this item.")
                    input("(press enter to return)")
                    return
            else:
                print("skipped.")
            print()
    finally:
        stdscr.clear()
        stdscr.refresh()
        curses.doupdate()


def _review_mode(stdscr, cfg: Config, store: Store) -> None:
    """Temporarily exit curses to run interactive review on normal stdout."""
    items = store.list_by_status(ItemStatus.AWAITING_REVIEW)
    curses.endwin()
    try:
        if not items:
            print("no items awaiting review.")
            input("(press enter to return)")
            return
        print(f"{len(items)} item(s) awaiting review.\n")
        for item in items:
            _review_one_term(cfg, store, item)
    finally:
        # re-enter curses
        stdscr.clear()
        stdscr.refresh()
        curses.doupdate()


def _review_one_term(cfg: Config, store: Store, item: StoredItem) -> None:
    from .committer import approve_and_commit, reject
    import json as _json
    print("=" * 72)
    print(f"id:     {item.id}")
    print(f"title:  {item.title}")
    print(f"source: {item.source_file}:{item.source_line}")
    print(f"branch: {item.branch}")
    print(f"wt:     {item.worktree_path}")
    if item.result_json:
        res = _json.loads(item.result_json)
        print(f"summary: {res.get('summary')}")
        print(f"files:   {res.get('files_changed')}")
    print()
    diff = diff_vs_base(Path(item.worktree_path), cfg.git.base_branch)
    print(diff[:8000] if diff else "(empty diff)")
    if diff and len(diff) > 8000:
        print(f"... ({len(diff) - 8000} more bytes truncated)")
    print()
    choice = input("[a]pprove / [r]eject / [s]kip ? ").strip().lower()
    fresh = store.get(item.id)
    if choice == "a":
        msg = input("commit message (blank = default): ").strip()
        if not msg:
            msg = f"{item.title}\n\nAgent work for item {item.id}."
        sha = approve_and_commit(cfg, store, fresh, msg)
        print(f"committed {sha[:8]} on {fresh.branch}")
    elif choice == "r":
        fb = input("feedback for agent: ").strip()
        reject(store, fresh, fb or "(no feedback)")
        print("rejected.")
    else:
        print("skipped.")
    print()
