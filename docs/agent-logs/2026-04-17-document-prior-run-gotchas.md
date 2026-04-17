# Document prior-run gotchas in CLAUDE.md — 2026-04-17

## Surprises
- CLAUDE.md line 47 itself cited `agentor/dashboard.py` (now a package) — the
  exact trap the new "stale `file:line` refs" gotcha warns about. Fixed in the
  same commit; kept in scope because the bullet would read as ironic otherwise.

## Gotchas for future runs
- Before editing CLAUDE.md, grep it for refs to any module that has recently
  been split into a package; mid-document drift is easy to miss when only the
  target section is under review.
