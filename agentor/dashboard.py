import curses
import json
import subprocess
import time
from collections import deque
from pathlib import Path

from .config import Config
from .daemon import Daemon
from .git_ops import diff_vs_base
from .models import ItemStatus
from .store import Store, StoredItem

ACTIONS = "[s]tatus  [l]ist  [p]ickup  [r]eview  [d]eferred  [tab]filter  [q]uit"

# Filter views: ordered list cycled by Tab. Each entry maps a filter name
# to the statuses to display (None = all).
FILTERS: list[tuple[str, list[ItemStatus] | None]] = [
    ("main", [ItemStatus.WORKING, ItemStatus.AWAITING_REVIEW,
              ItemStatus.QUEUED, ItemStatus.REJECTED]),
    ("queued", [ItemStatus.QUEUED]),
    ("working", [ItemStatus.WORKING]),
    ("awaiting", [ItemStatus.AWAITING_REVIEW]),
    ("deferred", [ItemStatus.DEFERRED]),
    ("merged", [ItemStatus.MERGED]),
    ("rejected", [ItemStatus.REJECTED]),
    ("all", None),
]
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

    filter_idx = 0  # index into FILTERS
    while True:
        _render(stdscr, cfg, store, daemon, log_ring, filter_idx)
        ch = stdscr.getch()
        if ch == -1:
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q":
            return
        if k == "s":
            filter_idx = 0  # back to main
        elif k == "l":
            # "all" view
            filter_idx = next(i for i, (n, _) in enumerate(FILTERS) if n == "all")
        elif k == "r":
            _review_mode(stdscr, cfg, store)
        elif k == "p":
            _pickup_mode(stdscr, cfg, store, daemon)
        elif k == "d":
            _deferred_mode(stdscr, cfg, store)
        elif k == "\t":
            filter_idx = (filter_idx + 1) % len(FILTERS)


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


def _render(stdscr, cfg, store, daemon, log_ring, filter_idx):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    row = 0

    # header bar — show project + active filter
    filter_name, _ = FILTERS[filter_idx]
    title = f" agentor — {cfg.project_name}    filter: {filter_name} ({filter_idx + 1}/{len(FILTERS)}) "
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
        f"deferred={counts[ItemStatus.DEFERRED]}  "
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
    _, filter_statuses = FILTERS[filter_idx]
    statuses = filter_statuses if filter_statuses is not None else list(ItemStatus)
    _render_table(stdscr, store, body_top, body_height, w, statuses)

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
_COL_TOK = 8
_COL_SOURCE = 26


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _tokens_used(item: StoredItem) -> str:
    """Total billed tokens for the agent run (input + cache write/read +
    output). Reported as an absolute count formatted '12.3k' / '1.2M' rather
    than as a percent of context_window: cache_read_input_tokens is cumulative
    across turns and easily exceeds context, so a percent is misleading."""
    if not item.result_json:
        return "—"
    try:
        data = json.loads(item.result_json)
    except json.JSONDecodeError:
        return "—"
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return "—"
    total = usage.get("total_tokens")
    if total is None:
        total = sum(int(usage.get(k, 0) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "output_tokens",
        ))
    if not total:
        return "—"
    return _fmt_tokens(int(total))


