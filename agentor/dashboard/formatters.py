import datetime
import json
import time

from ..models import ItemStatus
from ..store import Store, StoredItem


# Table column layout. The TITLE column gets whatever width remains.
_COL_ID = 10      # 8 chars + 2 pad
_COL_STATE = 18   # widest status name + pad
_COL_ELAPSED = 9
_COL_CTX = 6      # "100%  " — last-turn context fill vs window
_COL_SOURCE = 26


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


def _ctx_fill_pct(item: StoredItem, fallback_window: int) -> str:
    """Approximate how full the main agent's context was on its last turn,
    as a percent of its context window.

    Honest formula: the `iterations` array in claude's JSON result is per-
    turn. On the LAST turn, `input_tokens + cache_read_input_tokens` is the
    total tokens the model had to read — i.e. how full the working context
    was. Summing across turns is the cumulative spend, a different number.

    Window is read from the largest `contextWindow` in `modelUsage` (which
    claude reports — 1M for the opus-4-6 1M variant, 200k for standard
    opus) to avoid a stale config default. Falls back to `fallback_window`."""
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


def _midnight_local_epoch(now: float | None = None) -> float:
    """Epoch seconds of the most recent local-midnight boundary. Uses the
    system tz (`astimezone()`), matching the backlog's "today (midnight-
    local)" framing. Separate helper so tests can freeze the clock."""
    ref = datetime.datetime.fromtimestamp(now if now is not None else time.time())
    midnight = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# `aggregate_token_usage` does a full-table scan over `items.result_json` and
# Python-side JSON-decodes every blob, so calling it three times per 500ms
# render tick was O(completed_items) per tick — the exact pattern flagged by
# the dashboard-hang gotcha in CLAUDE.md. A 2s TTL keeps cumulative totals
# imperceptibly stale while dropping repeat ticks to O(1).
_TOKEN_CACHE_TTL_S = 2.0
_token_cache: dict = {"key": None, "computed_at": 0.0, "value": None}


def _token_windows_invalidate() -> None:
    """Clear the token-windows cache so the next call recomputes. Exposed for
    tests; also safe to call from callers that know totals just changed."""
    _token_cache["key"] = None
    _token_cache["computed_at"] = 0.0
    _token_cache["value"] = None


def _token_windows(store: Store, daemon_started_at: float) -> dict[str, dict]:
    """Compute session / today / 7d token totals in one pass.

    `daemon_started_at == 0` means the daemon has not entered its main loop
    yet (e.g. tests); the "session" view then mirrors the "today" view so the
    panel stays populated instead of showing a confusing 0.

    Result is cached for `_TOKEN_CACHE_TTL_S` seconds keyed on
    `daemon_started_at` so the 500ms render loop doesn't re-aggregate the
    whole items table every tick.
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
    session_since: float | None = daemon_started_at or None
    today_since = _midnight_local_epoch(now)
    week_since = now - 7 * 24 * 3600
    result = {
        "session": store.aggregate_token_usage(since=session_since),
        "today": store.aggregate_token_usage(since=today_since),
        "7d": store.aggregate_token_usage(since=week_since),
    }
    _token_cache["key"] = key
    _token_cache["computed_at"] = now
    _token_cache["value"] = result
    return result


def _fmt_token_line(label: str, totals: dict) -> str:
    """One compact row for the token-usage panel. Kept narrow so it fits in
    80-column terminals: `session  in 1.5M  out 120k  cache_r 8.0M  cache_c 350k  Σ 10.0M`."""
    return (f"{label:<8}"
            f"in {_fmt_tokens(int(totals.get('input', 0))):>6}  "
            f"out {_fmt_tokens(int(totals.get('output', 0))):>6}  "
            f"cache_r {_fmt_tokens(int(totals.get('cache_read', 0))):>6}  "
            f"cache_c {_fmt_tokens(int(totals.get('cache_create', 0))):>6}  "
            f"Σ {_fmt_tokens(int(totals.get('total', 0))):>6}")


def _fmt_token_line_mid(label: str, totals: dict) -> str:
    """Mid-tier (60–79 col) compact form. Drops the cache columns and
    leads with Σ so the most operator-relevant number isn't clipped."""
    return (f"{label:<8}"
            f"Σ {_fmt_tokens(int(totals.get('total', 0))):>6}  "
            f"in {_fmt_tokens(int(totals.get('input', 0))):>6}  "
            f"out {_fmt_tokens(int(totals.get('output', 0))):>6}")


def _fmt_token_line_narrow(label: str, totals: dict) -> str:
    """Narrow-tier (<60 col) form. Label is truncated to 4 chars and
    only Σ survives — the other fields live in the inspect view."""
    short = label[:4]
    return (f"{short:<5}"
            f"Σ {_fmt_tokens(int(totals.get('total', 0))):>6}")


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
