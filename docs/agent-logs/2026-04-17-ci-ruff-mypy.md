# CI with ruff and mypy — 2026-04-17

## Surprises
- `dashboard/modes.py:347` had a nested f-string using the same quote character as the outer f-string — only legal on Python 3.12+ (PEP 701). Broke ruff parse on declared `requires-python = ">=3.11"`. Fix: pre-extract the nested expression.
- `TimeoutExpired.stdout`/`.stderr` is typed `Any | bytes | None` in typeshed regardless of Popen's `text=True`; mypy flags `str-bytes-safe` on f-string interpolation. Guarded with `isinstance(..., bytes)` decode.

## Gotchas for future runs
- Ruff + mypy must both be pinned to `py311`/`python_version = "3.11"` in `pyproject.toml`, not inferred from the local interpreter (devs may be on 3.13 locally while CI is 3.11).
- `store.get(id)` returns `StoredItem | None`; callers that just transitioned an item need a narrow `assert refreshed is not None` before passing to functions that expect `StoredItem`.

## Follow-ups
- None — scope-cap held.

## Stop if
- Mypy surfaces a class of errors needing structural type changes (Protocols, overloads). Current pass resolved everything with asserts/narrows; if a future change demands real type plumbing, that's a separate task.
