# feedback-input-reverted-to-single-line — 2026-04-19

## Surprises
- Backlog premise was false. `_prompt_multiline` is live at `agentor/dashboard/render.py:705` and wired into all three feedback callsites (`modes.py:277` plan-approve feedback, `:287` plan-retry, `:312` code-retry). `_prompt_text` only remains for item-id prefix (`:141`) and defer note (`:807`) — both intentional per original `docs/backlog/multi-line-feedback-input.md` scope.
- Ticket's `file:line` refs (`render.py:458-476`, `modes.py:85/536/705`) are all stale. "Pickup feedback at `:85`" references a path that no longer exists — pickup mode was deleted in commit `28168db`.

## Gotchas for future runs
- CLAUDE.md already warns about stale `file:line` refs. This ticket is a textbook case: grep the stable symbol (`_prompt_text`, `_prompt_multiline`) before trusting any path cited in the description.
- When a backlog item claims a feature regressed, verify against HEAD before planning work — `git log --all --oneline --grep=<feature>` + direct grep of the relevant symbols takes <1 min and would have caught this at triage.

## Follow-ups
- None. If operator reports persist, likely root cause is stale install or discoverability of the Ctrl-G submit hint — worth asking for terminal size + exact keypresses rather than re-opening a code change.

## Stop if
- Another ticket claims a dashboard prompt regressed and cites specific line numbers. Grep the prompt helper names first; if they already match the cited behavior, close as no-op with a pointer to this log.
