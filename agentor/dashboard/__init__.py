import curses
from collections import deque

from ..config import Config
from ..daemon import Daemon
from ..store import Store, StoredItem

from .render import (
    FILTERS,
    REFRESH_MS,
    _handle_resize,
    _init_colors,
    _render,
    _set_terminal_title,
    _show_help,
)
from .modes import (
    _deferred_mode,
    _enter_action,
    _inspect_mode,
    _new_issue_mode,
    _review_mode,
)


__all__ = ["run_dashboard"]


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
    selected_id: str | None = None  # main-table cursor, tracked by item id
    # Previous-tick cursor anchor. When the selected item's status changes
    # (merge, auto-queue, etc.) it can jump far in the list; we snap the
    # cursor to the row it was visually on rather than chase the item.
    prev_selected_id: str | None = None
    prev_selected_idx = 0
    prev_selected_status: str | None = None
    # Last list of items rendered (in display order). Refreshed each tick
    # so arrow-key navigation operates on what the user actually sees.
    rendered: list[StoredItem] = []
    try:
        while True:
            try:
                rendered = _render(
                    stdscr, cfg, store, daemon, log_ring, filter_idx,
                    selected_id,
                )

                cur = next(
                    (i for i in rendered if i.id == selected_id), None
                )
                if not rendered:
                    selected_id = None
                elif cur is None:
                    new_idx = min(prev_selected_idx, len(rendered) - 1)
                    selected_id = rendered[max(0, new_idx)].id
                elif (selected_id == prev_selected_id
                      and prev_selected_status is not None
                      and cur.status.value != prev_selected_status):
                    # Item status changed while cursor was on it — row
                    # has probably jumped far. Keep the cursor at the
                    # visual position the user was looking at.
                    new_idx = min(prev_selected_idx, len(rendered) - 1)
                    selected_id = rendered[max(0, new_idx)].id

                cur = next(
                    (i for i in rendered if i.id == selected_id), None
                )
                if cur is not None:
                    prev_selected_idx = rendered.index(cur)
                    prev_selected_id = selected_id
                    prev_selected_status = cur.status.value
                else:
                    prev_selected_id = None
                    prev_selected_status = None
                ch = stdscr.getch()
            except KeyboardInterrupt:
                # ctrl-c in the dashboard should exit cleanly, not crash
                # with a stack trace. The daemon is a daemon thread;
                # leaving the loop lets curses.wrapper restore the
                # terminal and the process exits.
                return
            if ch == -1:
                continue
            if _handle_resize(stdscr, ch):
                continue
            k = chr(ch).lower() if 0 < ch < 256 else ""

            # Cursor navigation on the main table.
            if ch in (curses.KEY_DOWN, ord("j")) and rendered:
                idx = _idx_of(rendered, selected_id)
                selected_id = rendered[min(len(rendered) - 1, idx + 1)].id
                continue
            if ch in (curses.KEY_UP, ord("k")) and rendered:
                idx = _idx_of(rendered, selected_id)
                selected_id = rendered[max(0, idx - 1)].id
                continue
            if ch == curses.KEY_NPAGE and rendered:
                idx = _idx_of(rendered, selected_id)
                selected_id = rendered[min(len(rendered) - 1, idx + 10)].id
                continue
            if ch == curses.KEY_PPAGE and rendered:
                idx = _idx_of(rendered, selected_id)
                selected_id = rendered[max(0, idx - 10)].id
                continue
            if ch == curses.KEY_HOME and rendered:
                selected_id = rendered[0].id
                continue
            if ch == curses.KEY_END and rendered:
                selected_id = rendered[-1].id
                continue
            if ch in (10, 13, curses.KEY_ENTER) and rendered and selected_id:
                sel = store.get(selected_id)
                if sel is not None:
                    # Unified detail view: Enter always opens inspect,
                    # which exposes the action set (approve/reject/defer/
                    # retry merge/diff/etc.) gated by the item's current
                    # status. The `r`/`d` keys still kick off cycling
                    # walks through the review and deferred queues.
                    _enter_action(stdscr, cfg, store, daemon, sel)
                    # Flush any keys typed while the sub-screen was
                    # tearing down — a double-tap of 'q' to close
                    # inspect would otherwise bubble up and exit the app.
                    curses.flushinp()
                continue

            if k == "q":
                return
            if ch == ord("?"):
                _show_help(stdscr)
                continue
            if k == "r":
                _review_mode(stdscr, cfg, store, daemon)
            elif k == "n":
                _new_issue_mode(stdscr, cfg, store, daemon)
            elif k == "d":
                _deferred_mode(stdscr, cfg, store, daemon)
            elif k == "i":
                _inspect_mode(stdscr, cfg, store, daemon)
            elif ch in (ord("+"), ord("=")):
                # '=' is the unshifted key that shares '+'; accept both so
                # the user doesn't have to hold shift. Kick dispatch now so
                # the new slot is filled immediately instead of waiting for
                # the next scan.
                cfg.agent.pool_size += 1
                daemon.try_fill_pool()
            elif ch in (ord("-"), ord("_")):
                # Pool = 0 is a valid "pause" — in-flight workers finish
                # naturally, no new dispatches happen until you bump pool
                # back up.
                cfg.agent.pool_size = max(0, cfg.agent.pool_size - 1)
            elif k == "u":
                # Acknowledge a system alert and resume dispatching. No-op
                # when nothing is paused, so safe to spam.
                daemon.clear_alert()
            elif ch in (curses.KEY_SR, ord("P")) and selected_id:
                # Shift+Up (KEY_SR) bumps priority on the selected row.
                # `P` is the portable fallback — KEY_SR isn't emitted by
                # every terminal/multiplexer combo. Higher priority wins
                # in claim_next_queued's ORDER BY.
                try:
                    store.bump_priority(selected_id, 1)
                except KeyError:
                    pass  # row vanished between render and keystroke
            elif ch in (curses.KEY_SF, ord("O")) and selected_id:
                try:
                    store.bump_priority(selected_id, -1)
                except KeyError:
                    pass
            elif k == "\t":
                filter_idx = (filter_idx + 1) % len(FILTERS)
                selected_id = None  # reset cursor to top of new filter
    finally:
        _set_terminal_title(f"agentor[{cfg.project_name}] stopped")


def _idx_of(items: list[StoredItem], item_id: str | None) -> int:
    if not item_id:
        return 0
    for i, it in enumerate(items):
        if it.id == item_id:
            return i
    return 0
