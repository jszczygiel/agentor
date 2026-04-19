---
title: Fix mypy func-returns-value on _run_with_progress lambda
state: available
category: bug
---

Pre-existing mypy error at `agentor/dashboard/modes.py:760` — the
`_run_with_progress` callsite uses the tuple-indexing trick
`lambda p: (p("…"), work(...))[-1]` to sequence a progress update with a work
call inside a single-expression lambda. Mypy reports `func-returns-value`
because `p(...)` returns `None` and the tuple-subscript loses the type.

Surfaced four times across agent-logs (enum-drift, split-approve,
enter-merge, enter-merge-2) and logged to `docs/IMPROVEMENTS.md` twice. Gates
any future strict-mypy pass.

Fix: rewrite the callsite as a named inner function (or a pair of statements
in a local helper). No behavior change.

Verification:

- `mypy agentor` is clean after the change (currently reports exactly these
  two `func-returns-value` errors on `modes.py` — both gone).
- `python3 -m unittest discover tests` passes.
