import curses
import json
import subprocess
import time
from collections import deque
from pathlib import Path

from .config import Config
from .daemon import Daemon
from .git_ops import diff_vs_base
from .models import ItemStatus
from .store import Store, StoredItem

ACTIONS = ("[p]ickup  [r]eview  [d]eferred  [i]nspect  "
           "[tab]filter  [+/-]pool  [m]ode  [u]npause  [q]uit")

# Filter views: ordered list cycled by Tab. Each entry maps a filter name
# to the statuses to display (None = all).
FILTERS: list[tuple[str, list[ItemStatus] | None]] = [
    ("all", [ItemStatus.WORKING, ItemStatus.AWAITING_PLAN_REVIEW,
             ItemStatus.AWAITING_REVIEW, ItemStatus.QUEUED,
             ItemStatus.BACKLOG, ItemStatus.ERRORED, ItemStatus.REJECTED,
             ItemStatus.MERGED, ItemStatus.CANCELLED, ItemStatus.DEFERRED]),
    ("errored", [ItemStatus.ERRORED]),
    ("backlog", [ItemStatus.BACKLOG]),
    ("queued", [ItemStatus.QUEUED]),
    ("working", [ItemStatus.WORKING]),
    ("awaiting_plan", [ItemStatus.AWAITING_PLAN_REVIEW]),
    ("awaiting", [ItemStatus.AWAITING_REVIEW]),
    ("deferred", [ItemStatus.DEFERRED]),
    ("merged", [ItemStatus.MERGED]),
    ("rejected", [ItemStatus.REJECTED]),
]
REFRESH_MS = 500


def run_dashboard(
    cfg: Config, store: Store, daemon: Daemon, log_ring: deque
) -> None:
    curses.wrapper(_loop, cfg, store, daemon, log_ring)


def _loop(stdscr, cfg: Config, store: Store, daemon: Daemon, log_ring: deque):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(REFRESH_MS)
    _init_colors()

    filter_idx = 0  # index into FILTERS
    while True:
        try:
            _render(stdscr, cfg, store, daemon, log_ring, filter_idx)
            ch = stdscr.getch()
        except KeyboardInterrupt:
            # ctrl-c in the dashboard should exit cleanly, not crash with a
            # stack trace. The daemon is a daemon thread; leaving the loop
            # lets curses.wrapper restore the terminal and the process exits.
            return
        if ch == -1:
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q":
            return
        if k == "r":
            _review_mode(stdscr, cfg, store, daemon)
        elif k == "p":
            _pickup_mode(stdscr, cfg, store, daemon)
        elif k == "d":
            _deferred_mode(stdscr, cfg, store)
        elif k == "i":
            _inspect_mode(stdscr, cfg, store)
        elif ch in (ord("+"), ord("=")):
            # '=' is the unshifted key that shares '+'; accept both so the
            # user doesn't have to hold shift. Kick dispatch now so the new
            # slot is filled immediately instead of waiting for the next scan.
            cfg.agent.pool_size += 1
            daemon.try_fill_pool()
        elif ch in (ord("-"), ord("_")):
            # Pool = 0 is a valid "pause" — in-flight workers finish naturally,
            # no new dispatches happen until you bump pool back up.
            cfg.agent.pool_size = max(0, cfg.agent.pool_size - 1)
        elif k == "m":
            cfg.agent.pickup_mode = (
                "auto" if cfg.agent.pickup_mode == "manual" else "manual"
            )
            if cfg.agent.pickup_mode == "auto":
                daemon.try_fill_pool()
        elif k == "u":
            # Acknowledge a system alert and resume dispatching. No-op when
            # nothing is paused, so safe to spam.
            daemon.clear_alert()
        elif k == "\t":
            filter_idx = (filter_idx + 1) % len(FILTERS)


def _init_colors():
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    except curses.error:
        pass


def _status_color(status: ItemStatus) -> int:
    return {
        ItemStatus.BACKLOG: 3,
        ItemStatus.QUEUED: 3,
        ItemStatus.WORKING: 5,
        ItemStatus.AWAITING_PLAN_REVIEW: 1,
        ItemStatus.AWAITING_REVIEW: 1,
        ItemStatus.MERGED: 2,
        ItemStatus.REJECTED: 4,
        ItemStatus.CANCELLED: 4,
    }.get(status, 0)


