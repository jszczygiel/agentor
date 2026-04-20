"""Unified streaming-usage envelope.

Each provider's stream accumulator (`_StreamState` for claude,
`_CodexStreamState` for codex) builds one of these, then serialises
through `Envelope.to_legacy_dict()` into the same on-disk JSON shape
the rest of the codebase reads from `items.result_json`. Explicit
`int | None` counters let readers distinguish "provider didn't report
this metric" from "provider reported zero" — codex fills almost no
counters, claude fills them all — so dashboard formatters can render
`—` instead of a misleading `0%` on codex rows.

The on-disk shape is deliberately preserved byte-for-byte
(`aggregate_token_usage`, the 2s token-windows cache, any archived
transcripts all read the legacy keys directly). Readers migrate
incrementally: the current round moves `dashboard/formatters.py` onto
`from_legacy_dict`; `tools/analyze_transcripts.py` and
`agentor/committer.py` stay on the raw dict and are queued for a
follow-up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids cycle
    from .runner import _CodexStreamState, _StreamState


_COUNTER_KEYS_FLAT = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_COUNTER_KEYS_MODEL = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)


def _opt_int(v: Any) -> int | None:
    """Coerce to int, preserving None. Non-int values and non-dict
    parents bubble up as None so `from_legacy_dict` stays total."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class TokenCounters:
    """Flat per-phase / per-iteration token counts. Every counter is
    `int | None` so "unreported" is distinct from 0."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None

    def all_none(self) -> bool:
        return (
            self.input_tokens is None
            and self.output_tokens is None
            and self.cache_read_input_tokens is None
            and self.cache_creation_input_tokens is None
        )

    def sum_reported(self) -> int:
        """Sum counters that are not `None` (None is skipped, not 0).
        Used by formatters that want a total while still being able
        to detect "nothing reported" via `all_none()`."""
        return sum(
            v for v in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_input_tokens,
                self.cache_creation_input_tokens,
            ) if v is not None
        )

    def to_flat_dict(self) -> dict[str, int]:
        """Claude's legacy flat `usage` dict: all four keys, 0 for
        None. Claude always computes this from the sum of iterations
        (0 when no turns). Codex omits the dict entirely in its
        legacy shape, so this serialisation is Claude-only."""
        return {
            "input_tokens": self.input_tokens or 0,
            "output_tokens": self.output_tokens or 0,
            "cache_read_input_tokens": self.cache_read_input_tokens or 0,
            "cache_creation_input_tokens": self.cache_creation_input_tokens or 0,
        }


@dataclass
class IterationUsage:
    """Per-assistant-turn usage, as claude emits it. Codex doesn't
    emit per-turn usage — its iterations list stays empty."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    model: str | None = None

    def to_legacy_dict(self) -> dict[str, Any]:
        """Mirror the dict shape that _StreamState currently appends
        to `iterations` (flat int zeros, not None — preserves on-disk
        compatibility)."""
        out: dict[str, Any] = {
            "input_tokens": self.input_tokens or 0,
            "output_tokens": self.output_tokens or 0,
            "cache_read_input_tokens": self.cache_read_input_tokens or 0,
            "cache_creation_input_tokens": self.cache_creation_input_tokens or 0,
        }
        if self.model is not None:
            out["model"] = self.model
        return out

    @classmethod
    def from_legacy_dict(cls, data: dict | None) -> "IterationUsage":
        if not isinstance(data, dict):
            return cls()
        model = data.get("model")
        return cls(
            input_tokens=_opt_int(data.get("input_tokens")),
            output_tokens=_opt_int(data.get("output_tokens")),
            cache_read_input_tokens=_opt_int(data.get("cache_read_input_tokens")),
            cache_creation_input_tokens=_opt_int(
                data.get("cache_creation_input_tokens")),
            model=str(model) if isinstance(model, str) else None,
        )


