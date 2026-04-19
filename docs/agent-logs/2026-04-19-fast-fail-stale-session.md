# Fast-fail stale-session on recovery sweep — 2026-04-19

## Surprises
- The source backlog markdown `docs/backlog/fast-fail-stale-session-in-recovery.md` did not exist in this worktree (or anywhere in git history). The mandatory `git rm` step was a no-op; only `docs/backlog/deduplicate-transcript-parsing.md` is present.
- `_AUTO_RECOVERABLE_PATTERNS` runs *in the same call* as the new stale-session demotion. Adding the new marker to the pattern list immediately wipes it during the same sweep, hiding the operator-visible "session expired; restarting plan" marker. Fix: skip just-demoted ids from the benign sweep so the marker survives one tick and clears on the next startup.

## Gotchas for future runs
- `_error_signature` strips digits + whitespace + paren groups — substring matches on `error_sig` need the spaces removed (`"noconversationfoundwithsessionid"`), while raw `error` matches use the full lowercased phrase. `_has_dead_session_failure` checks both forms.
- `recover_on_startup` runs three sweeps (WORKING demotion, benign-error clear, terminal stale-error clear) sequentially over a shared store. Anything written in sweep 1 is visible to sweep 2 — order and inter-sweep filters matter.

## Follow-ups
- Lingering worktree dir after stale-session demotion: `claim_next_queued` overwrites `worktree_path` to a fresh slug, so the old directory stays on disk until external cleanup. Logged to `docs/IMPROVEMENTS.md`.
