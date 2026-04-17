# dashboard split into render/modes/formatters/transcript — 2026-04-17

## Surprises
- `render.py` came in at 452 LOC vs the plan's ≤350 estimate. Curses overlay primitives (`_show_item_screen`, `_run_with_progress`, `_prompt_yn`, `_prompt_text`, `_wrap`, `_scroll_key`, `_flash`, `_view_text_in_curses`) all belong with the core renderers — splitting them off into a fifth module would have produced a thin shim, so they stayed in render. Still well under the 650 mode-file ceiling.
- Manual UI smoke (the plan's third acceptance bullet) isn't runnable in this agent environment — no live TTY. Import wiring + full unit suite passing is the best programmatic signal; a human will exercise the TUI during review.

## Gotchas for future runs
- `cli.py:11` is the **only** external importer of the dashboard. Any future dashboard split should preserve `from .dashboard import run_dashboard` as the public surface.
- Mode functions use lazy `from ..committer import …` imports inside function bodies to sidestep a circular import (committer imports store which ends up re-entering dashboard-adjacent code). Keep that pattern when adding new mode actions.
