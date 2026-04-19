"""Guard against `ItemStatus` member drift in enum-keyed dicts.

Past incident: `_STATE_GLYPHS` carried `ItemStatus.BACKLOG: "B"` after the
enum member was removed; mypy didn't flag it because the dict type
(`dict[ItemStatus, str]`) accepts any current-or-historical member name.
The breakage surfaced at import time on the next dashboard launch.

Add a new exhaustive enum-keyed dict? Wire it into `_DICTS_THAT_MUST_COVER_ALL`
and the next rename / add / remove of an `ItemStatus` member will fail this
test instead of breaking at runtime.
"""

import unittest

from agentor.dashboard.modes import _ACTION_KEYS_BY_STATUS
from agentor.dashboard.render import _STATE_GLYPHS
from agentor.models import ItemStatus


# Every dict in this list MUST have one entry per ItemStatus member.
# Add new exhaustive enum-keyed dicts here; partial dicts (e.g. fold's
# _NON_TERMINAL_STATUSES tuple) intentionally exclude terminal states and
# don't belong here.
_DICTS_THAT_MUST_COVER_ALL: list[tuple[str, dict[ItemStatus, object]]] = [
    ("dashboard.render._STATE_GLYPHS", _STATE_GLYPHS),
    ("dashboard.modes._ACTION_KEYS_BY_STATUS", _ACTION_KEYS_BY_STATUS),
]


class TestEnumExhaustiveness(unittest.TestCase):
    def test_every_status_keyed_dict_covers_all_members(self) -> None:
        all_members = set(ItemStatus)
        for name, mapping in _DICTS_THAT_MUST_COVER_ALL:
            with self.subTest(dict=name):
                keys = set(mapping.keys())
                missing = all_members - keys
                extra = keys - all_members
                self.assertFalse(
                    missing,
                    f"{name} missing entries for: "
                    f"{sorted(m.name for m in missing)}",
                )
                self.assertFalse(
                    extra,
                    f"{name} has stale entries (no longer ItemStatus members): "
                    f"{sorted(getattr(e, 'name', repr(e)) for e in extra)}",
                )


if __name__ == "__main__":
    unittest.main()
