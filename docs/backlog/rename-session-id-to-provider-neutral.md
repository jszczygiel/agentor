---
title: Rename `session_id` column to provider-neutral name
state: available
tags: [refactor, multi-provider]
---

`items.session_id` is Claude CLI vocabulary. Codex runner overloads it for
`thread_id`; a third provider inherits the misnomer. Rename the column to
`agent_ref` (or `resume_token`) and route all reads/writes through
`Store` helpers — same pattern as `_encode_status` / `_decode_status`.

Touch points:
- `agentor/store.py:29` schema, `:76` migration, `:144` dataclass, `:166` row
  mapping, `:343` mutable-field whitelist.
- `agentor/models.py` `StoredItem.session_id`.
- `agentor/runner.py` ~40 refs across `ClaudeRunner` / `CodexRunner` /
  `Runner.run` — all `item.session_id` / `session_id=` transitions.
- `agentor/recovery.py:140` `.session_id` checks, `:152` clears.
- `agentor/committer.py:425`, `:499`, `:635` docstrings/assignments.
- `agentor/dashboard/modes.py:515` (`session:  {item.session_id or '—'}`).
- `agentor/dashboard/transcript.py:21` phase derivation.

Add `_migrate` healing: rename column, preserve values. Enum/string
round-trips through a single SQLite boundary helper. Tests:
`tests/test_store.py` round-trip + migration from legacy schema.
