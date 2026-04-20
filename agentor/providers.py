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

import re
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .config import Config


class Provider:
    """Base class. Subclasses override both methods."""

    # Short alias → current-best model id for this CLI. Rotated in lockstep
    # with the vendor's releases. `execute_model_whitelist` in AgentConfig
    # defaults to `[]` meaning "this map's keys" — keep the default path
    # honest by populating the map on every concrete subclass. Empty maps
    # disable the `@model:` tag / plan-nomination channel for that provider.
    model_aliases: ClassVar[dict[str, str]] = {}

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

    def model_to_alias(self, model_id: str) -> str | None:
        """Reverse lookup: map a full model id back to its short alias.
        Default is exact-match against `model_aliases`; subclasses that
        want a prefix fallback (e.g. `claude-opus-4-6` → `opus` even when
        the map has rotated to `claude-opus-4-7`) override."""
        if not model_id:
            return None
        for alias, mid in self.model_aliases.items():
            if mid == model_id:
                return alias
        return None


class ClaudeProvider(Provider):
    """Claude CLI. Sessions live ~5h and produce `No conversation found
    with session ID <uuid>` when a stale id is resumed."""

    _NEEDLES = (
        "no conversation found with session id",
    )
    _SIG_NEEDLES = tuple(n.replace(" ", "") for n in _NEEDLES)

    # Rotated in lockstep with Anthropic releases.
    model_aliases: ClassVar[dict[str, str]] = {
        "haiku": "claude-haiku-4-5",
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-7",
    }

    _ALIAS_PREFIX_RE = re.compile(r"^claude-(haiku|sonnet|opus)\b")

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

    def model_to_alias(self, model_id: str) -> str | None:
        # Prefix fallback so e.g. `claude-opus-4-6` still resolves to
        # `opus` when `model_aliases["opus"]` has rotated to a newer id.
        exact = super().model_to_alias(model_id)
        if exact is not None:
            return exact
        m = self._ALIAS_PREFIX_RE.match(model_id or "")
        return m.group(1) if m else None


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

    # Size-tier aliases over OpenAI's current flagships. Distinct from
    # Claude's `haiku/sonnet/opus` vocabulary — `@model:haiku` on a
    # Codex-routed item correctly falls through to the default with a
    # soft warning instead of silently pinning a Claude id.
    model_aliases: ClassVar[dict[str, str]] = {
        "mini": "gpt-5-mini",
        "full": "gpt-5",
    }

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

    # Mirror Claude's aliases so `runner="stub"` tests that expected the
    # old global `_ALIAS_TO_MODEL` continue to resolve `haiku/sonnet/opus`
    # without needing to pin `runner="claude"`.
    model_aliases: ClassVar[dict[str, str]] = dict(ClaudeProvider.model_aliases)

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
