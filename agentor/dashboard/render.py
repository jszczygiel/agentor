import curses
import os
import queue
import threading
import time
from typing import Callable

from ..models import ItemStatus
from ..store import StoredItem

from .formatters import (
    _COL_CTX,
    _COL_ELAPSED,
    _COL_ID,
    _COL_SOURCE,
    _COL_STATE,
    _ctx_fill_pct,
    _elapsed_for,
    _fmt_elapsed,
    _phase_for,
)


ACTIONS = ("[↑/↓/j/k]nav  [enter]open  [n]ew  [r]eview  "
           "[d]eferred  [i]nspect  [tab]filter  [+/-]pool  [⇧↑/⇧↓]pri  "
           "[q]uit")

# Filter views: ordered list cycled by Tab. Each entry maps a filter name
# to the statuses to display (None = all).
FILTERS: list[tuple[str, list[ItemStatus] | None]] = [
    ("all", [ItemStatus.WORKING, ItemStatus.AWAITING_PLAN_REVIEW,
             ItemStatus.AWAITING_REVIEW, ItemStatus.CONFLICTED,
             ItemStatus.QUEUED, ItemStatus.ERRORED,
             ItemStatus.REJECTED, ItemStatus.MERGED, ItemStatus.CANCELLED,
             ItemStatus.DEFERRED]),
    ("errored", [ItemStatus.ERRORED]),
    ("conflicted", [ItemStatus.CONFLICTED]),
    ("queued", [ItemStatus.QUEUED]),
    ("working", [ItemStatus.WORKING]),
    ("awaiting_plan", [ItemStatus.AWAITING_PLAN_REVIEW]),
    ("awaiting", [ItemStatus.AWAITING_REVIEW]),
    ("deferred", [ItemStatus.DEFERRED]),
    ("merged", [ItemStatus.MERGED]),
    ("rejected", [ItemStatus.REJECTED]),
]
REFRESH_MS = 500


_TTY_FD: int | None = None


def _set_terminal_title(title: str) -> None:
    """Write an OSC-0 sequence to /dev/tty so the terminal tab/window title
    reflects agentor's live state. Bypasses curses so the escape isn't
    swallowed by the screen buffer. Best-effort — silently no-ops if the
    tty isn't writable."""
    global _TTY_FD
    try:
        if _TTY_FD is None:
            _TTY_FD = os.open("/dev/tty", os.O_WRONLY)
        os.write(_TTY_FD, f"\033]0;{title}\007".encode("utf-8", "replace"))
    except OSError:
        pass


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
        ItemStatus.AWAITING_PLAN_REVIEW: 1,
        ItemStatus.AWAITING_REVIEW: 1,
        ItemStatus.MERGED: 2,
        ItemStatus.CONFLICTED: 4,
        ItemStatus.REJECTED: 4,
        ItemStatus.CANCELLED: 4,
    }.get(status, 0)


