# Audit remaining sync subprocess calls on the curses thread — 2026-04-19

Follow-up to `2026-04-18-dashboard-hang.md`, which only wrapped
`diff_vs_base`. Grep-audited all of `agentor/dashboard/` for
`subprocess.`, `git_ops.`, `.read_text(`, `.read_bytes(`, `.open(`,
`_invoke_*`, `runner.*`, and non-trivial `store.*` calls to confirm each
runs on a worker thread (via `_run_with_progress`) or is justifiably
inline (cheap, bounded, O(1) per tick).

## Call-site dispositions

### Wrapped — runs on a background thread via `_run_with_progress`
- `modes.py:304` `approve_and_commit(cfg, store, item, msg, progress=p)` —
  AWAITING_REVIEW `[a]` approve+merge.
- `modes.py:329–332` `diff_vs_base(wt, cfg.git.base_branch)` —
  AWAITING_REVIEW `[v]` diff (fixed in 2026-04-18 dashboard-hang PR).
- `modes.py:344` `retry_merge(cfg, store, item, progress=p)` —
  CONFLICTED `[m]` retry merge.
- `modes.py:699` `subprocess.run(["claude", ...])` inside
  `_expand_note_via_claude`; the sole caller is `_new_issue_mode` at
  `modes.py:792` which wraps the call in `_run_with_progress`.

### Justified inline — bounded tail reads (256 KB cap)
- `transcript.py:36` `path.open("rb")` + seek-to-tail + `fh.read()` in
  `_tail_lines`; hard-capped at `_TAIL_BYTES = 256 * 1024`.
- `modes.py:452, 544` `_session_activity(transcript_path)` →
  `iter_events(..., tail_bytes=_TAIL_BYTES)`; 1 Hz inspect refresh, cap
  applied.
- `modes.py:458, 550` `_tail_lines(transcript_path)` — same 256 KB cap.

### Justified inline — one-shot per user action, not per tick
- `modes.py:674–684` `Path(first)`, `parent.mkdir(parents=True, ...)`,
  `p.is_absolute()` inside `_new_issue_target` — path arithmetic,
  single mkdir.
- `modes.py:758` `file_path.read_text()` in `_append_checkbox_block`
  — fires once per `_new_issue_mode` submit, against a watched markdown
  file (typically <100 KB).
- `modes.py:763, 808` `file_path.write_text(...)` — same one-shot
  submit path; small markdown payload.
- `modes.py:813` `scan_once(cfg, store)` — walks watched markdown files
  once per submit to surface the new item; not in any render loop.
- `render.py:62` `os.open("/dev/tty", os.O_WRONLY)` — cached in
  `_TTY_FD`, opened at most once for the process lifetime.
- `render.py:63` `os.write(_TTY_FD, ...)` — single tiny OSC-0 escape
  per render tick; best-effort, wrapped in `try/except OSError`.

### Justified inline — O(1) / single-row indexed DB reads per tick
- `render.py:128` `store.count_by_status(st)` × len(ItemStatus) per main
  render — indexed `SELECT COUNT(*) WHERE status=?`.
- `render.py:220` `store.list_by_status(st)` per displayed filter —
  bounded by active queue size; rows are short.
- `render.py:258` and `formatters.py:56` `store.latest_transition_at(...)`
  — single-row ordered LIMIT 1 (fixed in 2026-04-18 PR).
- `modes.py:524` `store.list_failures(item.id, limit=10)` and
  `modes.py:527` `store.count_failures(item.id)` — 1 Hz inspect refresh;
  both are item-scoped queries with a `LIMIT`.
- `modes.py:327` `Path(item.worktree_path)` — trivial constructor.
- `modes.py:443` `transcript_path.exists()` — single stat call.

### Fixed in this change — was violating the hot-path O(1) budget
- `render.py:148` `_render_token_panel(...)` → `formatters._token_windows`
  → `store.aggregate_token_usage(since=...)` × 3 per 500 ms tick. The
  aggregator does `SELECT updated_at, result_json FROM items WHERE
  result_json IS NOT NULL` (full table scan) and then Python-side
  `json.loads()` on every row's blob. On a long-running dashboard this
  grows linearly with the archive of merged/errored items — same failure
  mode as the pre-fix transcript `read_text()`.

  Fix: module-level TTL cache in `agentor/dashboard/formatters.py`
  keyed on `(id(store), daemon_started_at)` with
  `_TOKEN_CACHE_TTL_S = 2.0`. Repeat ticks inside the TTL window return
  the cached dict (O(1)); one full recompute every ~2 s keeps totals
  fresh without the per-tick scan. `_token_windows_invalidate()` is
  exported for tests and reserved for callers that know totals just
  changed.

## Verification
- `python3 -m unittest discover tests -v` — all pass.
- New test coverage in
  `tests/test_dashboard_formatters.py::TestTokenWindowsCache`: cache
  hit within TTL (no delegate calls), invalidate forces recompute,
  monkeypatched clock past TTL forces recompute, differing
  `id(store)` busts cache, differing `daemon_started_at` busts cache.
