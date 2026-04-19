# Surface checkout-advance outcome on MERGED note — 2026-04-19

## Surprises
- The ticket was implemented concurrently on main via commit
  `c211196` while this branch's own implementation (44a6604) sat
  unmerged. Conflict resolution accepted main's design in full and
  added only the ticket's explicit "surface a visible, non-fatal
  dashboard message — not silent" requirement on top as a follow-up
  commit. Main's silent-on-skip behavior is what the ticket flagged as
  a gap; the remaining delta is the note suffix.
- The "HEAD diverged from pre-merge base" guard branch is practically
  unreachable end-to-end. The committer captures `base_sha_before`
  inside `_INTEGRATION_LOCK` and evaluates the guard a few
  microseconds later — for the user to race a commit into that window
  they'd need to land two commit operations faster than the lock body
  runs. The guard remains as defensive code; end-to-end coverage
  replaced with a monkeypatch test that verifies the committer threads
  the reason string through when the guard reports it.

## Gotchas for future runs
- When reconciling with a branch that already landed your feature on
  main, `git checkout --theirs -- <file>` then delete any of your own
  now-obsolete scaffolding (test classes, log files, helpers) — do NOT
  try to preserve your earlier design alongside main's. Two overlapping
  implementations is always worse than one.
- `advance_user_checkout_allowed` returns `(bool, str | None)` — the
  reason string is part of the public contract for UI surfacing. If
  you ever add a new guard, add a matching reason literal and update
  the CLAUDE.md enumeration of possible suffixes so operators can grep
  for them.

## Follow-ups
- None — ticket is now fully satisfied (main's implementation + this
  branch's surfacing delta).
