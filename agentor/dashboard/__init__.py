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
    try:
        while True:
            try:
                _render(stdscr, cfg, store, daemon, log_ring, filter_idx)
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
    finally:
        _set_terminal_title(f"agentor[{cfg.project_name}] stopped")
