import curses
from collections import deque

from ..config import Config
from ..daemon import Daemon
from ..store import Store

from .render import (
    FILTERS,
    REFRESH_MS,
    _init_colors,
    _render,
    _set_terminal_title,
)
from .modes import (
    _deferred_mode,
    _enter_action,
    _inspect_mode,
    _pickup_mode,
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
    selected_idx = 0  # row cursor inside the rendered items list
    items: list = []
    try:
        while True:
            try:
                items = _render(stdscr, cfg, store, daemon, log_ring,
                                filter_idx, selected_idx)
                # Clamp selection against the freshly-rendered list: items
                # move between status buckets as the daemon works, and the
                # cursor shouldn't dangle past the end.
                if items:
                    selected_idx = max(0, min(selected_idx, len(items) - 1))
                else:
                    selected_idx = 0
                ch = stdscr.getch()
            except KeyboardInterrupt:
                # ctrl-c in the dashboard should exit cleanly, not crash
                # with a stack trace. The daemon is a daemon thread;
                # leaving the loop lets curses.wrapper restore the
                # terminal and the process exits.
                return
            if ch == -1:
                continue
            k = chr(ch).lower() if 0 < ch < 256 else ""
            if k == "q":
                return
            if ch in (10, 13, curses.KEY_ENTER):
                # Route enter by the selected row's status — pickup for
                # backlog/deferred, review for the awaiting states, inspect
                # for everything else.
                _enter_action(stdscr, cfg, store, daemon, items, selected_idx)
                continue
            if ch in (curses.KEY_DOWN, ord("j")):
                if items:
                    selected_idx = min(selected_idx + 1, len(items) - 1)
                continue
            if ch in (curses.KEY_UP, ord("k")):
                selected_idx = max(0, selected_idx - 1)
                continue
            if k == "r":
                _review_mode(stdscr, cfg, store, daemon)
            elif k == "p":
                _pickup_mode(stdscr, cfg, store, daemon)
            elif k == "d":
                _deferred_mode(stdscr, cfg, store)
            elif k == "i":
                _inspect_mode(stdscr, cfg, store)
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
            elif k == "m":
                cfg.agent.pickup_mode = (
                    "auto" if cfg.agent.pickup_mode == "manual" else "manual"
                )
                if cfg.agent.pickup_mode == "auto":
                    daemon.try_fill_pool()
            elif k == "u":
                # Acknowledge a system alert and resume dispatching. No-op
                # when nothing is paused, so safe to spam.
                daemon.clear_alert()
            elif k == "\t":
                filter_idx = (filter_idx + 1) % len(FILTERS)
                selected_idx = 0  # filter change rebuilds the list
    finally:
        _set_terminal_title(f"agentor[{cfg.project_name}] stopped")
