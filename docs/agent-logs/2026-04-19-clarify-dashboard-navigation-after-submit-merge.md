# Clarify dashboard navigation after submitting merge — 2026-04-19

No code change this run. Original backlog note was truncated ("when i
submit merge i get bsvk to") — destination the operator finds unexpected
was never stated. Plan deliberately deferred any behaviour edit until
the operator picks a target; execute phase produces this log so a
follow-up item can act on a clear intent.

## Current behaviour (post-merge landing, by entry route)

1. **Main-table Enter → inspect (AWAITING_REVIEW) → `a`**
   - `_enter_action` opens `_inspect_render` with `cycle=False`.
   - After `approve_and_commit`, status AWAITING_REVIEW → MERGED.
   - `_inspect_render` sees status change, returns `""` → main table.
   - `dashboard/__init__.py:67-74` cursor-snap kicks in: selected item
     jumped list-position, so cursor sticks to the **visual index** the
     row used to occupy — not the merged row, not any specific "next"
     row. Whatever happens to be at that index wins.

2. **Main-table `r` → `_review_mode` → inspect → `a`**
   - `_inspect_render` opened with `cycle=True`.
   - After merge, returns `""` → review loop picks next item via
     `_next_review_item` (AWAITING_PLAN_REVIEW first, then
     AWAITING_REVIEW, filtered by `seen_ids`).
   - Empty queue → drops to main table. Otherwise → immediately shows a
     different item, which can feel like "I merged X and now I'm
     looking at unrelated Y".

3. **CONFLICTED `[m]` retry**
   - Success (CONFLICTED → MERGED): cycle=False, status changed →
     same main-table landing + cursor-snap as route 1.
   - Still conflicted: status unchanged, stays on inspect view with
     updated `last_error`. (This path is not ambiguous.)

## Options for the operator

- **A. Stay on the merged item's inspect view** so the MERGED summary,
  token breakdown and commit note are visible. Requires `_inspect_render`
  to not auto-close on MERGED-from-approval, and `_review_mode` to drop
  its cycle contract for this case.
- **B. Advance to next review candidate** (current behaviour for route
  2, extend to routes 1+3). Conveyor-belt flow.
- **C. Return to main table with cursor explicitly on the merged row**
  (override the general cursor-snap in `dashboard/__init__.py:67-74`
  for the merge transition). Operator sees the row moved into MERGED.
- **D. Return to main table with cursor on the next actionable row**
  (first AWAITING_* or first non-terminal).

Uniform-across-routes vs per-route is itself a question.

## Open questions (blocking any implementation)

1. Which entry route produced the unexpected landing?
2. Which destination (A/B/C/D or other)?
3. Same rule everywhere, or per-route?

## Follow-ups

- New backlog item needed once operator answers above. This item ships
  with no behaviour change — investigation captured here.

## Stop if

- A future agent picks up the same backlog without operator
  clarification. Without concrete intent, any pick is a guess and
  will likely land the operator somewhere else they didn't expect.
