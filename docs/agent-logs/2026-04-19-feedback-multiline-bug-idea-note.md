# feedback multi-line — bug/idea note — 2026-04-19

## Surprises
- Ticket premise largely stale (same as the `820515ce` no-op): `_prompt_multiline` is live at `render.py:724`, the three retry feedback callsites already use it, pickup mode was deleted in `28168db`, `_handle_reject_flow` does not exist. Only genuinely open site was the bug/idea note prompt, which was already logged in `docs/IMPROVEMENTS.md` as an intentional follow-up from `297e86b`.
- Source markdown `docs/backlog/feedback-input-reverted-to-single-line.md` not present in the tree — noted in the commit body in place of a `git rm`.

## Gotchas for future runs
- `_new_issue_mode` consumes `scan_once(...).new_items` — tests that drive the mode end-to-end must return an object with `.new_items`, not a bare list. Existing module tests only covered the overlay widget; adding a site-level test needed this shape.
- Pre-existing F401 ruff errors in `tests/test_dashboard_resize.py` (introduced in `6cde420`) surface alongside the long-standing `tests/test_config.py` ones. Logged under IMPROVEMENTS.md. Ruff still exits non-zero on this tree even with a clean diff — don't block on that signal alone.

## Follow-ups
- None for this item. Adaptive overlay reflow on KEY_RESIZE and the `test_dashboard_resize.py` F401 cleanup remain in `docs/IMPROVEMENTS.md`.

## Stop if
- A future ticket again claims "multi-line feedback regressed" with `file:line` refs that don't grep. The symbol-grep check in the `820515ce` log plus this one should short-circuit re-planning.
