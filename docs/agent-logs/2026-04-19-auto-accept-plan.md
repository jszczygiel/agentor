# Flag to auto-accept plans (v1: always) — 2026-04-19

## Gotchas for future runs
- `approve_plan` now takes optional `note=` for the audit trail. Auto-paths pass `auto-accepted: <reason>` / `auto-accepted on recovery: <reason>`. Callers without a custom note fall back to the default message — no behavior change.
- Recovery sweep re-runs the auto-accept predicate against AWAITING_PLAN_REVIEW items to heal the crash window between plan-done and the daemon's in-worker auto-approve. New `RecoveryResult.auto_approved` field logged at startup.
- `auto_accept_plan` unknown values fall back to `"off"` with a one-time stderr warning (`_warned` set dedupes). Keeps stale configs safe.

## Follow-ups
- v2: `auto_accept_plan = "small"` with explicit `@auto` tag + keyword denylist predicate; also `auto_accept_verifier = "model"` as a second gate. Tag regex `TAG_RE = r"@(\w+):(\S+)"` currently requires a value — v2 either extends regex to accept bare `@auto` or documents the `@auto:true` convention.
