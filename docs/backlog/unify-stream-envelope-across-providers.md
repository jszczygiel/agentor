---
title: Unify streaming-usage envelope across providers
state: available
tags: [refactor, multi-provider, dashboard]
---

`_StreamState` (`runner.py:1377`) and `_CodexStreamState` (`:1529`) are
parallel accumulators emitting overlapping-but-unequal dicts keyed the
same (`usage`, `iterations`, `modelUsage`, `num_turns`, `progress`).
Claude fills them; Codex leaves them mostly empty (`usage: {}`,
`iterations: []`, `modelUsage: {}`). Dashboard consumers in
`dashboard/formatters.py:118+` are coded against the Claude shape —
context-window %, per-iteration breakdown, cache-read accounting — and
silently degrade on Codex rows.

Introduce an `Envelope` dataclass with explicit optionality: every
numeric counter is `int | None`, so a provider that doesn't report the
metric is distinguishable from one that reports zero. Add
`Envelope.from_claude(state)` / `Envelope.from_codex(state)` factory
methods; formatters render `—` for `None` instead of computing against
zero.

Keep `result_json` JSON-on-disk shape stable by writing
`envelope.to_legacy_dict()`. Migrate readers incrementally —
`dashboard/formatters.py` first (it's where the degradation is
user-visible), then `tools/analyze_transcripts.py`, then committer.

Acceptance: Codex items show `—` for unreported metrics instead of `0%`
/ `0 in`, and a provider-agnostic unit test round-trips both envelopes
through the legacy dict without key drift.
