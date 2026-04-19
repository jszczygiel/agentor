---
title: Feedback input reverted to single line — restore multi-line at every feedback site
state: available
category: bug
---

The "multi-line input field for feedback prompts" branch was merged (commit `ef37c2a`) and later a multi-line overlay `_prompt_multiline` landed in `297e86b`, but current `agentor/dashboard/render.py:458-476` is back to the single-row `curses.getstr` implementation — no `Textbox`, no `_prompt_multiline` helper at all, and every feedback-shaped caller in `agentor/dashboard/modes.py` is typing on the bottom line clipped to terminal width. Operators report that providing feedback of any meaningful length is effectively impossible.

Scope: restore the multi-line overlay and wire it at **every** feedback-providing site, not just the retry paths.

Call sites that must use the multi-line overlay:

- `modes.py:85` — pickup-mode approve-with-feedback (backlog/deferred → queued).
- `modes.py:536` — `_handle_reject_flow` reject+retry feedback (plan and code phases).
- `modes.py:705` — bug/idea note capture (feeds Claude expansion; users often want >1 line).

Keep single-line `_prompt_text` only for the item-id prefix at `modes.py:164` (genuinely a short slug, not feedback).

Implementation notes (from the 297e86b version that was lost): centered `newwin` frame with label + footer hint, inner `Textbox` with `stripspaces=False` so blank separator lines survive into `item.feedback`, validator maps Ctrl-C/Esc → cancel (empty string) and Ctrl-G → submit, graceful fallback to `_prompt_text` below 10 rows / 40 cols. Preserve the "empty = cancel" semantics `reject_and_retry` and `approve_plan` depend on.

Investigate first whether the helper was lost in a merge or deliberately reverted — there are large uncommitted reverts in the working tree touching `dashboard/render.py`, `dashboard/modes.py`, `runner.py`, and `config.py`, so the restore needs to land on a clean base.
