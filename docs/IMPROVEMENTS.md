# Improvements backlog

Out-of-scope items discovered during in-progress work. Groom periodically
into `docs/backlog/` when picked up.

- **Ruff fails on `tests/test_config.py`** (3 × F401 unused imports:
  `ReviewConfig`, `SourcesConfig`, `ParsingConfig`). Present on `main`
  as of 2026-04-17 — CI (`ruff check agentor tests`) added in ef9fb29
  runs green because… actually it doesn't; the repo's own CI workflow
  would currently fail on main. Quick `ruff check --fix tests/test_config.py`.
