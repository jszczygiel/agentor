"""Tests for the provider-neutral `Envelope` dataclass.

Exercises:
  * Round-trip Claude `_StreamState` → `Envelope.from_claude` →
    `to_legacy_dict` → `Envelope.from_legacy_dict`, asserting every
    counter survives the trip.
  * Codex `_CodexStreamState` symmetrical round-trip, asserting that
    unreported counters stay `None` (not `0`) on the rehydrated
    envelope.
  * On-disk key-drift guard: the set of top-level keys each provider
    emits matches a golden snapshot, so a future contributor can't
    silently rename `modelUsage` / `num_turns` etc. without a test
    failure surfacing the ripple into `aggregate_token_usage` and
    archived transcripts.
  * `_opt_int` semantics — empty flat `usage: {}` reads back as
    all-None counters; `usage: {input_tokens: 0, ...}` reads back as
    all-zero counters. That distinction is the point of the
    refactor.
"""
import unittest

from agentor.envelope import (
    Envelope,
    IterationUsage,
    ModelUsage,
    Progress,
    TokenCounters,
)
from agentor.runner import _CodexStreamState, _StreamState


# Keys each provider's `to_legacy_dict` is guaranteed to emit.
# These are the structural contract with `aggregate_token_usage`,
# archived transcripts, and `dashboard/formatters.py`.
_CLAUDE_REQUIRED_KEYS = {"usage", "iterations", "modelUsage", "num_turns"}
_CODEX_REQUIRED_KEYS = {"usage", "iterations", "modelUsage", "num_turns"}


class TestEnvelopeFromClaude(unittest.TestCase):
    """A minimal happy-path stream ingests one assistant turn + a
    terminal result event, then round-trips through the envelope."""

    def _claude_state_with_turn(self) -> _StreamState:
        state = _StreamState(item_id="item-1", phase="execute")
        state.ingest({
            "type": "system", "subtype": "init",
            "session_id": "sess-abc",
        })
        state.ingest({
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                },
            },
        })
        state.ingest({
            "type": "result", "num_turns": 1,
            "stop_reason": "end_turn",
            "duration_ms": 1234, "duration_api_ms": 1200,
            "modelUsage": {
                "claude-opus-4-7": {
                    "inputTokens": 100, "outputTokens": 50,
                    "cacheReadInputTokens": 20,
                    "cacheCreationInputTokens": 10,
                    "contextWindow": 200_000,
                },
            },
            "result": "work done",
        })
        return state

    def test_from_claude_populates_all_counters(self):
        env = Envelope.from_claude(self._claude_state_with_turn())
        self.assertEqual(env.num_turns, 1)
        self.assertEqual(env.usage.input_tokens, 100)
        self.assertEqual(env.usage.output_tokens, 50)
        self.assertEqual(env.usage.cache_read_input_tokens, 20)
        self.assertEqual(env.usage.cache_creation_input_tokens, 10)
        self.assertIsNotNone(env.iterations)
        assert env.iterations is not None  # type narrowing
        self.assertEqual(len(env.iterations), 1)
        self.assertEqual(env.iterations[0].input_tokens, 100)
        self.assertEqual(env.iterations[0].model, "claude-opus-4-7")
        self.assertEqual(env.model_usage["claude-opus-4-7"].context_window,
                         200_000)
        self.assertEqual(env.agent_ref, "sess-abc")
        self.assertEqual(env.result_text, "work done")
        self.assertEqual(env.stop_reason, "end_turn")
        self.assertEqual(env.duration_ms, 1234)
        self.assertEqual(env.duration_api_ms, 1200)

    def test_claude_roundtrips_through_legacy_dict(self):
        """`from_claude → to_legacy_dict → from_legacy_dict` must
        preserve every counter. Catches any drift between the
        producer-side camelCase write (`modelUsage[m].inputTokens`)
        and the reader-side snake_case → dataclass parse."""
        original = Envelope.from_claude(self._claude_state_with_turn())
        legacy = original.to_legacy_dict()
        rehydrated = Envelope.from_legacy_dict(legacy)

        self.assertEqual(rehydrated.num_turns, original.num_turns)
        self.assertEqual(rehydrated.usage, original.usage)
        self.assertEqual(rehydrated.model_usage, original.model_usage)
        self.assertEqual(rehydrated.iterations, original.iterations)
        self.assertEqual(rehydrated.agent_ref, original.agent_ref)
        self.assertEqual(rehydrated.result_text, original.result_text)
        self.assertEqual(rehydrated.stop_reason, original.stop_reason)
        self.assertEqual(rehydrated.duration_ms, original.duration_ms)
        self.assertEqual(rehydrated.duration_api_ms,
                         original.duration_api_ms)