def _render(stdscr, cfg, store, daemon, log_ring, filter_idx,
            selected_id: str | None = None) -> list[StoredItem]:
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

    # system alert banner — sticky red row when daemon has paused itself
    # because of an infrastructure failure. Truncated to fit; full message
    # is in the log ring.
    if daemon.system_alert:
        # Tail of the message is usually the most informative part of a git
        # error (the actual failure line). Show the prefix and let the user
        # scroll the log for the full text.
        msg = daemon.system_alert.replace("\n", " ").strip()
        if len(msg) > w - 30:
            msg = msg[: w - 33] + "..."
        banner = f" ⚠ PAUSED — {msg}  (press [u] to resume) "
        _safe_addstr(stdscr, row, 0, banner.ljust(w), w,
                     curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
        row += 1

    # combined status + counts line
    s = daemon.stats
    counts = {st: store.count_by_status(st) for st in ItemStatus}
    # Active items (excluding terminal states) that currently carry an
    # unresolved last_error. Surfaces stuck/faulty items in the header
    # even when the default 'all' filter would visually smear them into
    # the rest of the queue.
    status_line = (
        f" {cfg.agent.runner}  pool={cfg.agent.pool_size}  "
        f"workers={len(daemon.workers)}  "
        f"done={s.completed}  errored={counts[ItemStatus.ERRORED]}  "
        f"conflicted={counts[ItemStatus.CONFLICTED]}  │  "
        f"queued={counts[ItemStatus.QUEUED]}  "
        f"working={counts[ItemStatus.WORKING]}  "
        f"plan?={counts[ItemStatus.AWAITING_PLAN_REVIEW]}  "
        f"awaiting={counts[ItemStatus.AWAITING_REVIEW]}  "
        f"deferred={counts[ItemStatus.DEFERRED]}  "
        f"merged={counts[ItemStatus.MERGED]}  "
        f"rejected={counts[ItemStatus.REJECTED]}"
    )
    _safe_addstr(stdscr, row, 0, status_line, w)
    row += 1
    _safe_addstr(stdscr, row, 0, "─" * w, w, curses.A_DIM)
    row += 1

    # body table
    body_top = row
    body_height = h - body_top - 1  # leave 1 line for log
    _filter_name_cur, filter_statuses = FILTERS[filter_idx]
    statuses = filter_statuses if filter_statuses is not None else list(ItemStatus)
    rendered = _render_table(
        stdscr, store, body_top, body_height, w, statuses,
        cfg.agent.context_window, selected_id,
    )

    # log tail
    log_row = h - 1
    latest = list(log_ring)[-1:] if log_ring else [""]
    _safe_addstr(stdscr, log_row, 0,
                 (f" log: {latest[0]}" if latest[0] else
                  " log: (no events yet)").ljust(w), w, curses.A_DIM)
    stdscr.refresh()

    reviews = (counts[ItemStatus.AWAITING_PLAN_REVIEW]
               + counts[ItemStatus.AWAITING_REVIEW])
    _set_terminal_title(
        f"agentor[{cfg.project_name}] "
        f"W{len(daemon.workers)}/{cfg.agent.pool_size} "
        f"Q{counts[ItemStatus.QUEUED]} "
        f"R{reviews}"
    )
    return rendered


def _safe_addstr(stdscr, y, x, s, w, attr=0):
    try:
        stdscr.addnstr(y, x, s, w, attr)
    except curses.error:
        pass


def _render_table(
    stdscr, store, top, height, w, statuses, context_window,
    selected_id: str | None = None,
) -> list[StoredItem]:
    """Draw the main table and return the items in display order so the
    caller can drive arrow-key navigation. The row matching `selected_id`
    is highlighted; the viewport auto-scrolls to keep it visible."""
    if height <= 0:
        return []
    header = (f" {'ID':<{_COL_ID-1}}{'STATE':<{_COL_STATE}}"
              f"{'ELAPSED':<{_COL_ELAPSED}}{'CTX':<{_COL_CTX}}"
              f"{'SOURCE':<{_COL_SOURCE}}TITLE")
    _safe_addstr(stdscr, top, 0, header.ljust(w), w,
                 curses.A_BOLD | curses.A_UNDERLINE)

    all_items: list[tuple[ItemStatus, StoredItem]] = []
    for st in statuses:
        for it in store.list_by_status(st):
            all_items.append((st, it))

    visible = max(0, height - 1)  # minus header
    if visible <= 0 or not all_items:
        return [it for _, it in all_items]

    sel_idx = 0
    if selected_id:
        for i, (_, it) in enumerate(all_items):
            if it.id == selected_id:
                sel_idx = i
                break

    # When the list overflows, reserve a row top+bottom for scroll
    # indicators so the selection highlight never disappears under them.
    n = len(all_items)
    if n > visible:
        data_rows = max(1, visible - 2)
        reserve_top = 1
        reserve_bot = 1
    else:
        data_rows = n
        reserve_top = 0
        reserve_bot = 0

    if data_rows <= 0 or sel_idx < data_rows:
        offset = 0
    else:
        offset = min(sel_idx - data_rows + 1, n - data_rows)

    data_y0 = top + 1 + reserve_top
    for row_i in range(data_rows):
        global_i = offset + row_i
        if global_i >= n:
            break
        st, it = all_items[global_i]
        has_err = bool(it.last_error)
        elapsed = _elapsed_for(store, it.id) if st == ItemStatus.WORKING else None
        elapsed_s = _fmt_elapsed(elapsed) if elapsed is not None else "—"
        ctx_s = _ctx_fill_pct(it, context_window)
        src = it.source_file
        if len(src) > _COL_SOURCE - 1:
            src = "…" + src[-(_COL_SOURCE - 2):]
        title_max = max(0, w - 1 - _COL_ID - _COL_STATE - _COL_ELAPSED
                          - _COL_CTX - _COL_SOURCE)
        marker = "!" if has_err else " "
        state_label = st.value
        if st == ItemStatus.WORKING:
            phase = _phase_for(it)
            if not phase:
                phase = "execute" if it.session_id else "plan"
            state_label = f"{state_label}·{'plan' if phase == 'plan' else 'exec'}"
        state_cell = f"{marker}{state_label}"[: _COL_STATE]
        title = it.title[:title_max]
        line = (f" {it.id[:8]:<{_COL_ID-1}}{state_cell:<{_COL_STATE}}"
                f"{elapsed_s:<{_COL_ELAPSED}}{ctx_s:<{_COL_CTX}}"
                f"{src:<{_COL_SOURCE}}{title}")
        if has_err:
            attr = curses.color_pair(4) | curses.A_BOLD
        else:
            attr = curses.color_pair(_status_color(st))
            if st == ItemStatus.WORKING:
                attr |= curses.A_BOLD
        if global_i == sel_idx:
            # A_REVERSE trumps color so the highlight always reads as
            # "this is selected" regardless of status color.
            attr |= curses.A_REVERSE
        _safe_addstr(stdscr, data_y0 + row_i, 0, line.ljust(w), w, attr)

    if reserve_top and offset > 0:
        _safe_addstr(stdscr, top + 1, 0,
                     f" ↑ {offset} above ".ljust(w), w, curses.A_DIM)
    if reserve_bot:
        trailing = n - (offset + data_rows)
        if trailing > 0:
            _safe_addstr(stdscr, data_y0 + data_rows, 0,
                         f" ↓ {trailing} below ".ljust(w), w, curses.A_DIM)

    return [it for _, it in all_items]


def _show_item_screen(
    stdscr,
    header_lines: list[str],
    content_lines: list[str],
    action_hint: str,
    content_scroll: int = 0,
) -> None:
    """Render a single item full-screen, top-aligned:
      row 0       — title bar (reverse video)
      rows 1..N   — metadata/subheader (dim)
      separator
      rows M..    — content_lines, optionally scrolled
      last row    — action hint bar
    Content that overflows is truncated with a dim '...' marker."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    row = 0
    if header_lines:
        _safe_addstr(stdscr, row, 0, header_lines[0].ljust(w), w,
                     curses.A_BOLD | curses.A_REVERSE)
        row += 1
        for hl in header_lines[1:]:
            _safe_addstr(stdscr, row, 0, hl, w, curses.A_DIM)
            row += 1
    _safe_addstr(stdscr, row, 0, "─" * w, w, curses.A_DIM)
    row += 1
    content_top = row
    max_content_row = h - 2  # reserve last row for action hint
    # Visible slice of content_lines (top-aligned, scroll-aware).
    end = max_content_row - content_top
    visible = content_lines[content_scroll:content_scroll + end]
    for line in visible:
        if row >= max_content_row:
            break
        _safe_addstr(stdscr, row, 0, line[:w], w)
        row += 1
    truncated_above = content_scroll > 0
    truncated_below = content_scroll + end < len(content_lines)
    if truncated_above:
        _safe_addstr(stdscr, content_top, 0,
                     "↑ more above (k)".ljust(w), w, curses.A_DIM)
    if truncated_below:
        _safe_addstr(stdscr, max_content_row - 1, 0,
                     "↓ more below (j)".ljust(w), w, curses.A_DIM)
    _safe_addstr(stdscr, h - 1, 0, action_hint.ljust(w), w, curses.A_REVERSE)
    stdscr.refresh()


def _view_text_in_curses(stdscr, text: str) -> None:
    """Scroll a block of text (diff, log output) in-curses. j/k/pgup/pgdn
    scroll; q or enter exits. Avoids dropping back to the shell pager."""
    lines = text.splitlines() or ["(empty)"]
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        _show_item_screen(
            stdscr, ["  diff view"], lines,
            " [q/enter]close · [j/k]scroll · [space/pgdn]page ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(lines), max(1, h - 4))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q" or ch in (10, 13, 27):  # q, \n, \r, esc
            return


def _flash(stdscr, msg: str) -> None:
    """Briefly show `msg` across the bottom row. Used for transient
    confirmations that don't warrant a full overlay."""
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, (" " + msg).ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    stdscr.refresh()
    curses.napms(1200)


def _run_with_progress(
    stdscr, title: str, work: Callable[[Callable[[str], None]], object],
    hint: str | None = None,
) -> object:
    """Run `work(progress)` on a background thread while repainting a
    centered progress overlay with the latest progress message and an
    elapsed-seconds counter. Keeps curses responsive during slow
    operations (git merge/rebase, claude one-shot calls) that would
    otherwise leave the dashboard looking frozen.

    `hint` is an optional short line printed below the spinner to tell
    the user *why* this is slow. Keep it specific to the caller —
    misleading hints are worse than none.

    Only the main thread touches curses; the worker posts strings via a
    queue. Propagates exceptions raised by `work`."""
    msgs: queue.Queue[str] = queue.Queue()
    result: dict[str, object] = {}

    def progress(msg: str) -> None:
        msgs.put(msg)

    def target() -> None:
        try:
            result["ok"] = work(progress)
        except BaseException as e:  # noqa: BLE001 — re-raised below
            result["err"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()

    current = "starting…"
    started = time.monotonic()
    stdscr.nodelay(True)
    stdscr.timeout(150)
    spinner = "|/-\\"
    tick = 0
    try:
        while t.is_alive() or not msgs.empty():
            try:
                current = msgs.get_nowait()
            except queue.Empty:
                pass
            elapsed = time.monotonic() - started
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            _safe_addstr(stdscr, 0, 0, title.ljust(w), w,
                         curses.A_BOLD | curses.A_REVERSE)
            body = [
                "",
                f"  {spinner[tick % 4]}  {current}",
                f"     ({elapsed:.1f}s elapsed)",
            ]
            if hint:
                body.append("")
                for hint_line in hint.splitlines():
                    body.append(f"  {hint_line}")
            for i, ln in enumerate(body):
                _safe_addstr(stdscr, 2 + i, 0, ln[:w], w)
            _safe_addstr(stdscr, h - 1, 0,
                         " working… keystrokes ignored ".ljust(w), w,
                         curses.A_DIM | curses.A_REVERSE)
            stdscr.refresh()
            stdscr.getch()  # drain; timeout returns -1
            tick += 1
            if not t.is_alive():
                break
    finally:
        stdscr.timeout(REFRESH_MS)

    if "err" in result:
        raise result["err"]  # type: ignore[misc]
    return result.get("ok")


def _prompt_yn(stdscr, message: str) -> bool:
    """Overlay a yes/no confirmation on the bottom row. Returns True on 'y'.
    Leaves the window in blocking mode (nodelay=False) — callers run a
    follow-up getch loop that must block. Top-level modes reset nodelay
    themselves on exit."""
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, (" " + message + " [y/N] ").ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    stdscr.refresh()
    stdscr.nodelay(False)
    ch = stdscr.getch()
    k = chr(ch).lower() if 0 < ch < 256 else ""
    return k == "y"


def _prompt_text(stdscr, message: str) -> str:
    """Inline text input on the bottom row. Returns the typed string."""
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, (" " + message).ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    stdscr.move(h - 1, len(message) + 2)
    curses.curs_set(1)
    curses.echo()
    stdscr.nodelay(False)
    try:
        raw = stdscr.getstr(h - 1, len(message) + 2, max(1, w - len(message) - 3))
    finally:
        curses.noecho()
        curses.curs_set(0)
        # Leave nodelay=False; callers run blocking getch loops after this.
    try:
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _wrap(text: str, width: int) -> list[str]:
    """Wrap text into lines ≤ width while keeping explicit newlines. Long
    whitespace-free runs (eg. SHAs) are hard-broken."""
    import textwrap
    out: list[str] = []
    for para in (text or "").splitlines() or [""]:
        if not para.strip():
            out.append("")
            continue
        wrapped = textwrap.wrap(
            para, width=max(10, width - 2),
            replace_whitespace=False, drop_whitespace=False,
            break_long_words=True, break_on_hyphens=False,
        )
        out.extend(wrapped or [para])
    return out


def _scroll_key(ch: int, current: int, content_len: int, page: int) -> int:
    """Map a keypress to a new scroll offset. Returns -1 if the key isn't a
    scroll key, so callers can fall through to action handling."""
    if ch in (curses.KEY_DOWN, ord("j")):
        return min(current + 1, max(0, content_len - 1))
    if ch in (curses.KEY_UP, ord("k")):
        return max(0, current - 1)
    if ch in (curses.KEY_NPAGE, ord(" ")):
        return min(current + page, max(0, content_len - 1))
    if ch == curses.KEY_PPAGE:
        return max(0, current - page)
    return -1
