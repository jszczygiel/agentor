import json
import time

from ..capabilities import CLAUDE_CAPS, ProviderCapabilities
from ..models import ItemStatus
from ..store import Store, StoredItem


# Table column layout. The TITLE column gets whatever width remains.
_COL_ID = 10      # 8 chars + 2 pad
_COL_STATE = 18   # widest status name + pad
_COL_ELAPSED = 9
_COL_CTX = 6      # "100%  " — last-turn context fill vs window


def _fmt_elapsed(sec: float | None) -> str:
    if sec is None:
        return "—:—"
    m, s = divmod(int(sec), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_relative_age(sec: float | None) -> str:
    if sec is None:
        return "—"
    if sec < 1:
        return "just now"
    if sec < 60:
        return f"{int(sec)}s ago"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m {s:02d}s ago"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m ago"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _one_line(text: str, width: int) -> str:
    s = " ".join((text or "").split())
    return s[: width - 1] + "…" if len(s) > width else s


def _elapsed_for(store: Store, item_id: str) -> float | None:
    """Seconds since the most recent transition INTO `working` for this item."""
    at = store.latest_transition_at(item_id, ItemStatus.WORKING)
    if at is None:
        return None
    return max(0.0, time.time() - at)


def _result_data(item: StoredItem) -> dict | None:
    if not item.result_json:
        return None
    try:
        return json.loads(item.result_json)
    except json.JSONDecodeError:
        return None


def _progress_data(item: StoredItem) -> dict:
    data = _result_data(item) or {}
    progress = data.get("progress")
    return progress if isinstance(progress, dict) else {}


def _phase_for(item: StoredItem) -> str | None:
    data = _result_data(item) or {}
    phase = data.get("phase")
    return phase if isinstance(phase, str) and phase else None


def _tokens_for_model(mu_entry: dict) -> int:
    """Total billed tokens for one modelUsage row (input + cache rd/wr + out)."""
    if not isinstance(mu_entry, dict):
        return 0
    return (int(mu_entry.get("inputTokens", 0) or 0)
            + int(mu_entry.get("cacheReadInputTokens", 0) or 0)
            + int(mu_entry.get("cacheCreationInputTokens", 0) or 0)
            + int(mu_entry.get("outputTokens", 0) or 0))


def _tokens_total(item: StoredItem) -> str:
    """Total billed tokens across all models used in the run, formatted
    compactly (1.5M / 120k). Shown in the main dashboard column."""
    data = _result_data(item)
    if not data:
        return "—"
    mu = data.get("modelUsage")
    total = 0
    if isinstance(mu, dict) and mu:
        total = sum(_tokens_for_model(v) for v in mu.values())
    if not total:
        # Fall back to the top-level `usage` dict (older result_json shape).
        usage = data.get("usage")
        if isinstance(usage, dict):
            total = sum(int(usage.get(k, 0) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens",
            ))
    if not total:
        return "—"
    return _fmt_tokens(total)


def _ctx_fill_pct(
    item: StoredItem, fallback_window: int,
    caps: ProviderCapabilities = CLAUDE_CAPS,
) -> str:
    """Approximate how full the main agent's context was on its last turn,
    as a percent of its context window.

    Honest formula: the `iterations` array in claude's JSON result is per-
    turn. On the LAST turn, `input_tokens + cache_read_input_tokens` is the
    total tokens the model had to read — i.e. how full the working context
    was. Summing across turns is the cumulative spend, a different number.

    Window is read from the largest `contextWindow` in `modelUsage` (which
    claude reports — 1M for the opus-4-6 1M variant, 200k for standard
    opus) to avoid a stale config default. Falls back to `fallback_window`.

    `caps.reports_context_window` short-circuits to `—` when the provider
    doesn't emit `modelUsage[m].contextWindow` (codex) — the existing
    logic would fall through to the same answer via the empty-modelUsage
    path, but declaring the gate keeps the intent explicit and avoids the
    iteration scan entirely."""
    if not caps.reports_context_window:
        return "—"
    data = _result_data(item)
    if not data:
        return "—"
    # Pick the biggest reported window across models — that's the main
    # agent's, not a small sub-agent (haiku runs with a 200k window even when
    # the orchestrator has 1M).
    window = fallback_window
    mu = data.get("modelUsage")
    if isinstance(mu, dict):
        reported = [int(v.get("contextWindow", 0) or 0) for v in mu.values()
                    if isinstance(v, dict)]
        if reported:
            window = max(window, max(reported))
    iters = data.get("iterations")
    last_turn_tokens = 0
    observed_max = 0
    if isinstance(iters, list) and iters:
        for turn in iters:
            if not isinstance(turn, dict):
                continue
            t = (int(turn.get("input_tokens", 0) or 0)
                 + int(turn.get("cache_read_input_tokens", 0) or 0)
                 + int(turn.get("cache_creation_input_tokens", 0) or 0))
            observed_max = max(observed_max, t)
        last = iters[-1]
        if isinstance(last, dict):
            last_turn_tokens = (
                int(last.get("input_tokens", 0) or 0)
                + int(last.get("cache_read_input_tokens", 0) or 0)
                + int(last.get("cache_creation_input_tokens", 0) or 0)
            )
    # Live streams don't populate modelUsage.contextWindow until the terminal
    # 'result' event. If any turn's working set already exceeded our window
    # estimate, the model must be on a larger variant — bump accordingly.
    if observed_max > window:
        window = 1_000_000 if observed_max > 200_000 else 200_000
    if window <= 0:
        return "—"
    if not last_turn_tokens:
        # No per-turn data — approximate with input+cache_create from the
        # flat usage block (summed cache_read would balloon past the window,
        # so exclude it).
        usage = data.get("usage")
        if isinstance(usage, dict):
            last_turn_tokens = (
                int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("cache_creation_input_tokens", 0) or 0)
            )
    if not last_turn_tokens:
        return "—"
    pct = 100.0 * last_turn_tokens / window
    return f"{int(round(pct))}%"


