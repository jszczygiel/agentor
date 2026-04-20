"""Per-CLI behaviour that recovery + runner code must consult without
hardcoding a Claude substring.

A `Provider` encapsulates two concerns that differ between the Claude CLI
and the Codex CLI:

1. Dead-session detection — the set of error substrings that mean
   "resuming this persisted session id / thread id will never succeed,
   start fresh instead". Claude says `No conversation found with session
   ID ...`; Codex says `thread not found` / `thread/start failed` /
   `session not found`. Routing through the active provider keeps
   recovery from matching a Claude-only string against a Codex failure
   row (or vice-versa).

2. Wall-clock session expiry — Claude CLI sessions age out in ~5h, so
   the recovery sweep pre-emptively demotes WORKING items whose session
   is older than `agent.session_max_age_hours` rather than pay for a
   doomed `--resume`. Stub has no real session. A provider returns
   `None` here to opt out of the age gate entirely.

The module is intentionally dependency-light (imports only `Config` for a
forward ref via string annotation) so `runner` can import it at module
top without a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


class Provider:
    """Base class. Subclasses override both methods."""

    def is_dead_session_error(self, msg: str) -> bool:
        """True when the error message means the persisted session id /
        thread id is gone and the next `--resume` will always fail.

        Matches are lowercased-substring; callers may pass either the raw
        error string or the whitespace-stripped `error_sig` form (both
        `_error_signature` outputs and raw text flow through the same
        callsite in recovery)."""
        raise NotImplementedError

    def session_max_age_hours(self) -> float | None:
        """Configured max age (in hours) beyond which a persisted session
        id is assumed dead. Returning `None` disables the age gate
        entirely — recovery still honours the per-failure-row predicate
        but stops demoting purely on wall-clock age."""
        raise NotImplementedError


class ClaudeProvider(Provider):
    """Claude CLI. Sessions live ~5h and produce `No conversation found
    with session ID <uuid>` when a stale id is resumed."""

    _NEEDLES = (
        "no conversation found with session id",
    )
    _SIG_NEEDLES = tuple(n.replace(" ", "") for n in _NEEDLES)

    def __init__(self, config: "Config") -> None:
        self._config = config

    def is_dead_session_error(self, msg: str) -> bool:
        low = (msg or "").lower()
        if not low:
            return False
        return any(n in low for n in self._NEEDLES) or any(
            n in low for n in self._SIG_NEEDLES
        )

    def session_max_age_hours(self) -> float | None:
        hours = float(self._config.agent.session_max_age_hours)
        return hours if hours > 0 else None


class CodexProvider(Provider):
    """Codex CLI. Threads aren't immortal either — the CLI returns
    `thread not found` / `thread/start failed` / `session not found`
    once the backend drops a stale thread id. Max age uses the same
    generic knob as Claude: if the operator tuned it, honour it."""

    _NEEDLES = (
        "thread not found",
        "thread/start failed",
        "session not found",
    )
    _SIG_NEEDLES = tuple(n.replace(" ", "") for n in _NEEDLES)

    def __init__(self, config: "Config") -> None:
        self._config = config

    def is_dead_session_error(self, msg: str) -> bool:
        low = (msg or "").lower()
        if not low:
            return False
        return any(n in low for n in self._NEEDLES) or any(
            n in low for n in self._SIG_NEEDLES
        )

    def session_max_age_hours(self) -> float | None:
        hours = float(self._config.agent.session_max_age_hours)
        return hours if hours > 0 else None


class StubProvider(Provider):
    """Test runner — no real sessions, no wall-clock expiry, no dead-
    session signature."""

    def __init__(self, config: "Config") -> None:
        self._config = config

    def is_dead_session_error(self, msg: str) -> bool:
        return False

    def session_max_age_hours(self) -> float | None:
        return None


def make_provider(config: "Config") -> Provider:
    kind = config.agent.runner.lower()
    if kind == "stub":
        return StubProvider(config)
    if kind == "claude":
        return ClaudeProvider(config)
    if kind == "codex":
        return CodexProvider(config)
    raise ValueError(f"unknown agent.runner: {kind!r}")
