---
title: Fix F401 unused-import errors in tests/test_config.py
state: available
category: bug
---

`tests/test_config.py:9-10` imports `ReviewConfig`, `ParsingConfig`, and
`SourcesConfig` but never references them. Ruff reports three F401
"imported but unused" errors. Four separate agent runs (`ci-ruff-mypy`,
`remove-unpause-footer`, `audit-enum-drift-merge`,
`rework-conflict-summary`) have rediscovered and logged this — clear
signal it should be resolved.

Decide between:
- Delete the imports if they were left over from a refactor.
- Add `# noqa: F401` with a comment if they are intentional symbol
  exports (e.g. for `from tests.test_config import *` patterns).

Then check the CI workflow: `ruff check` should be failing on these
already. If CI is green, the workflow is somehow ignoring them — tighten
the ruff config so future drift is caught at PR time, not by agents
re-stumbling on it.
