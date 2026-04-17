# Audit string-based status comparisons for enum drift — 2026-04-17

## Surprises
- Audit found zero latent bugs. Every existing `status == "..."` comparison already
  routed through `ItemStatus.XXX` or `ItemStatus.XXX.value`; no drift between
  literals and enum values. Work pivoted from "fix bugs" to "centralize the
  boundary so future drift can't happen silently".

## Gotchas for future runs
- The `transitions` table stores statuses as TEXT. `Store.transitions_for` now
  returns typed `Transition` dataclasses (attributes `from_status`,
  `to_status`, `note`, `at`) instead of raw dicts — `from_status` / `to_status`
  are already decoded to `ItemStatus`. Don't reach for `t["to_status"]` or
  `ItemStatus(t["to_status"])` in new code.
- All encoding/decoding crosses through `_encode_status` / `_decode_status` in
  `store.py`. Don't sprinkle `.value` at new callsites — route through the
  helpers so a future enum representation change stays local.

## Stop if
- `TestStatusRoundTrip` in `tests/test_store.py` fails after adding a new
  `ItemStatus` member — it means `list_by_status`, `count_by_status`, or the
  decode path never saw the new value. Fix upstream before moving on.
