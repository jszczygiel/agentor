"""Mid-run checkpoint emitter for long agent sessions.

Watches `(num_turns, output_tokens)` as they accumulate during a stream-json
run and emits advisory nudge messages when configured thresholds are crossed.
Each threshold fires at most once per run; the emitter is purely advisory —
callers decide whether to actually inject (via stream-json stdin) or just log.
"""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_SOFT_TEMPLATE = (
    "You're at {turns} turns. If you still need to discover call sites, "
    "file locations, or test patterns, delegate that to an `Explore` or "
    "`general-purpose` subagent — its context is separate and doesn't bill "
    "against this session. Otherwise, confirm you're closing out."
)

DEFAULT_HARD_TEMPLATE = (
    "You're at {turns} turns. State in one sentence what's blocking "
    "closeout, then either finish or delegate."
)

DEFAULT_TOKENS_TEMPLATE = (
    "You've emitted {output_tokens} output tokens in this session — high "
    "for a single task. If remaining work is discovery (finding call sites, "
    "tests, enum siblings), delegate to an `Explore` or `general-purpose` "
    "subagent rather than continuing in this context. Otherwise, close out."
)


@dataclass(frozen=True)
class CheckpointConfig:
    soft_turns: int = 60
    hard_turns: int = 100
    output_tokens: int = 50_000
    soft_template: str = DEFAULT_SOFT_TEMPLATE
    hard_template: str = DEFAULT_HARD_TEMPLATE
    tokens_template: str = DEFAULT_TOKENS_TEMPLATE

    def all_disabled(self) -> bool:
        return (self.soft_turns <= 0
                and self.hard_turns <= 0
                and self.output_tokens <= 0)


class CheckpointEmitter:
    """Stateful per-run observer. Call `observe(num_turns, output_tokens)`
    after each assistant turn; it returns zero-or-more nudge strings to
    inject. Each threshold fires at most once; repeated observations after
    a fire return `[]` for that threshold."""

    def __init__(self, config: CheckpointConfig):
        self._cfg = config
        self._soft_fired = False
        self._hard_fired = False
        self._tokens_fired = False

    def observe(self, num_turns: int, output_tokens: int) -> list[str]:
        out: list[str] = []
        cfg = self._cfg
        if (not self._soft_fired and cfg.soft_turns > 0
                and num_turns >= cfg.soft_turns):
            self._soft_fired = True
            out.append(cfg.soft_template.format(
                turns=num_turns, output_tokens=output_tokens,
            ))
        if (not self._hard_fired and cfg.hard_turns > 0
                and num_turns >= cfg.hard_turns):
            self._hard_fired = True
            out.append(cfg.hard_template.format(
                turns=num_turns, output_tokens=output_tokens,
            ))
        if (not self._tokens_fired and cfg.output_tokens > 0
                and output_tokens >= cfg.output_tokens):
            self._tokens_fired = True
            out.append(cfg.tokens_template.format(
                turns=num_turns, output_tokens=output_tokens,
            ))
        return out

    @property
    def any_fired(self) -> bool:
        return self._soft_fired or self._hard_fired or self._tokens_fired
