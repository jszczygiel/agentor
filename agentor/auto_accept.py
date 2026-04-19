"""Predicate deciding whether a plan-review gate can auto-approve.

Pure function, no store/runner/daemon deps — unit-testable in isolation.
v1 implements `off` and `always`; future values (`small` with per-item
gating, model-verifier) will extend this same predicate.
"""
import sys

from .config import Config
from .models import Item
from .store import StoredItem

_VALID_MODES = ("off", "always")
_warned: set[str] = set()


def should_auto_accept(
    config: Config, item: Item | StoredItem,
) -> tuple[bool, str]:
    """Return (decision, reason). `reason` is a short tag used in the
    transition note (`auto-accepted: <reason>`) and in recovery logs.

    Unknown `auto_accept_plan` values fall back to `off` and warn once on
    stderr so operators notice typos without crashing the daemon."""
    mode = config.agent.auto_accept_plan
    if mode == "off":
        return False, "off"
    if mode == "always":
        return True, "always"
    if mode not in _warned:
        _warned.add(mode)
        print(
            f"[auto_accept] unknown agent.auto_accept_plan={mode!r}; "
            f"treating as 'off'. Valid: {_VALID_MODES}",
            file=sys.stderr,
        )
    return False, "off"
