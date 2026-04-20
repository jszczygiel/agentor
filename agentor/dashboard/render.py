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
    _COL_STATE,
    _ctx_fill_pct,
    _elapsed_for,
    _fmt_elapsed,
    _fmt_token_compact,
    _fmt_token_row,
    _phase_for,
    _token_windows,
)


ACTIONS_WIDE = ("[↑/↓/j/k]nav  [enter]open  [n]ew  [r]eview  "
                "[d]eferred  [i]nspect  [tab]filter  [+/-]pool  [⇧↑/⇧↓]pri  "
                "[q]uit")
ACTIONS_MID = "[↑↓][⏎][n][r][d][i][tab][+/-][?]help [q]uit"
ACTIONS_NARROW = "↑↓ ⏎ tab q  [?]help"

# Back-compat alias — existing tests import ACTIONS and assert tokens in it.
ACTIONS = ACTIONS_WIDE


def _layout_tier(w: int) -> str:
    """Width tier for responsive rendering. One source of truth so every
    renderer (hint bar, status line, table columns, inspect view) agrees."""
    if w >= 80:
        return "wide"
    if w >= 60:
        return "mid"
    return "narrow"


# One-character glyph per status for the narrow-tier STATE column.
_STATE_GLYPHS: dict[ItemStatus, str] = {
    ItemStatus.QUEUED: "Q",
    ItemStatus.WORKING: "W",
    ItemStatus.AWAITING_PLAN_REVIEW: "P",
    ItemStatus.AWAITING_REVIEW: "R",
    ItemStatus.MERGED: "M",
    ItemStatus.CONFLICTED: "C",
    ItemStatus.ERRORED: "E",
    ItemStatus.REJECTED: "X",
    ItemStatus.CANCELLED: "K",
    ItemStatus.DEFERRED: "D",
    ItemStatus.APPROVED: "A",
}


def _state_glyph(status: ItemStatus) -> str:
    return _STATE_GLYPHS.get(status, "?")

