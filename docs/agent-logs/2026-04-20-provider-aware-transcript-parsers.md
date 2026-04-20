# Provider-aware transcript parsers (primer, activity feed) — 2026-04-20

## Surprises
- `iter_raw_events(..., tail_bytes=…)` unconditionally dropped the first
  line even when the read didn't actually seek past byte 0. Harmless for
  Claude (first line is always the human-readable `args: […]` header and
  gets filtered by the `startswith("{")` check anyway), but it silently
  swallowed the first event of small Codex JSONL transcripts. Fixed by
  having `_read_maybe_tail` return `(text, seeked)` and only dropping
  line 0 when truncation really happened.
- The research subagent's survey omitted `Provider.model_aliases` /
  `model_to_alias` — I overwrote `providers.py` once without them and
  had to restore before proceeding. Cheap lesson: when a subagent
  summarises a file, diff its claimed contents against `git show HEAD:`
  before treating the summary as exhaustive.

## Gotchas for future runs
- Small-file tail-reads now preserve the first line. If a future
  transcript format *does* need a leading non-event header (the way
  Claude's `args: […]` line lives pre-`stdout:`), make sure the header
  is still non-`{…}` so the existing line filter drops it.
- `_session_activity(cfg, path)` requires `cfg.agent.runner` — test
  stubs that construct `SimpleNamespace` for Config must include
  `runner="stub"` (or any valid kind) now that the dashboard routes
  through `detect_provider`.

## Follow-ups
- Codex-side primer (`CodexProvider.build_primer`) still returns None.
  Once Codex emits tool-level granularity (or we decide to mine
  `turn.started` sequences for "what did the prior run do"), implement
  it — related ticket `docs/backlog/deduplicate-transcript-parsing.md`
  references the same cross-tool parser story.
- `tools/analyze_transcripts.py` still imports `iter_events` directly;
  it works against Claude transcripts but will silently skip Codex
  ones. Out of scope for this ticket; the dedupe-parsing backlog entry
  already tracks the follow-up.

## Stop if
- A test fails with `FileNotFoundError` on a transcript path that didn't
  exist before the change — `detect_provider` falls through to
  `make_provider(cfg)` which raises on missing `agent.runner`. The fix
  is in the test stub, not the production code.

## Outcome
- Files touched: `agentor/providers.py`, `agentor/runner.py`,
  `agentor/transcript.py`, `agentor/dashboard/transcript.py`,
  `agentor/dashboard/modes.py`, `agentor/resume_primer.py` (deleted).
- Tests added/adjusted: new `tests/test_provider_parsers.py`
  (TestCodexActivityFeed ×5, TestCodexBuildPrimer ×2,
  TestStubProviderDefaults ×2, TestDetectProvider ×5,
  TestDashboardSessionActivityProviderAware ×1);
  `tests/test_resume_primer.py` retargeted at
  `ClaudeProvider.build_primer`;
  `tests/test_transcript_tail.py` + `tests/test_dashboard_inspect_dispatch.py`
  adjusted for the new `_session_activity(cfg, path)` signature.
- Follow-ups: Codex primer (pending transcript granularity);
  `tools/analyze_transcripts.py` dedupe.