class TestEnvelopeFromCodex(unittest.TestCase):

    def _codex_state_with_turn(self) -> _CodexStreamState:
        state = _CodexStreamState(item_id="item-2", phase="execute")
        state.ingest({"type": "thread.started", "thread_id": "thr-xyz"})
        state.ingest({"type": "turn.started"})
        state.ingest({"type": "agent.message", "message": "codex finished"})
        return state

    def test_codex_counters_are_none_not_zero(self):
        """Every token counter on a codex envelope is `None`, not `0`.
        That's the point of the refactor — downstream formatters need
        to distinguish "unreported" from "reported zero" so codex rows
        render `—` instead of a misleading 0%."""
        env = Envelope.from_codex(self._codex_state_with_turn())
        self.assertIsNone(env.usage.input_tokens)
        self.assertIsNone(env.usage.output_tokens)
        self.assertIsNone(env.usage.cache_read_input_tokens)
        self.assertIsNone(env.usage.cache_creation_input_tokens)
        self.assertTrue(env.usage.all_none())
        self.assertIsNone(env.iterations)
        self.assertEqual(env.model_usage, {})
        # Provider-specific fields that codex does report:
        self.assertEqual(env.num_turns, 1)
        self.assertEqual(env.agent_ref, "thr-xyz")
        self.assertEqual(env.result_text, "codex finished")

    def test_codex_roundtrips_through_legacy_dict(self):
        """Codex round-trip: every counter stays `None` after the
        legacy-dict trip, because `usage == {}` is preserved and
        `from_legacy_dict` maps an empty dict to all-None counters
        (the explicit codex sentinel)."""
        original = Envelope.from_codex(self._codex_state_with_turn())
        legacy = original.to_legacy_dict()
        rehydrated = Envelope.from_legacy_dict(legacy)

        self.assertTrue(rehydrated.usage.all_none())
        # Codex legacy shape has `iterations: []` on disk; readers
        # interpret this same as the producer intended (no per-turn
        # data). `iterations is None` is specifically the
        # "provider declared nothing" marker on the writer side; on
        # read we accept both `None` and `[]` as semantically empty.
        self.assertIn(rehydrated.iterations, ([], None))
        self.assertEqual(rehydrated.model_usage, {})
        self.assertEqual(rehydrated.num_turns, original.num_turns)
        self.assertEqual(rehydrated.agent_ref, original.agent_ref)
        self.assertEqual(rehydrated.result_text, original.result_text)