def _tokens_split(item: StoredItem) -> str:
    """Compact per-model split like 'O:1.5M H:210k'. Labels are single-letter
    family hints (O=opus, S=sonnet, H=haiku); unknown families fall back to
    the first 3 chars of the model id. Returns '' if no modelUsage recorded."""
    data = _result_data(item)
    if not data:
        return ""
    mu = data.get("modelUsage")
    if not isinstance(mu, dict) or not mu:
        return ""
    parts: list[tuple[str, int]] = []
    for model, v in mu.items():
        n = _tokens_for_model(v)
        if n <= 0:
            continue
        name = model.lower()
        if "opus" in name:
            tag = "O"
        elif "sonnet" in name:
            tag = "S"
        elif "haiku" in name:
            tag = "H"
        else:
            tag = model[:3]
        parts.append((tag, n))
    parts.sort(key=lambda p: -p[1])
    return " ".join(f"{tag}:{_fmt_tokens(n)}" for tag, n in parts)


def _token_breakdown(item: StoredItem) -> list[dict]:
    """Per-model token breakdown, sorted by total tokens descending.
    Returns empty list if unavailable."""
    data = _result_data(item)
    if not data:
        return []
    mu = data.get("modelUsage") or {}
    rows = []
    for model, v in mu.items():
        if not isinstance(v, dict):
            continue
        rows.append({
            "model": model,
            "input": int(v.get("inputTokens", 0) or 0),
            "output": int(v.get("outputTokens", 0) or 0),
            "cache_read": int(v.get("cacheReadInputTokens", 0) or 0),
            "cache_create": int(v.get("cacheCreationInputTokens", 0) or 0),
        })
    rows.sort(key=lambda r: -(r["input"] + r["output"] +
                              r["cache_read"] + r["cache_create"]))
    return rows


# `aggregate_token_usage` does a full-table scan over `items.result_json` and
# Python-side JSON-decodes every blob, so calling it twice per 500ms render
# tick was O(completed_items) per tick — the exact pattern flagged by the
# dashboard-hang gotcha in CLAUDE.md. A 2s TTL keeps cumulative totals
# imperceptibly stale while dropping repeat ticks to O(1).
_TOKEN_CACHE_TTL_S = 2.0
_TOKEN_5H_SECONDS = 5 * 3600
_TOKEN_WEEK_SECONDS = 7 * 24 * 3600
_token_cache: dict = {"key": None, "computed_at": 0.0, "value": None}


def _token_windows_invalidate() -> None:
    """Clear the token-windows cache so the next call recomputes. Exposed for
    tests; also safe to call from callers that know totals just changed."""
    _token_cache["key"] = None
    _token_cache["computed_at"] = 0.0
    _token_cache["value"] = None


def _token_windows(store: Store, daemon_started_at: float) -> dict[str, dict]:
    """Compute rolling 5-hour and weekly token totals in one pass — the same
    two windows that `claude.ai/settings/usage` headlines.

    `daemon_started_at` is retained in the cache key (so swapping daemons
    busts cached aggregates) but no longer affects the windowing — both
    windows are rolling against `now()` so the dashboard mirrors what the
    operator sees on the Anthropic usage page.

    Result is cached for `_TOKEN_CACHE_TTL_S` seconds so the 500ms render
    loop doesn't re-aggregate the whole items table every tick.
    """
    now = time.time()
    # Key includes id(store) so swapping the backing Store (notably between
    # tests with fresh TemporaryDirectory-backed DBs) correctly bypasses a
    # cached aggregate that belonged to a prior store.
    key = (id(store), daemon_started_at)
    cached = _token_cache["value"]
    if (cached is not None
            and _token_cache["key"] == key
            and now - _token_cache["computed_at"] < _TOKEN_CACHE_TTL_S):
        return cached  # type: ignore[return-value]
    result = {
        "5h": store.aggregate_token_usage(since=now - _TOKEN_5H_SECONDS),
        "week": store.aggregate_token_usage(since=now - _TOKEN_WEEK_SECONDS),
    }
    _token_cache["key"] = key
    _token_cache["computed_at"] = now
    _token_cache["value"] = result
    return result


