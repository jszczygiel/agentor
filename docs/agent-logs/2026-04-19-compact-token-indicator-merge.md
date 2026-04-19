# Compact token indicator — merge w/ responsive-layout main — 2026-04-19

## Surprises
- Main landed `a80f532 feat(dashboard): responsive layout tiers for narrow terminals` while this branch was out for review. The refactor restructured `_render` around `_layout_tier(w)` + `_build_status_line(tier, …)` + tier-aware `_render_token_panel(…, tier)`, replacing the flat status-line f-string that this branch extended.

## Gotchas for future runs
- `_build_status_line` signature is called positionally in tests — any new arg must be keyword-default to avoid breaking `TestStatusLineTier` call sites.
- Inline header additions should be wide-tier only. Mid tier has a hard ≤60-col budget and narrow ≤40 — appending any summary string clips counters that already abbreviate heavily. The token panel (which main made tier-aware) is the right surface for summaries at smaller widths.
- `_render_token_panel` now takes `tier` positionally and `windows` kwarg. Pre-computed `_token_windows` reuse still matters for the 500ms render tick even with the 2s TTL cache — explicit sharing keeps call-ownership obvious and insulates against future cache changes.

## Follow-ups
- None.
