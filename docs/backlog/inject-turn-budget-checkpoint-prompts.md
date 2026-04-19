---
title: Inject turn-budget checkpoint prompts mid-run
category: feature
state: available
---

A 2026-04-19 review of `/Users/szczygiel/StudioProjects/lancelot/.agentor/transcripts/` (93 logs, 48 sessions, $187.21 total) found that a small tail of long sessions dominates cost. Five sessions burned 592 exec turns and ~$46 — a quarter of the whole spend:

| Session         | Exec turns | Cost    |
|-----------------|-----------:|--------:|
| a1898a95e9f8    | 153        | $16.37  |
| 07340a3f34a9    | 116        | $6.12   |
| a37f53d7a735    | 112        | $9.80   |
| d0966d8e680f    | 106        | $3.90   |
| e9f42af532aa    | 105        | $10.09  |

Inspection shows the late turns are typically *discovery* (find caller, find sibling test, find matching enum) that could live in a throwaway subagent context instead of the main one. Every extra main-context turn pays the full cached-prefix price.

Add a **mid-run checkpoint injector** in the runner. When the parsed stream-json turn count crosses a threshold, inject a user-role message ahead of the next agent turn:

- Turn ~60 (soft): *"You're at 60 turns. If you still need to discover call sites, file locations, or test patterns, delegate that to an `Explore` or `general-purpose` subagent — its context is separate and doesn't bill against this session. Otherwise, confirm you're closing out."*
- Turn ~100 (hard nudge): *"You're at 100 turns. State in one sentence what's blocking closeout, then either finish or delegate."*

Not a kill — purely advisory messages. Let the agent continue if it judges the turn budget worth spending.

Implementation notes:

- Fires on a counter derived from the same stream-json transcript the dashboard already parses (`agentor/dashboard.py:_session_activity`). Shared parsing lives in the deduplicated transcript module once `deduplicate-transcript-parsing.md` lands — prefer building on that.
- Thresholds configurable via `agent.turn_checkpoint_soft` / `agent.turn_checkpoint_hard`; default on but easy to disable (`0` → off).
- Inject exactly once per threshold per run — don't re-nudge every turn after crossing.
- Consider an alternative signal: cumulative **output tokens** correlate better with "doing too much in-context" than turn count (a37f53d7a735 hit 54K output tokens, the batch's max). Could gate on either, whichever trips first.

Verification:

- Unit test: feed a synthetic transcript with 70 assistant turns into the checkpoint logic; assert exactly one soft-threshold injection payload is emitted.
- End-to-end: replay one of the 100+ turn sessions with checkpoints enabled in a dry-run mode; log where the injections would have landed.

Risk: false positives on legitimately long refactors. The nudges are soft, so this is acceptable; track whether agents actually delegate after the nudge and drop the soft threshold if uptake is zero.

Evidence file: `/Users/szczygiel/StudioProjects/lancelot/tmp/agentor_analysis_2026-04-19.md`.