def _pct_of_budget(total: int, budget: int) -> int | None:
    """Integer percent of `total / budget`, clamped at 100. Returns None when
    no budget configured so callers can switch to a raw-total fallback."""
    if budget <= 0:
        return None
    pct = int(total * 100 / budget)
    return min(pct, 100)


def _fmt_pct_of_budget(total: int, budget: int) -> str:
    """`(NN%)` suffix for legacy callers. Empty when budget is 0; clamps at
    `>99%` so a busted budget doesn't spam 4-digit percentages."""
    pct = _pct_of_budget(total, budget)
    if pct is None:
        return ""
    if pct > 99:
        return " (>99%)"
    return f" ({pct}%)"


def _fmt_pct_cell(total: int, budget: int, *, compact: bool = False) -> str:
    """Render one usage cell in the same idiom as claude.ai/settings/usage:
    the percentage leads when a budget is configured, with the raw counts in
    parentheses for context. Falls back to a bare token total when no budget
    is configured so operators without a configured cap still see activity.

    `compact=True` drops the parenthesised raw counts (used by the status-
    line glance, where the panel row already carries the full breakdown)."""
    pct = _pct_of_budget(total, budget)
    if pct is None:
        return _fmt_tokens(total)
    pct_str = ">99%" if pct > 99 else f"{pct}%"
    if compact:
        return pct_str
    return f"{pct_str} ({_fmt_tokens(total)} / {_fmt_tokens(budget)})"


def _fmt_token_compact(windows: dict, agent_cfg=None) -> str:
    """One-glance 5h + weekly readout for the status line. Mirrors the two
    cells claude.ai/settings/usage headlines: rolling 5-hour and rolling
    weekly windows, leading with `NN%` when budgets are configured.

    When `agent_cfg` supplies non-zero `session_token_budget` /
    `weekly_token_budget`, the cells render as percentages (matching the
    Anthropic usage page); otherwise the cell falls back to the raw token
    total so operators without a configured cap still see activity."""
    five_h = int(windows.get("5h", {}).get("total", 0))
    wk = int(windows.get("week", {}).get("total", 0))
    five_h_budget = getattr(agent_cfg, "session_token_budget", 0) or 0
    wk_budget = getattr(agent_cfg, "weekly_token_budget", 0) or 0
    return (f"tok 5h={_fmt_pct_cell(five_h, five_h_budget, compact=True)}  "
            f"wk={_fmt_pct_cell(wk, wk_budget, compact=True)}")


def _fmt_token_row(windows: dict, agent_cfg=None, tier: str = "wide") -> str:
    """One-line token readout mirroring claude.ai/settings/usage's two
    cells: rolling 5-hour and rolling weekly windows. Each cell leads with
    `NN%` when the matching budget is configured, with `(used / budget)`
    appended for context. Without a budget the cell falls back to the raw
    token total so operators without a configured cap still see activity.

    Narrow tier (<60 col) drops the parenthesised raw counts so the row
    fits under 50 chars even with M-scale totals."""
    five_h = int(windows.get("5h", {}).get("total", 0))
    wk = int(windows.get("week", {}).get("total", 0))
    five_h_budget = getattr(agent_cfg, "session_token_budget", 0) or 0
    wk_budget = getattr(agent_cfg, "weekly_token_budget", 0) or 0
    if tier == "narrow":
        return (f"tok 5h={_fmt_pct_cell(five_h, five_h_budget, compact=True)}  "
                f"wk={_fmt_pct_cell(wk, wk_budget, compact=True)}")
    return (f"usage  5h {_fmt_pct_cell(five_h, five_h_budget)}  "
            f"wk {_fmt_pct_cell(wk, wk_budget)}")


def _build_commit_message(item: StoredItem) -> str:
    """Commit message sourced from the agent's own summary, not the user.
    Falls back to the item title if no summary is available."""
    data = _result_data(item)
    summary = ""
    if data:
        summary = (data.get("result") or data.get("summary") or "").strip()
    subject = item.title.strip() or f"agent item {item.id[:8]}"
    if not summary or summary == subject:
        return f"{subject}\n\nAgent work for item {item.id}."
    return f"{subject}\n\n{summary}\n\nAgent work for item {item.id}."
