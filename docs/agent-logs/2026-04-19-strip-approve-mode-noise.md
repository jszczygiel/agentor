# Strip run-mechanics from approve mode — 2026-04-19

## Surprises
- Backlog source `docs/backlog/remove-text-from-approve-mode.md` was never tracked in git (`git log --all` empty). Dispatcher evidently dropped and consumed it in-flight. No `git rm` needed — left out of commit rather than failing hard.
- `_tokens_total` import in `dashboard/modes.py` became dead once the `tokens:` line in AWAITING_REVIEW was removed; removed the stale import too.

## Gotchas for future runs
- There is no standalone "approve mode" screen — approve UX is `_inspect_render` rendered for `AWAITING_PLAN_REVIEW` / `AWAITING_REVIEW`. Any "approve view" tweak should be scoped via `item.status`, not via a new screen.
- `_build_detail_lines` has an early return in the no-`data` branch; status-conditional guards must cover both the early-return and main code paths.

## Stop if
- Reviewer asks for "no plan text" — then this interpretation (A) was wrong and the edit should be reverted in favour of a keybind-driven pager (B).