def _render_table(stdscr, store, top, height, w, statuses):
    if height <= 0:
        return
    header = (f" {'ID':<{_COL_ID-1}}{'STATE':<{_COL_STATE}}"
              f"{'ELAPSED':<{_COL_ELAPSED}}{'TOK':<{_COL_TOK}}"
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
            tok_s = _tokens_used(it)
            src = it.source_file
            if len(src) > _COL_SOURCE - 1:
                src = "…" + src[-(_COL_SOURCE - 2):]
            title_max = max(0, w - 1 - _COL_ID - _COL_STATE - _COL_ELAPSED
                              - _COL_TOK - _COL_SOURCE)
            title = it.title[:title_max]
            line = (f" {it.id[:8]:<{_COL_ID-1}}{st.value:<{_COL_STATE}}"
                    f"{elapsed_s:<{_COL_ELAPSED}}{tok_s:<{_COL_TOK}}"
                    f"{src:<{_COL_SOURCE}}{title}")
            attr = curses.color_pair(_status_color(st))
            if st == ItemStatus.WORKING:
                attr |= curses.A_BOLD
            _safe_addstr(stdscr, top + rows_used, 0, line, w, attr)
            rows_used += 1


def _pickup_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Interactive picker over QUEUED items. For each: y=dispatch, s=defer,
    n=leave queued (browse only), q=quit pickup."""
    from .committer import defer
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
        print(f"{len(items)} queued item(s). "
              f"y=dispatch  s=defer (set aside)  n=leave queued  q=quit\n")
        for it in items:
            print("=" * 72)
            print(f"id:     {it.id}")
            print(f"title:  {it.title}")
            print(f"source: {it.source_file}:{it.source_line}")
            if it.body:
                snippet = it.body.strip().splitlines()[0][:200]
                print(f"body:   {snippet}")
            print(f"attempts: {it.attempts} / {cfg.agent.max_attempts}")
            choice = input("[y]dispatch [s]defer [n]leave [q]uit ? ").strip().lower()
            if choice == "q":
                break
            fresh = store.get(it.id)
            if choice == "y":
                ok = daemon.dispatch_specific(fresh.id)
                if not ok:
                    print("(could not dispatch — pool full or item gone)")
                else:
                    print("dispatched.")
                if cfg.agent.pool_size == 1:
                    print("\npool is 1 — returning to dashboard so you can "
                          "watch this item.")
                    input("(press enter to return)")
                    return
            elif choice == "s":
                defer(store, fresh)
                print("deferred.")
            else:
                print("left in queue.")
            print()
    finally:
        stdscr.clear()
        stdscr.refresh()
        curses.doupdate()


def _deferred_mode(stdscr, cfg: Config, store: Store) -> None:
    """Walk DEFERRED items: r=restore, n=leave deferred, q=quit."""
    from .committer import restore_deferred
    items = store.list_by_status(ItemStatus.DEFERRED)
    curses.endwin()
    try:
        if not items:
            print("no deferred items.")
            input("(press enter to return)")
            return
        print(f"{len(items)} deferred item(s). "
              f"r=restore to prior state  n=leave deferred  q=quit\n")
        for it in items:
            print("=" * 72)
            print(f"id:     {it.id}")
            print(f"title:  {it.title}")
            print(f"source: {it.source_file}")
            choice = input("[r]estore [n]leave [q]uit ? ").strip().lower()
            if choice == "q":
                break
            fresh = store.get(it.id)
            if choice == "r":
                target = restore_deferred(store, fresh)
                print(f"restored -> {target.value}")
            else:
                print("left deferred.")
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
    from .committer import approve_and_commit, defer, reject
    import json as _json
    print("=" * 72)
    print(f"id:     {item.id}")
    print(f"title:  {item.title}")
    print(f"source: {item.source_file}:{item.source_line}")
    print(f"branch: {item.branch}")
    print(f"wt:     {item.worktree_path}")
    files: list[str] = []
    if item.result_json:
        res = _json.loads(item.result_json)
        summary = res.get("summary") or "(no summary)"
        files = res.get("files_changed") or []
        usage = res.get("usage") or {}
        print()
        print("--- agent summary ---")
        print(summary)
        print()
        print(f"files changed: {len(files)}")
        if usage:
            print(f"tokens: {usage}")
    print()

    while True:
        choice = input(
            "[a]approve [r]eject [s]defer [n]leave  "
            "[v]iew diff [f]iles [c]commits ? "
        ).strip().lower()
        fresh = store.get(item.id)
        wt = Path(item.worktree_path)

        if choice == "v":
            diff = diff_vs_base(wt, cfg.git.base_branch)
            _page(diff if diff else "(empty diff)\n")
            continue
        if choice == "f":
            print()
            for f in files:
                print(f"  {f}")
            print()
            continue
        if choice == "c":
            cp = subprocess.run(
                ["git", "log", "--oneline", "--no-decorate",
                 f"{cfg.git.base_branch}..HEAD"],
                cwd=wt, capture_output=True, text=True,
            )
            print()
            print(cp.stdout or "(no commits beyond base)")
            continue
        if choice == "a":
            msg = input("commit message (blank = default): ").strip()
            if not msg:
                msg = f"{item.title}\n\nAgent work for item {item.id}."
            sha = approve_and_commit(cfg, store, fresh, msg)
            print(f"committed {sha[:8]} on {fresh.branch}")
            return
        if choice == "r":
            fb = input("feedback for agent: ").strip()
            reject(store, fresh, fb or "(no feedback)")
            print("rejected.")
            return
        if choice == "s":
            defer(store, fresh)
            print("deferred.")
            return
        if choice in ("n", ""):
            print("left awaiting review.")
            return


def _page(text: str) -> None:
    """Show text via $PAGER (less -R if unset). Falls back to print."""
    try:
        import pydoc
        pydoc.pager(text)
    except Exception:
        print(text)
