"""Declarative provider capabilities.

Runners previously encoded provider differences inline — passing
`stdin_holder` to signal mid-run injection support, inspecting
`modelUsage` shape to infer context-window reporting, etc. Collecting
those flags into a `ProviderCapabilities` dataclass lets callers
(`_invoke_claude_streaming`, `_invoke_codex_jsonl`, dashboard
formatters) consult a single declared source of truth and lets a third
provider declare its behaviour once instead of threading new branches
through every consumer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ResultSource = Literal["stdout_json", "output_file"]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Static, per-provider declaration of what the backing CLI supports.

    Bound as a class attribute on each `Runner` subclass; looked up by
    `capabilities_for(runner_name)` for consumers that only know the
    string from `agent.runner`.
    """

    supports_mid_run_injection: bool
    """Whether the provider accepts mid-stream user messages (e.g.
    claude's `--input-format stream-json` stdin pipe). When False, the
    checkpoint emitter still runs as a passive observer but nudges are
    only recorded as transcript markers, never injected."""

    reports_context_window: bool
    """Whether the provider emits a per-model `contextWindow` field
    (claude does via `modelUsage[m].contextWindow`; codex emits an
    empty `modelUsage`). Dashboard formatters use this to decide
    whether to render the CTX% column or a placeholder."""

    reports_output_tokens_per_turn: bool
    """Whether the provider emits per-turn `output_tokens` usable by
    the token-checkpoint threshold. Claude does; codex does not, so its
    token threshold stays dormant regardless of configured value."""

    result_source: ResultSource
    """How the final result_text is extracted — claude parses the
    terminal `result` event from stdout (`stdout_json`); codex writes
    it to the `--output-path` file (`output_file`). Stub runners have
    no result channel but re-use `stdout_json` to keep the union
    narrow per the ticket spec."""

    requires_explicit_session_arg: bool
    """Whether a session id must be passed as a separate CLI flag
    (claude: `--session-id <id>` on first run, `--resume <id>` on
    resume). Codex baked session handling into a dedicated resume
    command template, so this is False there. Declared for the
    sibling `Provider` interface refactor; not yet consumed at
    construction time."""

    resume_arg_name: str | None
    """The flag name used for resume when `requires_explicit_session_arg`
    is True (claude: `--resume`). None when the provider uses a
    dedicated `resume_command` template instead of an inline flag.
    Declared for the sibling `Provider` interface refactor."""


CLAUDE_CAPS = ProviderCapabilities(
    supports_mid_run_injection=True,
    reports_context_window=True,
    reports_output_tokens_per_turn=True,
    result_source="stdout_json",
    requires_explicit_session_arg=True,
    resume_arg_name="--resume",
)

CODEX_CAPS = ProviderCapabilities(
    supports_mid_run_injection=False,
    reports_context_window=False,
    reports_output_tokens_per_turn=False,
    result_source="output_file",
    requires_explicit_session_arg=False,
    resume_arg_name=None,
)

STUB_CAPS = ProviderCapabilities(
    supports_mid_run_injection=False,
    reports_context_window=False,
    reports_output_tokens_per_turn=False,
    result_source="stdout_json",
    requires_explicit_session_arg=False,
    resume_arg_name=None,
)


_BY_NAME: dict[str, ProviderCapabilities] = {
    "stub": STUB_CAPS,
    "claude": CLAUDE_CAPS,
    "codex": CODEX_CAPS,
}


def capabilities_for(runner_name: str) -> ProviderCapabilities:
    """Look up capabilities by runner kind. Mirrors `make_runner`'s
    lowered-name switch so any valid `agent.runner` value resolves."""
    kind = (runner_name or "").lower()
    try:
        return _BY_NAME[kind]
    except KeyError:
        raise ValueError(f"unknown agent.runner: {runner_name!r}")
