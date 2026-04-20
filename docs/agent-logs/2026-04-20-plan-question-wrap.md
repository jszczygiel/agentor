# Fix question rendering in plan Q&A overlay — 2026-04-20

## Surprises
- `_build_detail_lines` is reachable from pure tests with a `SimpleNamespace` cfg, but `_transcript_path_for` reads `cfg.project_root` (flat attr), not `cfg.project.root` — the cfg stub needs both aliases when exercising inspect detail assembly.
- `_inspect_dispatch` is invoked with `stdscr=None` in tests, so the overlay-width lookup must guard `stdscr.getmaxyx()` with a fallback rather than blindly calling it.

## Gotchas for future runs
- `curses.textpad.Textbox` does not soft-wrap — seeded lines longer than the overlay's `inner_cols` are visually clipped even with `stripspaces = False`. When adding new multiline prompts, pre-wrap seed text to `min(80, w-4) - 2` (match `_prompt_multiline`'s geometry) and use a leading-indent continuation so `_parse_answers`'s `^\s*[QA]\d+:` marker regex cannot false-match.
- The inspect view renders via `_show_item_screen`, which hard-clips every line to terminal width (`line[:w]`). Any new content list that can carry arbitrary-length strings must wrap up front; do not rely on curses to soft-wrap.

## Follow-ups
- `_parse_answers` regex still false-matches if a question's body itself contains `Q<n>:` or `A<n>:` mid-text after a wrap boundary — pre-existing hazard, same as when questions carry raw newlines. Worth a dedicated backlog item to migrate the Q/A delimiter to something the parser owns (e.g. unique sentinel) if this bites.

## Outcome
- Files touched: `agentor/dashboard/modes.py`, `tests/test_dashboard_inspect_dispatch.py`, `docs/agent-logs/2026-04-20-plan-question-wrap.md` (new). Backlog source `docs/backlog/fix-question-rendering-in-plan-q-a-overl.md` was already absent at dispatch — no `git rm` needed.
- Tests added: `TestAnswersScaffoldAndParse::test_scaffold_wraps_long_question`, `TestAnswersScaffoldAndParse::test_scaffold_short_questions_unchanged`, new class `TestBuildDetailLinesQuestionWrap` with `test_long_question_wraps_in_inspect_view` + `test_short_question_renders_single_line`.
