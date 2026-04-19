---
name: agentor-review
description: Review agentor run artefacts (.agentor/agentor.log, transcripts, state.db, docs/agent-logs) to surface cross-run themes, analyse token spend / cache efficiency, and file backlog items for unresolved follow-ups. Use when the user asks to analyse/review agentor runs, agentor logs, agent-logs, reflect items, token usage, cost, or wants to turn run outcomes into backlog.
argument-hint: "[optional focus — e.g. failures, retries, tokens, follow-ups]"
---

# Agentor run review

Review what agentor produced in this repo — daemon log, transcripts, state
DB, and per-run reflect items — and turn unresolved follow-ups into backlog
items. Skip gracefully when nothing has changed since the last review.

**RULE: never file a backlog item or mutate any file in Phase 5 without
explicit user approval.** Present the candidate table first, let the user
pick which to file.

$ARGUMENTS

If a focus area was provided above, weight analysis toward that topic while
still producing the full candidate list in Phase 4.

## Phase 1 — Skip check (do this first)

Read `.agentor/analyses/` for prior review records. For the most recent
`YYYY-MM-DD-*.md` entry:

1. Parse its **Skip until** section — a list of conditions that would make
   a new review worthwhile.
2. Compare against current state:
   - Count `failed:` lines in `.agentor/agentor.log` since the record's
     cut-off timestamp. Note any novel error class (not in the previous
     taxonomy).
   - Count `docs/agent-logs/*.md` files added since the cut-off
     (`git log --since=<date> --diff-filter=A --name-only -- docs/agent-logs/`).
   - Check for newly merged backlog items whose scope touches the failure
     surface (retry/backoff, turn budget, subprocess lifecycle).
3. If **none** of the skip-until triggers fired, tell the user which prior
   record covers this window and stop. Don't re-run the analysis.
4. If any trigger fired, name which one and continue.

If no prior record exists, skip straight to Phase 2.

## Phase 2 — Collect inputs

Gather in parallel (cheap, read-only):

- `.agentor/agentor.log` — tail for recent events; grep event-type
  histogram: `dispatch:`, `awaiting_review:`, `failed:`, `killing`,
  `killed`, `auto-recovered`, `queued`, `scan:`, `waiting`.
- `.agentor/state.db` (SQLite) — aggregate queries:
  - `SELECT status, COUNT(*) FROM items GROUP BY status`
  - `SELECT to_status, COUNT(*) FROM transitions GROUP BY to_status`
  - Per-item working-entry counts (retry hotspots):
    `SELECT i.title, COUNT(*) FROM transitions t JOIN items i ON t.item_id=i.id WHERE t.to_status='working' GROUP BY i.id HAVING COUNT(*)>=3 ORDER BY 2 DESC`
  - Recent failure rows:
    `SELECT i.title, f.phase, f.error_sig, substr(f.error,1,80), datetime(f.at,'unixepoch','localtime') FROM failures f JOIN items i ON f.item_id=i.id ORDER BY f.at DESC`
  - Currently WORKING items with stale `at`:
    `SELECT id, title, (strftime('%s','now') - (SELECT MAX(at) FROM transitions WHERE item_id=items.id)) FROM items WHERE status='working'`
- `.agentor/transcripts/` — inventory (line counts, missing `"type":"result"`
  terminators = in-flight or killed). Don't read full transcripts into
  context; instead, **tail the last ~64KB of each** and parse the terminal
  `"type":"result"` event for per-run usage/cost/turns. The rest of the
  transcript is only useful when investigating a specific failure row.
- `docs/agent-logs/*.md` — **main-branch copies are deleted after folding**.
  To get the full set, collect from live worktrees under
  `.agentor/worktrees/*/docs/agent-logs/` and de-duplicate by basename.
  Also check `git log --all --diff-filter=A --name-only -- 'docs/agent-logs/*.md'`
  for the complete historical inventory.
