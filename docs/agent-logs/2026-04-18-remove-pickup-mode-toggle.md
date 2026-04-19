# remove pickup-mode toggle — 2026-04-18

## Surprises
- `Store.upsert_discovered` was the tidier place to land items at QUEUED than `scan_once`: removes a second write per new item and collapses the whole manual gate to a one-line change.
- The existing `test_watcher.test_scan_enqueues_new_items` was already asserting `QUEUED` (a latent expectation of auto-mode). Pre-plan assumption that tests would explode was wrong for the watcher suite; only `test_store`, `test_runner::TestDaemonPickupModes`, and the dashboard action-hint guard needed real rewrites.
- `test_runner::TestDaemonPickupModes::test_dispatch_specific_works_in_manual_mode` relied on manual mode to avoid a race between auto-dispatch and the specific-id dispatch. Post-change, the rewritten test drives `dispatch_specific` *without* starting the daemon thread to keep it race-free.

## Gotchas for future runs
- `dispatch_specific` still enforces `pool_has_slot`. Tests that drive it in isolation should either use `pool_size >= 1` with no daemon thread, or bump pool + join the dispatched worker deterministically. The "start daemon, then dispatch specific" pattern races under auto-discovery.
- Removing a known key from a dataclass like `AgentConfig` is safe because `_filter_known` already warns-and-drops unknown keys, but only if the dropped key has no `default_factory` or downstream reader. Anything touching `cfg.agent.pickup_mode` would have crashed with `AttributeError`; grep before deleting.
- The dashboard action-hint regression guard (`test_dashboard_render.TestActionsHint.test_core_actions_present`) is easy to miss — it asserts specific `[x]…` tokens in `ACTIONS`. Any time you change the actions bar, update both the string and this test.

## Follow-ups
- `lancelot/agentor.toml` still sets `pickup_mode = "manual"`. After this change it will print a one-line `[config] ignoring unknown key [agent].pickup_mode` warning on startup. Coordinate a trivial cleanup PR with the lancelot operator (not blocked by this change).
- `approve_backlog` was deleted; the one remaining reference to the BACKLOG gate is the inline fallback in `_pickup_one_screen` for legacy DEFERRED rows whose history walks back to BACKLOG. If we remove BACKLOG from the enum entirely in a future pass, that branch goes too.

## Stop if
- A future run finds an item stuck at `BACKLOG` in a fresh DB — means `Store.upsert_discovered` regressed; `tests/test_watcher.py::test_scan_lands_at_queued_not_backlog` should catch it.
