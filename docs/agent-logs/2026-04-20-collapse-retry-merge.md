# Collapse retry-merge and resubmit-to-agent actions — 2026-04-20

## Surprises
- `_inspect_dispatch` uses a lazy `from ..committer import ...` block (documented in CLAUDE.md); removing `resubmit_conflicted` from that import list is enough to drop the binding — no callers elsewhere in the dashboard.

## Gotchas for future runs
- Patching a lazily-imported committer function from a dispatch test works via `patch("agentor.committer.<name>")` because the local `from ..committer import <name>` reads the module attribute each call. Useful pattern for future inspect-dispatch tests.
- `_run_with_progress` runs the work on a background thread and expects curses — tests covering dispatch paths that go through it must patch `agentor.dashboard.modes._run_with_progress` with a synchronous shim (call `work(lambda _m: None)` and return the result).

## Follow-ups
- `resubmit_conflicted` is now reachable only via `git.auto_resolve_conflicts=True` auto-chain from `approve_and_commit`. If a future operator wants a manual escape hatch back, adding a new action key is trivial — the function is still exported.
