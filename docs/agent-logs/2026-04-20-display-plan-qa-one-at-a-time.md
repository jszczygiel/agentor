# Display plan Q&A questions one at a time — 2026-04-20

## Surprises
- Reusing `_answers_scaffold([q])` (single-item) + `_parse_answers(reply, 1)` unchanged made the loop trivial; no new parsing logic needed.
- The empty-reply / Ctrl-C vs skip distinction falls out naturally: non-empty reply always contains at least the `Q1:` scaffold line, so `_parse_answers` returns `[""]` for a blank answer while Ctrl-C returns `""`.

## Gotchas for future runs
- `_prompt_multiline` returns `""` for both Ctrl-C/Esc cancels AND for terminal-too-small fallback (`_prompt_text` returning empty). Both correctly trigger the abort path here.
- `approve_plan` already filters `any(a.strip())` before writing `answers` to `result_json` — so all-blank collected answers produce no "answers" key, same as having no questions.

## Outcome
- Files touched: `agentor/dashboard/modes.py`, `tests/test_dashboard_inspect_dispatch.py`, `docs/agent-logs/2026-04-20-display-plan-qa-one-at-a-time.md`.
- Tests added: `test_plan_review_approve_with_questions_prompts_for_answers` (rewritten), `test_plan_review_approve_with_questions_empty_reply_on_first_cancels` (renamed), `test_plan_review_approve_abort_mid_sequence_preserves_answers` (new), `test_plan_review_approve_skip_question_records_blank` (new).
