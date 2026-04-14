import curses
import time
from collections import deque
from pathlib import Path

from .config import Config
from .daemon import Daemon
from .git_ops import diff_vs_base
from .models import ItemStatus
from .store import Store, StoredItem

ACTIONS = "[s]tatus  [l]ist  [r]eview  [log]  [q]uit"
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

    # header
    title = f" agentor — {cfg.project_name} "
    stdscr.addnstr(row, 0, title.ljust(w), w, curses.A_BOLD | curses.A_REVERSE)
    row += 1

    # actions bar
    stdscr.addnstr(row, 0, f" {ACTIONS}".ljust(w), w, curses.A_REVERSE)
    row += 1

    # stats line
    s = daemon.stats
    stats_line = (
        f" runner={cfg.agent.runner}  pool={cfg.agent.pool_size}  "
        f"interval={daemon.scan_interval}s  workers={len(daemon.workers)}  "
        f"scans={s.scans}  dispatched={s.dispatched}  done={s.completed}  "
        f"failed={s.failed}"
    )
    stdscr.addnstr(row, 0, stats_line, w)
    row += 1
    stdscr.addnstr(row, 0, "-" * w, w)
    row += 1

    # counts
    counts = {st: store.count_by_status(st) for st in ItemStatus}
    stdscr.addnstr(row, 0, " counts:", w, curses.A_BOLD)
    row += 1
    for st, n in counts.items():
        if n == 0 and st not in (ItemStatus.QUEUED, ItemStatus.WORKING,
                                  ItemStatus.AWAITING_REVIEW):
            continue
        try:
            stdscr.addnstr(row, 2, f"{st.value:<18} {n:>4}", w,
                           curses.color_pair(_status_color(st)))
        except curses.error:
            pass
        row += 1
    row += 1

    # view body
    body_top = row
    body_height = h - body_top - 2  # leave 2 lines for log + footer

    if view == "main":
        _render_main_body(stdscr, store, row, body_height, w)
    elif view == "list":
        _render_list_body(stdscr, store, row, body_height, w)

    # log tail
    log_row = h - 1
    latest = list(log_ring)[-1:] if log_ring else [""]
    stdscr.addnstr(log_row, 0, (f" log: {latest[0]}" if latest[0] else
                                 " log: (no events yet)").ljust(w), w,
                   curses.A_DIM)
    stdscr.refresh()


def _render_main_body(stdscr, store, start_row, height, w):
    # show awaiting_review + working items prominently
    rows_used = 0
    for st in (ItemStatus.WORKING, ItemStatus.AWAITING_REVIEW, ItemStatus.QUEUED):
        items = store.list_by_status(st)
        if not items:
            continue
        if rows_used >= height:
            break
        try:
            stdscr.addnstr(start_row + rows_used, 0, f" [{st.value}]".ljust(w), w,
                           curses.color_pair(_status_color(st)) | curses.A_BOLD)
        except curses.error:
            pass
        rows_used += 1
        for it in items[:max(0, height - rows_used)]:
            line = f"   {it.id[:8]}  {it.title}"
            try:
                stdscr.addnstr(start_row + rows_used, 0, line, w)
            except curses.error:
                pass
            rows_used += 1
            if rows_used >= height:
                break


def _render_list_body(stdscr, store, start_row, height, w):
    rows_used = 0
    for st in ItemStatus:
        items = store.list_by_status(st)
        if not items:
            continue
        if rows_used >= height:
            break
        try:
            stdscr.addnstr(start_row + rows_used, 0, f" [{st.value}]".ljust(w), w,
                           curses.color_pair(_status_color(st)) | curses.A_BOLD)
        except curses.error:
            pass
        rows_used += 1
        for it in items:
            if rows_used >= height:
                break
            line = f"   {it.id[:8]}  {it.title[:w - 16]}"
            try:
                stdscr.addnstr(start_row + rows_used, 0, line, w)
            except curses.error:
                pass
            rows_used += 1


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