- `docs/backlog/` — existing backlog filenames (to dedupe before filing).
- `docs/IMPROVEMENTS.md` — running out-of-scope log (to dedupe and flag
  stale entries against merged fixes).

## Phase 3 — Analyse

Produce these signals:

### 3a. Failure taxonomy

Group `failed:` events by error class. Expected classes:

- `claude killed: agentor shutdown` — operator Ctrl-C; benign.
- `claude exited 1: No conversation found with session ID <uuid>` — stale
  CLI session on restart; recovery re-queued but `--resume` refused.
- `claude exited 1: <truncated-JSON>` — CLI exited non-zero despite
  emitting a result envelope; often a race or CLI internal error.
- `do_work: ... timeout` — turn cap or wall-clock cap.
- Other — note explicitly.

### 3b. Retry hotspots

Items re-entering WORKING ≥5×. Correlate each with conflicts (count in
`transitions` where `to_status='conflicted'`) and session-loss failures.
These are candidates for `retry-transient-claude-errors` or
`inject-turn-budget-checkpoint-prompts` adjacency.

### 3c. Token usage / cost

For every `.agentor/transcripts/*.log`, parse the terminal `"type":"result"`
JSON event (tail the last ~64KB — don't read the whole file). Extract:

- `usage.input_tokens` (fresh / not cached)
- `usage.cache_creation_input_tokens`
- `usage.cache_read_input_tokens`
- `usage.output_tokens`
- `num_turns`
- `total_cost_usd`

Python snippet (runs standalone; no deps):

```python
import json, pathlib, re
td = pathlib.Path('.agentor/transcripts')
for f in sorted(td.glob('*.log')):
    m = re.match(r'([0-9a-f]+)\.(plan|execute)\.log', f.name)
    if not m: continue
    with f.open('rb') as fh:
        fh.seek(0,2); size=fh.tell()
        fh.seek(max(0,size-65536))
        tail = fh.read().decode('utf-8','replace')
    last = None
    for line in tail.splitlines():
        if '"type":"result"' in line:
            try: last = json.loads(line); break
            except: pass
    # last.get('usage',{}), last.get('num_turns'), last.get('total_cost_usd')
```

Report:

- **Totals** per phase (plan vs execute) and grand total: runs, turns,
  input, cache-create, cache-read, output, cost.
- **Cache efficiency**: `cache_read / (input + cache_create + cache_read)`.
  Healthy agentor runs clear 90%+; anything below 80% signals a cache
  miss (system-prompt drift, cache TTL lapse between plan→execute, or
  per-run prompt instability).
- **Cost by item status** (merged vs awaiting_review vs working vs orphan).
  Orphans are transcripts whose item_id no longer exists in state.db —
  rejected/deleted rows; worth noting but not billed to current backlog.
- **Retry tax**: split cost between items that re-entered WORKING ≥3×
  vs <3×. Compute the rework premium ($/item). This is the primary
  signal for `retry-transient-claude-errors`-class backlog.
- **Turn distribution** per phase (min/median/mean/max). Flag outliers
  ≥2× the phase's mean — these are the primary signal for
  `inject-turn-budget-checkpoint-prompts`-class backlog.
- **Top-N spend offenders**: items sorted by cost, showing plans/execs,
  turns, cache_read, and current status. Cross-reference with retry
  hotspots from 3b — expensive AND retry-heavy items are the priority
  targets.

### 3d. Cross-cutting themes from reflect items

For each unique agent-log, extract **Surprises / Gotchas / Follow-ups /
Stop if** sections. Then surface:

- Themes flagged in ≥3 reflections (prior-run gotchas worth codifying).
- Pre-existing debt mentioned in ≥2 Follow-ups (latent backlog candidates).
- Stop-if tripwires — note them as **regression guards**; they belong in
  CLAUDE.md if not already there, not backlog.

### 3e. Candidate backlog items

Diff "Follow-ups" across all reflect items against `docs/backlog/` and
`docs/IMPROVEMENTS.md`. For each open follow-up, classify:

- **Fileable** — bug, UX, cleanup, or small feature with clear scope.
- **Defer — wait-for-signal** — only matters under a condition that
  hasn't occurred (e.g. "rename .execute.log before 2nd kill-resume").
- **Not backlog** — external coordination, infra question, operator
  preference, or author-flagged out-of-scope with no adjacency.

## Phase 4 — Present

Output three tables, in this order:

**Table A — failure/retry snapshot** (compact summary, no action required):
columns = class, count, exemplar item, note.

**Table B — token spend** (compact summary, no action required):
- Totals row per phase (plan / execute / total).
- Cache-hit ratio line (one number, bold if <80%).
- Retry tax: `$X of $Y (Z%) in items with ≥3 working-entries`.
- Top 5 spend-offender items with cost + turn-count + status.
- Any turn-count outlier (≥2× phase mean) called out by item id.

**Table C — candidate backlog items**:
columns = #, Title, Source reflection, Type, Recommend?, Why / Why not.

When token signals point at an *existing* backlog item (e.g.
`inject-turn-budget-checkpoint-prompts`, `retry-transient-claude-errors`),
say so explicitly in the summary — don't file a duplicate, mark it as
"validated by current data" with the specific numbers as evidence.

Ask the user which rows from Table B to file (they may cherry-pick or
reject the whole set). If they accept, proceed to Phase 5.

## Phase 5 — Apply (with approval)

For each approved item:

1. Write `docs/backlog/<slug>.md` with this frontmatter:
   ```markdown
   ---
   title: <concise title, matches row>
   state: available
   category: <bug | ux | cleanup | feature | test-coverage | refactoring>
   ---

   <1–3 short paragraphs: context, scope, verification. Reference the
   source reflection path so future runs can trace provenance.>
   ```
2. Don't duplicate titles already in `docs/backlog/`. If a near-match
   exists, flag and ask before filing.
3. Update stale `docs/IMPROVEMENTS.md` entries (mark resolved / remove)
   only if the user approved that housekeeping separately.

Then write the review record to `.agentor/analyses/YYYY-MM-DD-<slug>.md`
using this structure:

```markdown
# <title> — <YYYY-MM-DD>

## Inputs

- Cut-off timestamp (state.db mtime + newest transcript time).
- Files / tables inspected (list them explicitly).

## Findings

- Failure taxonomy (counts per class).
- Retry hotspots (≥5× WORKING entries).
- Token totals (phase + grand total), cache-hit ratio, retry tax, top
  spend offenders.
- Cross-cutting themes from reflect items.

## Actions taken

- Backlog items filed (list + reason).
- Items rejected (list + reason — saves a future pass reconsidering them).
- Housekeeping (IMPROVEMENTS.md edits, CLAUDE.md refinements).

## Skip until

- Precise triggers that would justify re-running this analysis. Examples:
  - `>N new failed: lines of an unseen class`
  - `≥N new agent-logs added to docs/agent-logs/`
  - `<named backlog item> merges` (would invalidate the failure surface).
  - `cache-hit ratio drops below 90%` (cache strategy broke)
  - `cumulative spend since cut-off exceeds $X` (burn-rate check)
  - `any run exceeds 2× phase-mean turns` (turn-budget regression)
```

The **Skip until** block is the contract Phase 1 reads next time. Be
specific — vague triggers ("if anything interesting happens") defeat the
whole mechanism.

## Notes

- `.agentor/` is gitignored on agentor-instrumented repos, so the analysis
  record is local operator state — it travels with the checkout, not the
  repo.
- Don't read full transcripts into context. The daemon log + state.db
  aggregates are enough signal; transcripts are only useful when
  investigating a specific failure row, and should be tail/grep'd in a
  sub-tool call.
- Don't re-summarise reflect items the user has already folded into
  CLAUDE.md — check CLAUDE.md first to avoid a "same gotcha twice" pass.
