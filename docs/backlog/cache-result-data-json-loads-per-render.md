---
title: Cache `_result_data` json.loads per item.updated_at
state: available
tags: [perf, dashboard]
---

`_result_data` in `agentor/dashboard/formatters.py:61` calls
`json.loads(item.result_json)` every call with no memoization. It is
invoked multiple times per visible row per render tick (500ms) via
`_ctx_fill_pct`, `_phase_for`, `_tokens_total`, `_progress_data`,
`_build_commit_message`, and `_tokens_split`.

For WORKING rows with large live envelopes (result_json grows with the
`iterations` array — measured up to 30-40 KB in
`.agentor/state.db`), this is redundant parsing on every tick even when
the row hasn't changed.

Fix: replace `_result_data` with a small module-level cache keyed on
`(item.id, item.updated_at)`:

```python
_result_cache: dict[tuple[str, float], dict] = {}

def _result_data(item):
    if not item.result_json:
        return None
    key = (item.id, item.updated_at)
    hit = _result_cache.get(key)
    if hit is not None:
        return hit
    try:
        data = json.loads(item.result_json)
    except json.JSONDecodeError:
        return None
    # Drop the prior snapshot for this item so the cache can't grow
    # without bound across a long session.
    for k in list(_result_cache):
        if k[0] == item.id:
            del _result_cache[k]
    _result_cache[key] = data
    return data
```

Also expose `_result_data_invalidate()` mirroring the existing
`_token_windows_invalidate()` so tests that seed `result_json` can
bust the cache deterministically.

Bounded by `len(items)` (one entry per item); no TTL needed because
`updated_at` changes every time `update_result_json` fires, so stale
entries get replaced automatically.