@dataclass
class ModelUsage:
    """Per-model rollup, as claude reports in the terminal `result`
    event's `modelUsage`. `context_window` is claude-only (codex
    doesn't expose it — see `ProviderCapabilities.reports_context_window`)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    context_window: int | None = None

    def all_counters_none(self) -> bool:
        return (
            self.input_tokens is None
            and self.output_tokens is None
            and self.cache_read_input_tokens is None
            and self.cache_creation_input_tokens is None
        )

    def sum_reported(self) -> int:
        return sum(
            v for v in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_input_tokens,
                self.cache_creation_input_tokens,
            ) if v is not None
        )

    def to_legacy_dict(self) -> dict[str, int]:
        """camelCase on-disk shape used by `modelUsage[m]`. Claude
        computes these as ints (0 when no turns); preserve that."""
        return {
            "inputTokens": self.input_tokens or 0,
            "outputTokens": self.output_tokens or 0,
            "cacheReadInputTokens": self.cache_read_input_tokens or 0,
            "cacheCreationInputTokens": self.cache_creation_input_tokens or 0,
            "contextWindow": self.context_window or 0,
        }

    @classmethod
    def from_legacy_dict(cls, data: dict | None) -> "ModelUsage":
        if not isinstance(data, dict):
            return cls()
        return cls(
            input_tokens=_opt_int(data.get("inputTokens")),
            output_tokens=_opt_int(data.get("outputTokens")),
            cache_read_input_tokens=_opt_int(data.get("cacheReadInputTokens")),
            cache_creation_input_tokens=_opt_int(
                data.get("cacheCreationInputTokens")),
            context_window=_opt_int(data.get("contextWindow")),
        )


@dataclass
class Progress:
    """Small progress heartbeat carried on every envelope for the
    dashboard's live-activity line."""

    last_event_at: float | None = None
    last_event_type: str | None = None
    activity: str | None = None

    def is_empty(self) -> bool:
        return (
            self.last_event_at is None
            and self.last_event_type is None
            and self.activity is None
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.last_event_at is not None:
            out["last_event_at"] = self.last_event_at
        if self.last_event_type is not None:
            out["last_event_type"] = self.last_event_type
        if self.activity is not None:
            out["activity"] = self.activity
        return out

    @classmethod
    def from_legacy_dict(cls, data: dict | None) -> "Progress":
        if not isinstance(data, dict):
            return cls()
        last_at = data.get("last_event_at")
        last_type = data.get("last_event_type")
        activity = data.get("activity")
        return cls(
            last_event_at=(
                float(last_at) if isinstance(last_at, (int, float)) else None),
            last_event_type=str(last_type) if isinstance(last_type, str) else None,
            activity=str(activity) if isinstance(activity, str) else None,
        )


@dataclass
class Envelope:
    """Provider-neutral streaming-usage summary. `None` counters mean
    "not reported"; `0` means "reported as zero".

    `iterations is None` specifically encodes "provider doesn't emit
    per-turn usage" — codex. Claude always emits a list (possibly
    empty on a run that never got past `system.init`), so claude sets
    an empty list, not None."""

    # --- provider-agnostic ---
    num_turns: int | None = None
    usage: TokenCounters = field(default_factory=TokenCounters)
    iterations: list[IterationUsage] | None = None
    model_usage: dict[str, ModelUsage] = field(default_factory=dict)
    progress: Progress = field(default_factory=Progress)
    session_id: str | None = None
    result_text: str | None = None
    stop_reason: str | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    rate_limits: dict | None = None

    # ------------------------------------------------------------------
    # Producers
    # ------------------------------------------------------------------

    @classmethod
    def from_claude(cls, state: "_StreamState") -> "Envelope":
        """Capture the current accumulator snapshot into an `Envelope`.
        Claude fills every counter, so `usage` is the sum of each
        iteration's counters and `model_usage` is the CLI's
        authoritative per-model rollup (preferred over the locally-
        aggregated map whenever the terminal `result` event has
        overwritten it)."""
        iterations: list[IterationUsage] = []
        for turn in state.iterations:
            if not isinstance(turn, dict):
                continue
            iterations.append(IterationUsage(
                input_tokens=_opt_int(turn.get("input_tokens")) or 0,
                output_tokens=_opt_int(turn.get("output_tokens")) or 0,
                cache_read_input_tokens=(
                    _opt_int(turn.get("cache_read_input_tokens")) or 0),
                cache_creation_input_tokens=(
                    _opt_int(turn.get("cache_creation_input_tokens")) or 0),
                model=(turn.get("model")
                       if isinstance(turn.get("model"), str) else None),
            ))
        usage = TokenCounters(
            input_tokens=sum(
                (i.input_tokens or 0) for i in iterations),
            output_tokens=sum(
                (i.output_tokens or 0) for i in iterations),
            cache_read_input_tokens=sum(
                (i.cache_read_input_tokens or 0) for i in iterations),
            cache_creation_input_tokens=sum(
                (i.cache_creation_input_tokens or 0) for i in iterations),
        )
        model_usage: dict[str, ModelUsage] = {}
        for model, mu in (state.model_usage or {}).items():
            if not isinstance(mu, dict):
                continue
            model_usage[model] = ModelUsage(
                input_tokens=_opt_int(mu.get("inputTokens")) or 0,
                output_tokens=_opt_int(mu.get("outputTokens")) or 0,
                cache_read_input_tokens=(
                    _opt_int(mu.get("cacheReadInputTokens")) or 0),
                cache_creation_input_tokens=(
                    _opt_int(mu.get("cacheCreationInputTokens")) or 0),
                context_window=_opt_int(mu.get("contextWindow")) or 0,
            )
        progress = Progress(
            last_event_at=state.last_event_at,
            last_event_type=state.last_event_type,
            activity=state.activity,
        )
        return cls(
            num_turns=int(state.num_turns),
            usage=usage,
            iterations=iterations,
            model_usage=model_usage,
            progress=progress,
            session_id=state.session_id,
            result_text=state.result_text,
            stop_reason=state.stop_reason,
            duration_ms=state.duration_ms,
            duration_api_ms=state.duration_api_ms,
            rate_limits=state.rate_limits,
        )

    @classmethod
    def from_codex(
        cls, state: "_CodexStreamState", *, result_text: str | None = None,
    ) -> "Envelope":
        """Codex reports only `num_turns`, `session_id`, a final
        message, and progress. Every token counter stays None;
        `iterations` stays None (codex has no per-turn usage — the
        envelope encodes that explicitly rather than emitting an
        empty list); `model_usage` stays empty.

        `result_text` override mirrors `_CodexStreamState.envelope`'s
        existing kwarg: the caller in `_invoke_codex_jsonl` reads the
        final message from the `--output-path` file and passes it in
        at shutdown; otherwise the last-seen message from the stream
        is used."""
        progress = Progress(
            last_event_at=state.last_event_at,
            last_event_type=state.last_event_type,
            activity=state.activity,
        )
        return cls(
            num_turns=int(state.num_turns),
            usage=TokenCounters(),
            iterations=None,
            model_usage={},
            progress=progress,
            session_id=state.session_id,
            result_text=result_text or state.result_text,
            stop_reason=state.last_error,
        )

    @classmethod
    def from_legacy_dict(cls, data: dict | None) -> "Envelope":
        """Rehydrate from a dict decoded out of `items.result_json`.

        Key presence disambiguates provider shape:
          * `usage == {}` (codex) → `TokenCounters` all-None.
          * `usage` with any flat keys (claude) → populated counters.
          * `iterations` key absent → `iterations is None` (codex).
          * `iterations == []` (claude pre-any-turn) → empty list.

        The on-disk shape has remained the same since pre-envelope —
        the daemon lifecycle overwrites the blob on every phase
        transition, so historical rows in a long-running project may
        predate this logic and still round-trip correctly.
        """
        if not isinstance(data, dict):
            return cls()

        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict) and raw_usage:
            usage = TokenCounters(
                input_tokens=_opt_int(raw_usage.get("input_tokens")),
                output_tokens=_opt_int(raw_usage.get("output_tokens")),
                cache_read_input_tokens=_opt_int(
                    raw_usage.get("cache_read_input_tokens")),
                cache_creation_input_tokens=_opt_int(
                    raw_usage.get("cache_creation_input_tokens")),
            )
        else:
            # Missing or empty `{}` — provider didn't report flat usage.
            usage = TokenCounters()

        if "iterations" in data:
            raw_iters = data.get("iterations")
            if isinstance(raw_iters, list):
                iterations: list[IterationUsage] | None = [
                    IterationUsage.from_legacy_dict(t) for t in raw_iters
                    if isinstance(t, dict)
                ]
            else:
                iterations = None
        else:
            iterations = None

        model_usage: dict[str, ModelUsage] = {}
        raw_mu = data.get("modelUsage")
        if isinstance(raw_mu, dict):
            for k, v in raw_mu.items():
                if isinstance(v, dict):
                    model_usage[str(k)] = ModelUsage.from_legacy_dict(v)

        return cls(
            num_turns=_opt_int(data.get("num_turns")),
            usage=usage,
            iterations=iterations,
            model_usage=model_usage,
            progress=Progress.from_legacy_dict(data.get("progress")),
            session_id=(data.get("session_id")
                        if isinstance(data.get("session_id"), str) else None),
            result_text=(data.get("result")
                         if isinstance(data.get("result"), str) else None),
            stop_reason=(data.get("stop_reason")
                         if isinstance(data.get("stop_reason"), str) else None),
            duration_ms=_opt_int(data.get("duration_ms")),
            duration_api_ms=_opt_int(data.get("duration_api_ms")),
            rate_limits=(data.get("rate_limits")
                         if isinstance(data.get("rate_limits"), dict) else None),
        )

    # ------------------------------------------------------------------
    # Serialiser
    # ------------------------------------------------------------------

    def to_legacy_dict(self) -> dict[str, Any]:
        """Reproduce the exact on-disk shape both `_StreamState.envelope`
        and `_CodexStreamState.envelope` produced before the
        refactor. Presence/absence of keys, 0-vs-missing behaviour,
        and dict ordering all matter — downstream readers
        (`aggregate_token_usage`, the dashboard, archived transcripts)
        all index by legacy keys directly.

        Shape rules:
          * `iterations is None` → omit `usage`/`iterations`/
            `modelUsage` to `{}` / `[]` / `{}` (codex shape).
          * `iterations is list` → claude shape: `usage` flat ints,
            `iterations` list of dicts, `modelUsage` dict.
          * Optional keys (`stop_reason`, `duration_ms`,
            `duration_api_ms`, `session_id`, `result`, `rate_limits`)
            appear only when their underlying value is truthy — same
            gating both state classes used before.
        """
        out: dict[str, Any] = {}

        if self.iterations is None:
            # Codex shape: preserve the legacy placeholders so the rest
            # of the codebase's `data.get("usage")` / etc. still gets
            # the empty containers it expects.
            out["usage"] = {}
            out["iterations"] = []
            out["modelUsage"] = {}
        else:
            out["usage"] = self.usage.to_flat_dict()
            out["iterations"] = [i.to_legacy_dict() for i in self.iterations]
            out["modelUsage"] = {
                model: mu.to_legacy_dict()
                for model, mu in self.model_usage.items()
            }

        out["num_turns"] = int(self.num_turns or 0)

        if self.stop_reason:
            out["stop_reason"] = self.stop_reason
        if self.duration_ms is not None:
            out["duration_ms"] = int(self.duration_ms)
        if self.duration_api_ms is not None:
            out["duration_api_ms"] = int(self.duration_api_ms)
        if self.session_id:
            out["session_id"] = self.session_id
        if self.result_text:
            out["result"] = self.result_text
        if self.rate_limits:
            out["rate_limits"] = self.rate_limits

        progress = self.progress.to_legacy_dict()
        if progress:
            out["progress"] = progress

        return out
