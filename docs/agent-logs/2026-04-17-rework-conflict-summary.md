# Rework conflict summary to lead with feature context — 2026-04-17

## Surprises
- The backlog source markdown `docs/backlog/rework-conflict-summary-to-lead-with-fea.md` was absent from both the working tree and git history when execution started, so the mandatory `git rm` step became a no-op. Deviation noted in the commit message.
- `tests/test_config.py` has three pre-existing unused-import lint failures (`ReviewConfig`, `SourcesConfig`, and likely one more). Not touched by this change.

## Gotchas for future runs
- `last_error` is capped to 4000 chars at the `store.transition` call in `committer.py`. When composing structured summaries, cap individual sections (body, raw git output) independently so the trailing section is not silently chopped off by the outer cap.
- `item.body` on items created from `checkbox` backlog files is the continuation-line block (dedented); titles and bodies are both available on `StoredItem` without extra lookups.

## Follow-ups
- Consider cleaning up the unused `ReviewConfig`/`SourcesConfig` imports in `tests/test_config.py` — would unblock CI ruff once it's strict.
