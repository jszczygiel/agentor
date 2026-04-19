# Surface CONFLICTED auto-resolve chain in dashboard — 2026-04-19

## Surprises
- Source backlog `docs/backlog/surface-auto-resolve-conflict-chain.md` was already absent in this worktree (not in git history on this branch). Source-removal step from the task prompt became a no-op — noted in the commit.
- `_build_detail_lines` surfaces no transition history today — only `failure history`. A dedicated one-line `flow:` marker fit better than retro-fitting a transition block.

## Gotchas for future runs
- CLAUDE.md's lazy-committer-import rule extends to module-level constants, not just action functions. A plain `from ..committer import X` at the top of `dashboard/modes.py` is a landmine even for a string constant — use a lazy import inside the consumer.
- `Store.transitions_for` returns the full history; tail-slice (`history[-10:]`) before iterating when called from inspect-tick hot paths.

## Follow-ups
- None. Marker is propagated end-to-end; manual `[e]` path deliberately unchanged.
