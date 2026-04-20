# Unify streaming-usage envelope across providers — 2026-04-20

## Surprises
- On-disk codex shape (`iterations: []`, empty list) looks identical to a
  pre-any-turn claude envelope. First draft of `_ctx_fill_pct` short-
  circuited on `env.iterations is None` alone, which correctly caught
  codex reads but broke an existing formatter test
  (`test_usage_fallback_when_no_iterations`) where a legacy claude blob
  predates the `iterations` key. Gate is now `iterations is None AND
  usage.all_none()` — the two together mean "nothing reported", not
  "legacy shape".
- `_StreamState.iterations` pre-existing dict entries already use
  snake_case token keys (`input_tokens`, etc.) while `model_usage`
  entries use camelCase (`inputTokens`, etc.). The dual-case key
  convention on disk is load-bearing — `from_legacy_dict` and
  `to_legacy_dict` have to reproduce it exactly.

## Gotchas for future runs
- On-disk codex vs claude-before-any-turn: distinguish by `usage == {}`
  (codex) vs `usage == {"input_tokens": 0, ...}` (claude). Codex's
  envelope intentionally writes `usage: {}` as the sentinel; claude's
  `from_claude` always computes a flat sum which yields all-zero keys,
  not an empty dict. `Envelope.from_legacy_dict` uses `isinstance(raw,
  dict) and raw` (non-empty) as the discriminator.
- `_result_data` caches parsed dicts for 500ms-tick perf; `_envelope_for`
  wraps each call and rebuilds the dataclass on every invocation. Keep
  `from_legacy_dict` allocation-cheap — no JSON decode, just object
  construction — because it runs per visible row per tick.
- `_tokens_for_model(mu_entry: dict)` stays on the dict shape rather
  than operating on `ModelUsage` because `dashboard/modes.py` and any
  future caller of the public helper reads directly off
  `data["modelUsage"][m]`. The envelope path internal to
  `_tokens_total`/`_tokens_split`/`_token_breakdown` uses
  `ModelUsage.sum_reported()` / `.all_counters_none()`.

## Follow-ups
- Migrate `tools/analyze_transcripts.py` and `agentor/committer.py`
  readers off the raw legacy dict and onto `Envelope.from_legacy_dict`
  (out of scope for this round per the backlog). `aggregate_token_usage`
  in `agentor/store.py` also still reads the raw dict — same follow-up.
- `_tokens_total` currently renders `—` even for a claude run that
  genuinely reported zero everywhere. User-visible column can't usefully
  distinguish the two ("reported zero" vs "nothing"), but the envelope
  itself preserves the distinction — a future analytics/observability
  consumer could surface it.

## Stop if
- `Envelope.to_legacy_dict` stops preserving the exact key set — look
  at `TestLegacyDictKeyDrift` for the golden contract. On-disk consumers
  (`aggregate_token_usage`, archived `transcripts/*.log`, the
  `_result_data` cache → every formatter) index by these key names and
  break silently if renamed.
- `_CodexStreamState.envelope(result_text=…)` callers pass a value
  mined from the `--output-path` file; ensure `from_codex` still
  forwards it through `result_text or state.result_text` — losing the
  kwarg override removes the final-message fallback entirely.

## Outcome
- Files touched:
  - `agentor/envelope.py` (new)
  - `agentor/runner.py` (two `envelope()` bodies → `Envelope` delegation)
  - `agentor/dashboard/formatters.py` (`_tokens_total`, `_ctx_fill_pct`,
    `_tokens_split`, `_token_breakdown`)
  - `tests/test_envelope.py` (new)
  - `tests/test_dashboard_formatters.py` (codex-shape class)
  - `docs/backlog/unify-stream-envelope-across-providers.md` (deleted)
- Tests added/adjusted:
  - `tests/test_envelope.py::TestEnvelopeFromClaude`
  - `tests/test_envelope.py::TestEnvelopeFromCodex`
  - `tests/test_envelope.py::TestLegacyDictKeyDrift`
  - `tests/test_envelope.py::TestFromLegacyDictSemantics`
  - `tests/test_envelope.py::TestProgressAndAncillary`
  - `tests/test_dashboard_formatters.py::TestCodexShapeRendersEmdash`
- Follow-ups: migrate `tools/analyze_transcripts.py` /
  `agentor/committer.py` / `agentor/store.aggregate_token_usage` onto
  `Envelope.from_legacy_dict` (deferred per plan).
