# merge main into remove-pickup-mode — 2026-04-18

## Surprises
- Main added a `priority` column + `bump_priority` + shift-arrow keybindings while this branch was in flight. Auto-merge handled store.py, modes.py, `__init__`.py cleanly; only `committer.py` (approve_backlog deletion vs. approve_plan signature change) and `render.py` (ACTIONS string) needed hand resolution.
- Main's `TestApproveFeedbackSplit` had a `_backlog_item` helper that transitioned the seeded item back to BACKLOG via a self-loop — that path still works after this branch's changes (BACKLOG remains a legal enum value), but the tests were paired with the now-deleted `approve_backlog`, so they go too.
- Main's `test_missing_priority_column_heals_on_open` asserted items land in BACKLOG — a latent assumption about `upsert_discovered`'s status that this branch inverts. Fix was one-line: flip the list_by_status lookup to QUEUED.

## Gotchas for future runs
- After deleting a function like `approve_backlog`, `grep -rn <name>` across both `agentor/` and `tests/` is mandatory — the import in `tests/test_committer.py` would have silently tanked the whole test module on first collection.
- ACTIONS string conflicts are the canonical signal that the dashboard keybindings have drifted on both sides of a merge. Review each token individually — don't pick one side wholesale.

## Follow-ups
- Lancelot's `agentor.toml` still carries `pickup_mode = "manual"`; expect a one-line `[config] ignoring unknown key` warning until that repo drops the line. Not blocking.