# Filter views: ordered list cycled by Tab. Each entry maps a filter name
# to the statuses to display (None = every ItemStatus member).
# "active" is the default (index 0): work the operator can still act on —
# terminal states (merged/rejected/errored/cancelled) and deferred are
# hidden and only surface via the explicit per-status filters or "all".
FILTERS: list[tuple[str, list[ItemStatus] | None]] = [
    ("active", [ItemStatus.WORKING, ItemStatus.AWAITING_PLAN_REVIEW,
                ItemStatus.AWAITING_REVIEW, ItemStatus.CONFLICTED,
                ItemStatus.QUEUED, ItemStatus.APPROVED]),
    ("needs attention", [ItemStatus.ERRORED, ItemStatus.CONFLICTED]),
    ("queued", [ItemStatus.QUEUED]),
    ("working", [ItemStatus.WORKING]),
    ("awaiting_plan", [ItemStatus.AWAITING_PLAN_REVIEW]),
    ("awaiting", [ItemStatus.AWAITING_REVIEW]),
    ("deferred", [ItemStatus.DEFERRED]),
    ("merged", [ItemStatus.MERGED]),
    ("rejected", [ItemStatus.REJECTED]),
    ("all", None),
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
    tier = _layout_tier(w)
    row = 0

    # header bar — show project + active filter
    filter_name, _ = FILTERS[filter_idx]
    if tier == "narrow":
        title = f" agentor · {filter_name} ({filter_idx + 1}/{len(FILTERS)}) "
    else:
        title = (f" agentor — {cfg.project_name}    filter: {filter_name} "
                 f"({filter_idx + 1}/{len(FILTERS)}) ")
    _safe_addstr(stdscr, row, 0, title.ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    row += 1

    # actions bar
    hint = {"wide": ACTIONS_WIDE, "mid": ACTIONS_MID,
            "narrow": ACTIONS_NARROW}[tier]
    _safe_addstr(stdscr, row, 0, f" {hint}".ljust(w), w, curses.A_REVERSE)
    row += 1

    # system alert banner — sticky red row when daemon has paused itself
    # because of an infrastructure failure. Truncated to fit; full message
    # is in the log ring.
    if daemon.system_alert:
        banner = _build_alert_banner(daemon.system_alert, w)
        _safe_addstr(stdscr, row, 0, banner.ljust(w), w,
                     curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
        row += 1

    # Stale-session banners — informational (no pause). Capped at 3 rows
    # so a runaway pool doesn't eat the table on narrow terminals; beyond
    # the cap, a compact roll-up summarises the rest.
    stale = getattr(daemon, "stale_session_alerts", {}) or {}
    if stale:
        now_ns = time.time_ns()
        entries = sorted(stale.items(), key=lambda kv: kv[1])  # oldest first
        cap = 3
        shown = entries[:cap]
        for item_id, mtime_ns in shown:
            mins = max(0, (now_ns - mtime_ns) // 60_000_000_000)
            line = _build_stale_banner(item_id, mins, w)
            _safe_addstr(stdscr, row, 0, line.ljust(w), w,
                         curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE)
            row += 1
        extra = len(entries) - len(shown)
        if extra > 0:
            line = f" ⚠ +{extra} more stale session(s) — press [u] to ack "
            _safe_addstr(stdscr, row, 0, line[:w].ljust(w), w,
                         curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE)
            row += 1

    # combined status + counts line
    s = daemon.stats
    counts = {st: store.count_by_status(st) for st in ItemStatus}
    # Active items (excluding terminal states) that currently carry an
    # unresolved last_error. Surfaces stuck/faulty items in the header
    # even when the default 'all' filter would visually smear them into
    # the rest of the queue.
    # Compute once and reuse — the token-windows cache makes the second
    # call free but explicit sharing keeps the data-flow obvious.
    token_windows = _token_windows(store, daemon.started_at)
    token_compact = _fmt_token_compact(token_windows, cfg.agent)
    status_line = _build_status_line(
        tier, cfg, s, counts, len(daemon.workers),
        token_compact=token_compact,
    )
    _safe_addstr(stdscr, row, 0, status_line, w)
    row += 1
    token_row = " " + _fmt_token_row(token_windows, cfg.agent, tier)
    _safe_addstr(stdscr, row, 0, token_row.ljust(w), w, curses.A_DIM)
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


def _table_header(tier: str) -> str:
    """Header row for the main table. Narrow drops STATE label to a glyph
    column so TITLE gets the slack."""
    if tier == "wide":
        return (f" {'ID':<{_COL_ID-1}}{'STATE':<{_COL_STATE}}"
                f"{'ELAPSED':<{_COL_ELAPSED}}{'CTX':<{_COL_CTX}}TITLE")
    if tier == "mid":
        return (f" {'ID':<{_COL_ID-1}}{'STATE':<{_COL_STATE}}"
                f"{'ELAPSED':<{_COL_ELAPSED}}{'CTX':<{_COL_CTX}}TITLE")
    # narrow: 2-char glyph column (marker + letter)
    return (f" {'ID':<{_COL_ID-1}}{'S':<3}"
            f"{'ELAPSED':<{_COL_ELAPSED}}TITLE")


def _table_row(tier: str, item, st, elapsed_s: str, ctx_s: str,
               has_err: bool, w: int) -> str:
    """Compose one main-table row respecting the active width tier.
    The priority glyph (`*` for priority>0, space otherwise) is always
    reserved before the TITLE so pinned rows stay column-aligned with
    ordinary ones."""
    marker = "!" if has_err else " "
    pri_glyph = "*" if item.priority > 0 else " "
    pri_cell = f"{pri_glyph} "  # glyph + separator

    if tier == "narrow":
        glyph = _state_glyph(st)
        state_cell = f"{marker}{glyph} "  # 3 chars total — matches header
        cols_used = 1 + (_COL_ID - 1) + 3 + _COL_ELAPSED + len(pri_cell)
        title_max = max(0, w - cols_used)
        title = item.title[:title_max]
        return (f" {item.id[:8]:<{_COL_ID-1}}{state_cell}"
                f"{elapsed_s:<{_COL_ELAPSED}}{pri_cell}{title}")

    state_label = st.value
    if st == ItemStatus.WORKING:
        phase = _phase_for(item)
        if not phase:
            phase = "execute" if item.session_id else "plan"
        state_label = f"{state_label}·{'plan' if phase == 'plan' else 'exec'}"
    state_cell = f"{marker}{state_label}"[: _COL_STATE]

    if tier == "mid":
        cols_used = (1 + (_COL_ID - 1) + _COL_STATE
                     + _COL_ELAPSED + _COL_CTX + len(pri_cell))
        title_max = max(0, w - cols_used)
        title = item.title[:title_max]
        return (f" {item.id[:8]:<{_COL_ID-1}}{state_cell:<{_COL_STATE}}"
                f"{elapsed_s:<{_COL_ELAPSED}}{ctx_s:<{_COL_CTX}}"
                f"{pri_cell}{title}")

    # wide
    cols_used = (1 + (_COL_ID - 1) + _COL_STATE + _COL_ELAPSED
                 + _COL_CTX + len(pri_cell))
    title_max = max(0, w - cols_used)
    title = item.title[:title_max]
    return (f" {item.id[:8]:<{_COL_ID-1}}{state_cell:<{_COL_STATE}}"
            f"{elapsed_s:<{_COL_ELAPSED}}{ctx_s:<{_COL_CTX}}"
            f"{pri_cell}{title}")


def _build_alert_banner(alert: str, w: int) -> str:
    """Compose the system-alert banner so it never overflows `w`. Picks
    one of three wrappers depending on how much room is left after the
    chrome (PAUSED marker + unpause prompt)."""
    msg = (alert or "").replace("\n", " ").strip()
    if w < 33:
        return " ⚠ PAUSED [u] "[:w]
    if w < 50:
        prefix = " ⚠ PAUSED — "
        suffix = " [u]"
        budget = w - len(prefix) - len(suffix)
        if msg and len(msg) > budget:
            msg = msg[: max(0, budget - 3)] + "..."
        return f"{prefix}{msg}{suffix}"
    prefix = " ⚠ PAUSED — "
    suffix = "  (press [u] to resume) "
    budget = w - len(prefix) - len(suffix)
    if msg and len(msg) > budget:
        msg = msg[: max(0, budget - 3)] + "..."
    return f"{prefix}{msg}{suffix}"


def _build_stale_banner(item_id: str, minutes: int, w: int) -> str:
    """Compose the stale-session sticky banner. Kept short so three of
    these can share the header on a narrow terminal without pushing the
    table off-screen."""
    short = item_id[:8]
    if w < 33:
        return f" ⚠ stale {short} "[:w]
    prefix = f" ⚠ stale session {short} — {minutes}m idle"
    suffix = "  [u] to ack "
    budget = w - len(prefix) - len(suffix)
    if budget < 0:
        return (prefix + suffix)[:w]
    return f"{prefix}{' ' * budget}{suffix}"


def _build_status_line(tier: str, cfg, stats, counts: dict,
                       worker_count: int,
                       token_compact: str = "") -> str:
    """Tier-aware status/counts line. Mid/narrow abbreviate heavily so the
    most important counters (pool/workers/review-queue/errors) survive a
    phone-width terminal.

    `token_compact` is the compact session/weekly token indicator (see
    `_fmt_token_compact`). Appended to the wide tier only — mid and narrow
    have no spare budget, and the token panel still shows their tier-
    specific totals."""
    if tier == "wide":
        tail = f"  │  {token_compact}" if token_compact else ""
        return (
            f" {cfg.agent.runner}  pool={cfg.agent.pool_size}  "
            f"workers={worker_count}  "
            f"done={stats.completed}  "
            f"needs_attention="
            f"{counts[ItemStatus.ERRORED] + counts[ItemStatus.CONFLICTED]}  │  "
            f"queued={counts[ItemStatus.QUEUED]}  "
            f"working={counts[ItemStatus.WORKING]}  "
            f"plan?={counts[ItemStatus.AWAITING_PLAN_REVIEW]}  "
            f"awaiting={counts[ItemStatus.AWAITING_REVIEW]}  "
            f"deferred={counts[ItemStatus.DEFERRED]}  "
            f"merged={counts[ItemStatus.MERGED]}  "
            f"rejected={counts[ItemStatus.REJECTED]}"
            f"{tail}"
        )
    review = (counts[ItemStatus.AWAITING_PLAN_REVIEW]
              + counts[ItemStatus.AWAITING_REVIEW])
    if tier == "mid":
        return (
            f" {cfg.agent.runner} p={cfg.agent.pool_size} "
            f"w={worker_count} d={stats.completed} "
            f"!={counts[ItemStatus.ERRORED] + counts[ItemStatus.CONFLICTED]} │ "
            f"Q={counts[ItemStatus.QUEUED]} "
            f"W={counts[ItemStatus.WORKING]} R={review}"
        )
    # narrow
    return (
        f" p={cfg.agent.pool_size} w={worker_count} "
        f"R={review} "
        f"!={counts[ItemStatus.ERRORED] + counts[ItemStatus.CONFLICTED]}"
    )


def _safe_addstr(stdscr, y, x, s, w, attr=0):
    try:
        stdscr.addnstr(y, x, s, w, attr)
    except curses.error:
        pass


def _handle_resize(stdscr, ch: int) -> bool:
    """Return True when `ch` is `KEY_RESIZE` and the screen was refreshed.
    After a terminal shrink the internal curses buffer still holds the
    wider layout; without an explicit `clear()` + `update_lines_cols()`
    the stale cells wrap in the narrower terminal and push row 0 off
    screen. Callers typically `continue` their loop so the next tick
    repaints at the new size."""
    if ch != curses.KEY_RESIZE:
        return False
    if hasattr(curses, "update_lines_cols"):
        curses.update_lines_cols()
    stdscr.clear()
    return True


def _render_table(
    stdscr, store, top, height, w, statuses, context_window,
    selected_id: str | None = None,
) -> list[StoredItem]:
    """Draw the main table and return the items in display order so the
    caller can drive arrow-key navigation. The row matching `selected_id`
    is highlighted; the viewport auto-scrolls to keep it visible."""
    if height <= 0:
        return []
    tier = _layout_tier(w)
    header = _table_header(tier)
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
        line = _table_row(tier, it, st, elapsed_s, ctx_s, has_err, w)
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


def _show_help(stdscr) -> None:
    """Full-legend help overlay. Narrow-tier hint bars collapse to
    `↑↓ ⏎ q  [?]help`, so the user needs a dedicated surface to see the
    full keymap when the main-view hints are abbreviated."""
    lines = [
        "navigation",
        "  ↑ / k              move selection up",
        "  ↓ / j              move selection down",
        "  PgUp / PgDn        jump 10 rows",
        "  Home / End         top / bottom",
        "  Enter              open selected item (inspect/actions)",
        "  Tab                cycle filter view",
        "",
        "global actions",
        "  n                  new issue",
        "  r                  review queue walk",
        "  d                  deferred queue walk",
        "  i                  inspect by id prefix",
        "  +  /  -            increase / decrease agent pool size",
        "  Shift+↑  /  P      bump selected item priority up",
        "  Shift+↓  /  O      bump priority down",
        "  u                  acknowledge system alert (unpause)",
        "  q                  quit",
        "",
        "inspect actions (inside the detail view)",
        "  x                  delete current item (any status, confirms first)",
        "",
        "state glyphs (narrow tier)",
        "  Q queued   W working   P awaiting plan   R awaiting review",
        "  M merged   C conflicted  E errored  X rejected  D deferred",
        "  K cancelled  B backlog  A approved",
        "",
        "close help: q / enter / esc",
    ]
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        _show_item_screen(
            stdscr, ["  help · agentor dashboard"], lines,
            " [q/enter]close · [j/k]scroll · [space/pgdn]page ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        if _handle_resize(stdscr, ch):
            continue
        new_scroll = _scroll_key(ch, scroll, len(lines), max(1, h - 4))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q" or ch in (10, 13, 27):
            return


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
        if _handle_resize(stdscr, ch):
            continue
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
            ch = stdscr.getch()  # drain; timeout returns -1
            _handle_resize(stdscr, ch)
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
    stdscr.nodelay(False)
    while True:
        h, w = stdscr.getmaxyx()
        _safe_addstr(stdscr, h - 1, 0,
                     (" " + message + " [y/N] ").ljust(w), w,
                     curses.A_BOLD | curses.A_REVERSE)
        stdscr.refresh()
        ch = stdscr.getch()
        if _handle_resize(stdscr, ch):
            continue
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


def _prompt_multiline(stdscr, label: str, *, rows: int | None = None) -> str:
    """Multi-line text entry in a centered overlay. Enter inserts a newline;
    Ctrl-G or Ctrl-X submits; Ctrl-C or Esc cancels (empty string). Empty
    submit also returns empty. Falls back to `_prompt_text` on very small
    terminals.

    `rows` is the visible edit area height. When None, it adapts to the
    terminal — larger terminals get more room for longer feedback, capped
    so the overlay never eats the whole screen."""
    import curses.textpad

    h, w = stdscr.getmaxyx()
    if h < 10 or w < 40:
        return _prompt_text(stdscr, label + " (empty=cancel): ")

    if rows is None:
        # Adaptive: fill most of the terminal, floor=8 (matches old default
        # so sub-14-row terminals keep the prior behavior), cap=30 so very
        # tall terminals don't render a single gigantic box.
        rows = max(8, min(30, h - 6))

    box_h = min(rows + 4, h - 2)
    box_w = min(80, w - 4)
    inner_rows = box_h - 4
    inner_cols = box_w - 2
    top = (h - box_h) // 2
    left = (w - box_w) // 2

    frame = curses.newwin(box_h, box_w, top, left)
    frame.bkgd(" ", curses.A_NORMAL)
    frame.box()
    header = f" {label} "[: box_w - 2]
    try:
        frame.addnstr(0, max(1, (box_w - len(header)) // 2),
                      header, box_w - 2, curses.A_BOLD | curses.A_REVERSE)
    except curses.error:
        pass
    footer = " Ctrl-G/X submit · Ctrl-C cancel · empty=cancel "[: box_w - 2]
    try:
        frame.addnstr(box_h - 1, max(1, (box_w - len(footer)) // 2),
                      footer, box_w - 2, curses.A_DIM)
    except curses.error:
        pass
    frame.refresh()

    edit_win = curses.newwin(inner_rows, inner_cols, top + 2, left + 1)
    edit_win.keypad(True)
    box = curses.textpad.Textbox(edit_win)
    # stripspaces=True (default) drops trailing whitespace per-line; that
    # wipes intentional blank paragraph separators in multi-line feedback.
    box.stripspaces = False

    cancelled = {"flag": False}

    def validator(ch: int) -> int:
        if ch in (3, 27):  # Ctrl-C, Esc
            cancelled["flag"] = True
            return 7  # Ctrl-G — terminate edit()
        if ch == 24:  # Ctrl-X — nano-style submit
            return 7
        if ch in (127, curses.KEY_BACKSPACE, 8):
            return curses.KEY_BACKSPACE
        return ch

    curses.curs_set(1)
    stdscr.nodelay(False)
    try:
        box.edit(validator)
        text = box.gather()
    except KeyboardInterrupt:
        cancelled["flag"] = True
        text = ""
    finally:
        curses.curs_set(0)
        del edit_win
        del frame
        stdscr.touchwin()
        stdscr.refresh()

    if cancelled["flag"]:
        return ""
    return text.rstrip()


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
