# Responsive dashboard for narrow terminals — 2026-04-19

## Gotchas for future runs
- Headless render tests need `unittest.mock.patch.object(curses, "color_pair", return_value=0)` — `_render` calls `curses.color_pair(4)` for the alert banner, which raises without `initscr()`. Same workaround likely needed for any future render-path test.
- `curses.addnstr` clip semantics: pass `n = w` (not `w - x`) from `_safe_addstr`. The stub screen used `clipped = s[: max(0, n - x)]` to mirror this.
- `ACTIONS` constant is still re-exported (aliased to `ACTIONS_WIDE`) because existing `tests/test_dashboard_render.py` imports it. Removing that alias breaks the regression guard tests.

## Stop if
- Changing width thresholds (80 / 60): every tier-fit test asserts against those boundaries and `ACTIONS_MID` / status-line lengths are tuned to fit 60 cols exactly. Flipping to 70 would require re-tuning `ACTIONS_MID`.
