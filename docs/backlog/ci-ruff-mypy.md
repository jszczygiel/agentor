---
title: Add CI with ruff and mypy
state: available
category: infra
---

No CI, no lint, no type-check configured. Add `.github/workflows/ci.yml`
running `python3 -m unittest discover tests`, `ruff check`, and `mypy agentor`
on push/PR. Configure `ruff` + `mypy` in `pyproject.toml`. Keep the
stdlib-only policy: ruff and mypy are dev-only, not runtime deps. Fix any
lint/type errors surfaced (scope-cap: do not refactor beyond what the
checks demand).