class TestLegacyDictKeyDrift(unittest.TestCase):
    """Golden-snapshot keyset guards. Every downstream reader
    (`Store.aggregate_token_usage`, `dashboard/formatters.py`,
    archived transcripts on disk) indexes by these exact key names;
    renaming any of them is a breaking change that this test catches
    at lint-speed instead of after a production run."""

    def test_claude_emits_required_top_level_keys(self):
        state = _StreamState(item_id="i", phase="execute")
        state.ingest({
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 1, "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        })
        legacy = Envelope.from_claude(state).to_legacy_dict()
        self.assertTrue(_CLAUDE_REQUIRED_KEYS.issubset(legacy.keys()))
        # modelUsage per-entry keys are also a contract.
        entry = legacy["modelUsage"]["claude-opus-4-7"]
        self.assertEqual(set(entry.keys()), {
            "inputTokens", "outputTokens",
            "cacheReadInputTokens", "cacheCreationInputTokens",
            "contextWindow",
        })

    def test_codex_emits_required_top_level_keys(self):
        state = _CodexStreamState(item_id="i", phase="execute")
        state.ingest({"type": "thread.started", "thread_id": "t"})
        state.ingest({"type": "turn.started"})
        legacy = Envelope.from_codex(state).to_legacy_dict()
        self.assertTrue(_CODEX_REQUIRED_KEYS.issubset(legacy.keys()))
        # Legacy placeholders: empty dict/list, not omitted.
        self.assertEqual(legacy["usage"], {})
        self.assertEqual(legacy["iterations"], [])
        self.assertEqual(legacy["modelUsage"], {})

    def test_claude_and_codex_share_base_keyset(self):
        """Structural symmetry: both providers write the same top-
        level envelope keys. Provider differences live INSIDE the
        values (populated vs empty), not in which keys exist."""
        claude_state = _StreamState(item_id="i", phase="execute")
        codex_state = _CodexStreamState(item_id="i", phase="execute")
        claude_keys = set(Envelope.from_claude(claude_state)
                          .to_legacy_dict().keys())
        codex_keys = set(Envelope.from_codex(codex_state)
                         .to_legacy_dict().keys())
        base = _CLAUDE_REQUIRED_KEYS
        self.assertTrue(base.issubset(claude_keys))
        self.assertTrue(base.issubset(codex_keys))


class TestFromLegacyDictSemantics(unittest.TestCase):
    """The reader side's empty-vs-zero discriminator is the whole
    point of the refactor. These tests pin it."""

    def test_empty_usage_maps_to_none_counters(self):
        env = Envelope.from_legacy_dict({
            "usage": {},
            "iterations": [],
            "modelUsage": {},
            "num_turns": 0,
        })
        self.assertTrue(env.usage.all_none())

    def test_zero_usage_maps_to_zero_counters(self):
        env = Envelope.from_legacy_dict({
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "iterations": [],
            "modelUsage": {},
            "num_turns": 0,
        })
        self.assertFalse(env.usage.all_none())
        self.assertEqual(env.usage.input_tokens, 0)
        self.assertEqual(env.usage.output_tokens, 0)

    def test_missing_iterations_yields_none(self):
        """A legacy claude blob predating the envelope may omit the
        `iterations` key entirely; the reader treats that as
        'not reported', which is the same signal codex produces."""
        env = Envelope.from_legacy_dict({"usage": {"input_tokens": 10}})
        self.assertIsNone(env.iterations)

    def test_non_dict_input_yields_empty_envelope(self):
        self.assertEqual(Envelope.from_legacy_dict(None).usage,
                         TokenCounters())
        self.assertEqual(Envelope.from_legacy_dict("nonsense").usage,  # type: ignore[arg-type]
                         TokenCounters())

    def test_legacy_session_id_blob_falls_back_to_agent_ref(self):
        """`result_json` blobs written before the session_id → agent_ref
        rename carry the old key name. `from_legacy_dict` must still
        surface them under `agent_ref` so stale rows keep rendering
        correctly in the dashboard."""
        env = Envelope.from_legacy_dict({
            "usage": {},
            "iterations": [],
            "modelUsage": {},
            "num_turns": 0,
            "session_id": "legacy-sess",
        })
        self.assertEqual(env.agent_ref, "legacy-sess")

    def test_new_agent_ref_blob_preferred_over_legacy_session_id(self):
        """If both keys happen to be present (unexpected, but cheap to
        pin), the new `agent_ref` wins."""
        env = Envelope.from_legacy_dict({
            "usage": {},
            "iterations": [],
            "modelUsage": {},
            "num_turns": 0,
            "agent_ref": "new-ref",
            "session_id": "legacy-sess",
        })
        self.assertEqual(env.agent_ref, "new-ref")

    def test_model_usage_entries_round_trip(self):
        original = {
            "usage": {"input_tokens": 5, "output_tokens": 5,
                       "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0},
            "iterations": [],
            "modelUsage": {
                "claude-haiku": {
                    "inputTokens": 5, "outputTokens": 5,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "contextWindow": 200_000,
                },
            },
            "num_turns": 1,
        }
        env = Envelope.from_legacy_dict(original)
        self.assertIn("claude-haiku", env.model_usage)
        mu = env.model_usage["claude-haiku"]
        self.assertEqual(mu.input_tokens, 5)
        self.assertEqual(mu.context_window, 200_000)
        # to_legacy_dict should reproduce the modelUsage dict exactly.
        again = env.to_legacy_dict()["modelUsage"]["claude-haiku"]
        self.assertEqual(again, original["modelUsage"]["claude-haiku"])


class TestProgressAndAncillary(unittest.TestCase):
    """Progress / agent_ref / stop_reason handling. Preserves the
    existing legacy envelope's optional-key gating (progress dict is
    omitted entirely when empty; stop_reason / durations only appear
    when set)."""

    def test_progress_absent_when_state_empty(self):
        env = Envelope(progress=Progress())
        self.assertNotIn("progress", env.to_legacy_dict())

    def test_progress_populated_when_state_set(self):
        env = Envelope(progress=Progress(
            last_event_at=1.0, last_event_type="assistant",
            activity="turn 3 finished",
        ))
        out = env.to_legacy_dict()
        self.assertEqual(out["progress"], {
            "last_event_at": 1.0,
            "last_event_type": "assistant",
            "activity": "turn 3 finished",
        })

    def test_optional_keys_omitted_when_unset(self):
        env = Envelope()
        out = env.to_legacy_dict()
        for optional in ("stop_reason", "duration_ms", "duration_api_ms",
                          "agent_ref", "result", "rate_limits"):
            self.assertNotIn(optional, out)


if __name__ == "__main__":
    unittest.main()