def _render(stdscr, cfg, store, daemon, log_ring, filter_idx):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    row = 0

    # header bar — show project + active filter
    filter_name, _ = FILTERS[filter_idx]
    title = f" agentor — {cfg.project_name}    filter: {filter_name} ({filter_idx + 1}/{len(FILTERS)}) "
    _safe_addstr(stdscr, row, 0, title.ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    row += 1

    # actions bar
    _safe_addstr(stdscr, row, 0, f" {ACTIONS}".ljust(w), w, curses.A_REVERSE)
    row += 1

    # system alert banner — sticky red row when daemon has paused itself
    # because of an infrastructure failure. Truncated to fit; full message
    # is in the log ring.
    if daemon.system_alert:
        # Tail of the message is usually the most informative part of a git
        # error (the actual failure line). Show the prefix and let the user
        # scroll the log for the full text.
        msg = daemon.system_alert.replace("\n", " ").strip()
        if len(msg) > w - 30:
            msg = msg[: w - 33] + "..."
        banner = f" ⚠ PAUSED — {msg}  (press [u] to resume) "
        _safe_addstr(stdscr, row, 0, banner.ljust(w), w,
                     curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
        row += 1

    # combined status + counts line
    s = daemon.stats
    counts = {st: store.count_by_status(st) for st in ItemStatus}
    # Active items (excluding terminal states) that currently carry an
    # unresolved last_error. Surfaces stuck/faulty items in the header
    # even when the default 'all' filter would visually smear them into
    # the rest of the queue.
    status_line = (
        f" {cfg.agent.runner}  pool={cfg.agent.pool_size}  "
        f"mode={cfg.agent.pickup_mode}  "
        f"workers={len(daemon.workers)}  "
        f"done={s.completed}  errored={counts[ItemStatus.ERRORED]}  │  "
        f"backlog={counts[ItemStatus.BACKLOG]}  "
        f"queued={counts[ItemStatus.QUEUED]}  "
        f"working={counts[ItemStatus.WORKING]}  "
        f"plan?={counts[ItemStatus.AWAITING_PLAN_REVIEW]}  "
        f"awaiting={counts[ItemStatus.AWAITING_REVIEW]}  "
        f"deferred={counts[ItemStatus.DEFERRED]}  "
        f"merged={counts[ItemStatus.MERGED]}  "
        f"rejected={counts[ItemStatus.REJECTED]}"
    )
    _safe_addstr(stdscr, row, 0, status_line, w)
    row += 1
    _safe_addstr(stdscr, row, 0, "─" * w, w, curses.A_DIM)
    row += 1

    # body table
    body_top = row
    body_height = h - body_top - 1  # leave 1 line for log
    _filter_name_cur, filter_statuses = FILTERS[filter_idx]
    statuses = filter_statuses if filter_statuses is not None else list(ItemStatus)
    _render_table(stdscr, store, body_top, body_height, w, statuses,
                  cfg.agent.context_window)

    # log tail
    log_row = h - 1
    latest = list(log_ring)[-1:] if log_ring else [""]
    _safe_addstr(stdscr, log_row, 0,
                 (f" log: {latest[0]}" if latest[0] else
                  " log: (no events yet)").ljust(w), w, curses.A_DIM)
    stdscr.refresh()


def _safe_addstr(stdscr, y, x, s, w, attr=0):
    try:
        stdscr.addnstr(y, x, s, w, attr)
    except curses.error:
        pass


def _elapsed_for(store: Store, item_id: str) -> float | None:
    """Seconds since the most recent transition INTO `working` for this item."""
    for t in reversed(store.transitions_for(item_id)):
        if t["to_status"] == ItemStatus.WORKING.value:
            return max(0.0, time.time() - float(t["at"]))
    return None


def _fmt_elapsed(sec: float | None) -> str:
    if sec is None:
        return "—:—"
    m, s = divmod(int(sec), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# Table column layout. The TITLE column gets whatever width remains.
_COL_ID = 10      # 8 chars + 2 pad
_COL_STATE = 18   # widest status name + pad
_COL_ELAPSED = 9
_COL_CTX = 6      # "100%  " — last-turn context fill vs window
_COL_SOURCE = 26


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _result_data(item: StoredItem) -> dict | None:
    if not item.result_json:
        return None
    try:
        return json.loads(item.result_json)
    except json.JSONDecodeError:
        return None


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


def _render_table(stdscr, store, top, height, w, statuses, context_window):
    if height <= 0:
        return
    header = (f" {'ID':<{_COL_ID-1}}{'STATE':<{_COL_STATE}}"
              f"{'ELAPSED':<{_COL_ELAPSED}}{'CTX':<{_COL_CTX}}"
              f"{'SOURCE':<{_COL_SOURCE}}TITLE")
    _safe_addstr(stdscr, top, 0, header.ljust(w), w,
                 curses.A_BOLD | curses.A_UNDERLINE)
    rows_used = 1
    for st in statuses:
        items = store.list_by_status(st)
        for it in items:
            has_err = bool(it.last_error)
            if rows_used >= height:
                _safe_addstr(stdscr, top + rows_used - 1, 0,
                             f" ... (more not shown)".ljust(w), w,
                             curses.A_DIM)
                return
            elapsed = _elapsed_for(store, it.id) if st == ItemStatus.WORKING else None
            elapsed_s = _fmt_elapsed(elapsed) if elapsed is not None else "—"
            ctx_s = _ctx_fill_pct(it, context_window)
            src = it.source_file
            if len(src) > _COL_SOURCE - 1:
                src = "…" + src[-(_COL_SOURCE - 2):]
            title_max = max(0, w - 1 - _COL_ID - _COL_STATE - _COL_ELAPSED
                              - _COL_CTX - _COL_SOURCE)
            # `!` marker on the state column when the item carries an
            # unresolved error — makes sticky problems visible in the
            # default view without needing the errors filter.
            marker = "!" if has_err else " "
            state_cell = f"{marker}{st.value}"[: _COL_STATE]
            title = it.title[:title_max]
            line = (f" {it.id[:8]:<{_COL_ID-1}}{state_cell:<{_COL_STATE}}"
                    f"{elapsed_s:<{_COL_ELAPSED}}{ctx_s:<{_COL_CTX}}"
                    f"{src:<{_COL_SOURCE}}{title}")
            if has_err:
                # Red for rows that need the user's attention; overrides
                # the status-based color so the error is unmistakable.
                attr = curses.color_pair(4) | curses.A_BOLD
            else:
                attr = curses.color_pair(_status_color(st))
                if st == ItemStatus.WORKING:
                    attr |= curses.A_BOLD
            _safe_addstr(stdscr, top + rows_used, 0, line, w, attr)
            rows_used += 1


def _pickup_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Curses-native, one-item-per-screen pickup over BACKLOG + DEFERRED.
    Approving promotes BACKLOG → QUEUED; the daemon dispatches on its own."""
    from .committer import approve_backlog, defer, delete_idea, restore_deferred
    items = (store.list_by_status(ItemStatus.BACKLOG)
             + store.list_by_status(ItemStatus.DEFERRED))
    if not items:
        _flash(stdscr, "no backlog or deferred items.")
        return
    stdscr.nodelay(False)
    try:
        for it in items:
            fresh = store.get(it.id)
            if fresh is None:
                continue
            scroll = 0
            while True:
                h, w = stdscr.getmaxyx()
                header = [
                    f"  pickup · {fresh.status.value} · {fresh.title}",
                    f"  id {fresh.id[:8]}  source {fresh.source_file}:"
                    f"{fresh.source_line}  attempts "
                    f"{fresh.attempts}/{cfg.agent.max_attempts}",
                ]
                body = _wrap(fresh.body or "(no description)", w - 2)
                _show_item_screen(
                    stdscr, header, body,
                    " [y]approve  [s]defer  [x]delete  [n]leave  [q]uit "
                    " · [j/k]scroll ",
                    content_scroll=scroll,
                )
                ch = stdscr.getch()
                new_scroll = _scroll_key(ch, scroll, len(body), max(1, h - 6))
                if new_scroll >= 0:
                    scroll = new_scroll
                    continue
                k = chr(ch).lower() if 0 < ch < 256 else ""
                if k == "q":
                    return
                if k == "y":
                    f = store.get(fresh.id)
                    if f is None:
                        break
                    if f.status == ItemStatus.DEFERRED:
                        restored = restore_deferred(store, f)
                        if restored == ItemStatus.BACKLOG:
                            approve_backlog(store, store.get(f.id))
                    elif f.status == ItemStatus.BACKLOG:
                        approve_backlog(store, f)
                    daemon.try_fill_pool()
                    break
                if k == "s":
                    f = store.get(fresh.id)
                    if f and f.status != ItemStatus.DEFERRED:
                        defer(store, f)
                    break
                if k == "x":
                    f = store.get(fresh.id)
                    if f and _prompt_yn(stdscr, "delete this idea?"):
                        delete_idea(store, f)
                    break
                if k in ("n", ""):
                    break
    finally:
        stdscr.nodelay(True)


def _deferred_mode(stdscr, cfg: Config, store: Store) -> None:
    """Walk DEFERRED items as curses-native screens. r=restore, n=leave, q=quit."""
    from .committer import restore_deferred
    items = store.list_by_status(ItemStatus.DEFERRED)
    if not items:
        _flash(stdscr, "no deferred items.")
        return
    stdscr.nodelay(False)
    try:
        for it in items:
            fresh = store.get(it.id)
            if fresh is None:
                continue
            h, w = stdscr.getmaxyx()
            header = [
                f"  deferred · {fresh.title}",
                f"  id {fresh.id[:8]}  source {fresh.source_file}",
            ]
            body = _wrap(fresh.body or "(no description)", w - 2)
            scroll = 0
            while True:
                _show_item_screen(
                    stdscr, header, body,
                    " [r]estore  [n]leave  [q]uit · [j/k]scroll ",
                    content_scroll=scroll,
                )
                ch = stdscr.getch()
                new_scroll = _scroll_key(ch, scroll, len(body), max(1, h - 6))
                if new_scroll >= 0:
                    scroll = new_scroll
                    continue
                k = chr(ch).lower() if 0 < ch < 256 else ""
                if k == "q":
                    return
                if k == "r":
                    restore_deferred(store, fresh)
                    break
                if k in ("n", ""):
                    break
    finally:
        stdscr.nodelay(True)


def _inspect_mode(stdscr, cfg: Config, store: Store) -> None:
    """Prompt (in-curses) for an item id prefix, then render the full detail
    view in a scrollable single-item screen. Blank input = first WORKING item."""
    stdscr.nodelay(False)
    try:
        prefix = _prompt_text(stdscr, "item id prefix (blank = working): ")
        target = None
        if not prefix:
            working = store.list_by_status(ItemStatus.WORKING)
            target = working[0] if working else None
        else:
            for st in ItemStatus:
                for it in store.list_by_status(st):
                    if it.id.startswith(prefix):
                        target = it
                        break
                if target:
                    break
        if target is None:
            _flash(stdscr, f"no item matching {prefix!r}")
            return
        _inspect_render(stdscr, cfg, store, target)
    finally:
        stdscr.nodelay(True)


def _inspect_render(stdscr, cfg: Config, store: Store, item: StoredItem) -> None:
    lines = _build_detail_lines(cfg, store, item)
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        header = [
            f"  inspect · {item.title}",
            f"  id {item.id[:8]}  status {item.status.value}",
        ]
        _show_item_screen(
            stdscr, header, lines,
            " [q/enter]close · [j/k]scroll · [space/pgdn]page ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(lines), max(1, h - 4))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q" or ch in (10, 13, 27):
            return


def _build_detail_lines(cfg: Config, store: Store, item: StoredItem) -> list[str]:
    out: list[str] = []
    out.append(f"id:       {item.id}")
    out.append(f"title:    {item.title}")
    out.append(f"state:    {item.status.value}")
    out.append(f"source:   {item.source_file}:{item.source_line}")
    out.append(f"branch:   {item.branch or '—'}")
    out.append(f"worktree: {item.worktree_path or '—'}")
    out.append(f"session:  {item.session_id or '—'}")
    out.append(f"attempts: {item.attempts} / {cfg.agent.max_attempts}")
    elapsed = _elapsed_for(store, item.id)
    if elapsed is not None:
        out.append(f"elapsed:  {_fmt_elapsed(elapsed)} (since enter WORKING)")
    data = _result_data(item)
    if not data:
        out.append("")
        out.append("(no agent result yet — no token data)")
        return out
    out.append("")
    out.append("── agent run ──")
    if "num_turns" in data:
        out.append(f"turns:    {data['num_turns']}")
    if "duration_ms" in data:
        out.append(f"wall:     {data['duration_ms'] / 1000:.1f}s "
                   f"(api: {data.get('duration_api_ms', 0) / 1000:.1f}s)")
    if "stop_reason" in data:
        out.append(f"stop:     {data['stop_reason']}")
    rows = _token_breakdown(item)
    if rows:
        out.append("")
        out.append("── per-model tokens ──")
        out.append(f"{'MODEL':<36} {'IN':>10} {'OUT':>10} "
                   f"{'CACHE_R':>12} {'CACHE_W':>10}")
        for r in rows:
            out.append(f"{r['model']:<36} "
                       f"{_fmt_tokens(r['input']):>10} "
                       f"{_fmt_tokens(r['output']):>10} "
                       f"{_fmt_tokens(r['cache_read']):>12} "
                       f"{_fmt_tokens(r['cache_create']):>10}")
    summary = data.get("result") or data.get("summary")
    if summary:
        out.append("")
        out.append("── summary ──")
        out.extend(summary[:4000].splitlines())
    if item.last_error:
        out.append("")
        out.append(f"last_error: {item.last_error[:500]}")
    failures = store.list_failures(item.id, limit=10)
    if failures:
        out.append("")
        out.append(f"── failure history ({store.count_failures(item.id)} "
                   f"total, last {len(failures)} shown) ──")
        for f in failures:
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(float(f["at"])))
            header = (f"#{f['attempt']} {f['phase'] or '—'}  {when}"
                      f"  turns={f['num_turns'] or '—'}"
                      f"  dur={f['duration_ms'] and f'{f['duration_ms']/1000:.1f}s' or '—'}")
            out.append(header)
            err = (f["error"] or "").strip()
            # Keep each failure compact: first line, up to 3 wrapped lines.
            for ln in err.splitlines()[:3]:
                out.append(f"  {ln[:300]}")
            if f.get("transcript_path"):
                out.append(f"  transcript: {f['transcript_path']}")
    return out


def _review_mode(stdscr, cfg: Config, store: Store, daemon: Daemon) -> None:
    """Walk plan + code reviews as curses-native single-item screens.
    Plan reviews run first (they gate the pipeline)."""
    plan_items = store.list_by_status(ItemStatus.AWAITING_PLAN_REVIEW)
    code_items = store.list_by_status(ItemStatus.AWAITING_REVIEW)
    items = plan_items + code_items
    if not items:
        _flash(stdscr, "no items awaiting review.")
        return
    stdscr.nodelay(False)
    try:
        for item in items:
            fresh = store.get(item.id)
            if fresh is None:
                continue
            if fresh.status == ItemStatus.AWAITING_PLAN_REVIEW:
                if _review_plan_curses(stdscr, cfg, store, daemon, fresh) == "quit":
                    return
            elif fresh.status == ItemStatus.AWAITING_REVIEW:
                if _review_code_curses(stdscr, cfg, store, daemon, fresh) == "quit":
                    return
    finally:
        stdscr.nodelay(True)


def _review_plan_curses(stdscr, cfg: Config, store: Store, daemon: Daemon,
                        item: StoredItem) -> str:
    """Plan review as a single-item curses screen. Returns "quit" if the user
    pressed q, else "" to continue walking."""
    from .committer import approve_plan, defer, reject, reject_and_retry
    data = _result_data(item) or {}
    plan_text = data.get("plan") or data.get("summary") or "(no plan text)"
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        header = [
            f"  plan review · {item.title}",
            f"  id {item.id[:8]}  session "
            f"{(item.session_id or '—')[:8]}  source "
            f"{item.source_file}:{item.source_line}",
        ]
        content = _wrap(plan_text, w - 2)
        _show_item_screen(
            stdscr, header, content,
            " [a]approve → execute  [r]eject+feedback  [s]defer  "
            "[n]leave  [q]uit · [j/k]scroll ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(content), max(1, h - 6))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        fresh = store.get(item.id)
        if fresh is None:
            return ""
        if k == "q":
            return "quit"
        if k == "a":
            approve_plan(store, fresh)
            daemon.try_fill_pool()
            return ""
        if k == "r":
            _handle_reject_flow(stdscr, store, fresh, "plan")
            return ""
        if k == "s":
            defer(store, fresh)
            return ""
        if k in ("n", ""):
            return ""


def _review_code_curses(stdscr, cfg: Config, store: Store, daemon: Daemon,
                        item: StoredItem) -> str:
    """Code review (post-commit) as a single-item curses screen."""
    from .committer import approve_and_commit, defer, reject, reject_and_retry
    data = _result_data(item) or {}
    summary = data.get("summary") or "(no summary)"
    files = data.get("files_changed") or []
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        header = [
            f"  code review · {item.title}",
            f"  id {item.id[:8]}  branch {item.branch or '—'}  "
            f"files {len(files)}  tokens {_tokens_total(item)}",
        ]
        content: list[str] = []
        content += _wrap(summary, w - 2)
        content.append("")
        content.append("── files changed ──")
        for f in files[:50]:
            content.append(f"  {f}")
        if len(files) > 50:
            content.append(f"  ... and {len(files) - 50} more")
        _show_item_screen(
            stdscr, header, content,
            " [a]approve+merge  [r]eject+feedback  [s]defer  [v]diff  "
            "[n]leave  [q]uit · [j/k]scroll ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(content), max(1, h - 6))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        fresh = store.get(item.id)
        if fresh is None:
            return ""
        if k == "q":
            return "quit"
        if k == "a":
            msg = _build_commit_message(fresh)
            approve_and_commit(cfg, store, fresh, msg)
            return ""
        if k == "r":
            _handle_reject_flow(stdscr, store, fresh, "code")
            return ""
        if k == "s":
            defer(store, fresh)
            return ""
        if k == "v":
            if item.worktree_path:
                diff = diff_vs_base(Path(item.worktree_path),
                                    cfg.git.base_branch)
                _view_text_in_curses(stdscr, diff or "(empty diff)")
            continue
        if k in ("n", ""):
            return ""


def _handle_reject_flow(stdscr, store: Store, item: StoredItem,
                        kind: str) -> None:
    """Collect feedback inline; enter submits it as a retry (reject_and_retry).
    Empty feedback cancels — a silent rejection is almost always a mis-press.
    `kind` is 'plan' or 'code', used only for the prompt wording."""
    from .committer import reject_and_retry
    feedback = _prompt_text(stdscr, f"feedback ({kind} retry, empty=cancel): ")
    if not feedback:
        return
    reject_and_retry(store, item, feedback)


def _view_text_in_curses(stdscr, text: str) -> None:
    """Scroll a block of text (diff, log output) in-curses. j/k/pgup/pgdn
    scroll; q or enter exits. Avoids dropping back to the shell pager."""
    lines = text.splitlines() or ["(empty)"]
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        _show_item_screen(
            stdscr, ["  diff view"], lines,
            " [q/enter]close · [j/k]scroll · [space/pgdn]page ",
            content_scroll=scroll,
        )
        ch = stdscr.getch()
        new_scroll = _scroll_key(ch, scroll, len(lines), max(1, h - 4))
        if new_scroll >= 0:
            scroll = new_scroll
            continue
        k = chr(ch).lower() if 0 < ch < 256 else ""
        if k == "q" or ch in (10, 13, 27):  # q, \n, \r, esc
            return


def _flash(stdscr, msg: str) -> None:
    """Briefly show `msg` across the bottom row. Used for transient
    confirmations that don't warrant a full overlay."""
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, (" " + msg).ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    stdscr.refresh()
    curses.napms(1200)


def _show_item_screen(
    stdscr,
    header_lines: list[str],
    content_lines: list[str],
    action_hint: str,
    content_scroll: int = 0,
) -> None:
    """Render a single item full-screen, top-aligned:
      row 0       — title bar (reverse video)
      rows 1..N   — metadata/subheader (dim)
      separator
      rows M..    — content_lines, optionally scrolled
      last row    — action hint bar
    Content that overflows is truncated with a dim '...' marker."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    row = 0
    if header_lines:
        _safe_addstr(stdscr, row, 0, header_lines[0].ljust(w), w,
                     curses.A_BOLD | curses.A_REVERSE)
        row += 1
        for hl in header_lines[1:]:
            _safe_addstr(stdscr, row, 0, hl, w, curses.A_DIM)
            row += 1
    _safe_addstr(stdscr, row, 0, "─" * w, w, curses.A_DIM)
    row += 1
    content_top = row
    max_content_row = h - 2  # reserve last row for action hint
    # Visible slice of content_lines (top-aligned, scroll-aware).
    end = max_content_row - content_top
    visible = content_lines[content_scroll:content_scroll + end]
    for line in visible:
        if row >= max_content_row:
            break
        _safe_addstr(stdscr, row, 0, line[:w], w)
        row += 1
    truncated_above = content_scroll > 0
    truncated_below = content_scroll + end < len(content_lines)
    if truncated_above:
        _safe_addstr(stdscr, content_top, 0,
                     "↑ more above (k)".ljust(w), w, curses.A_DIM)
    if truncated_below:
        _safe_addstr(stdscr, max_content_row - 1, 0,
                     "↓ more below (j)".ljust(w), w, curses.A_DIM)
    _safe_addstr(stdscr, h - 1, 0, action_hint.ljust(w), w, curses.A_REVERSE)
    stdscr.refresh()


def _prompt_yn(stdscr, message: str) -> bool:
    """Overlay a yes/no confirmation on the bottom row. Returns True on 'y'."""
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, (" " + message + " [y/N] ").ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    stdscr.refresh()
    stdscr.nodelay(False)
    try:
        ch = stdscr.getch()
    finally:
        stdscr.nodelay(True)
    k = chr(ch).lower() if 0 < ch < 256 else ""
    return k == "y"


def _prompt_text(stdscr, message: str) -> str:
    """Inline text input on the bottom row. Returns the typed string."""
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, (" " + message).ljust(w), w,
                 curses.A_BOLD | curses.A_REVERSE)
    stdscr.move(h - 1, len(message) + 2)
    curses.curs_set(1)
    curses.echo()
    stdscr.nodelay(False)
    try:
        raw = stdscr.getstr(h - 1, len(message) + 2, max(1, w - len(message) - 3))
    finally:
        curses.noecho()
        curses.curs_set(0)
        stdscr.nodelay(True)
    try:
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _wrap(text: str, width: int) -> list[str]:
    """Wrap text into lines ≤ width while keeping explicit newlines. Long
    whitespace-free runs (eg. SHAs) are hard-broken."""
    import textwrap
    out: list[str] = []
    for para in (text or "").splitlines() or [""]:
        if not para.strip():
            out.append("")
            continue
        wrapped = textwrap.wrap(
            para, width=max(10, width - 2),
            replace_whitespace=False, drop_whitespace=False,
            break_long_words=True, break_on_hyphens=False,
        )
        out.extend(wrapped or [para])
    return out


def _scroll_key(ch: int, current: int, content_len: int, page: int) -> int:
    """Map a keypress to a new scroll offset. Returns -1 if the key isn't a
    scroll key, so callers can fall through to action handling."""
    if ch in (curses.KEY_DOWN, ord("j")):
        return min(current + 1, max(0, content_len - 1))
    if ch in (curses.KEY_UP, ord("k")):
        return max(0, current - 1)
    if ch in (curses.KEY_NPAGE, ord(" ")):
        return min(current + page, max(0, content_len - 1))
    if ch == curses.KEY_PPAGE:
        return max(0, current - page)
    return -1


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


