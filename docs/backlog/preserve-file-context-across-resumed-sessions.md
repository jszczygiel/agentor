---
title: Preserve file-seen context across resumed sessions
category: feature
state: available
---

Session `d0966d8e680f.execute` (Drifting Cloud Overlay, lancelot) logged the worst re-read ever recorded: `scripts/main/game_world.gd` Read **14 times** in a single run, with the full 1,291-line file dumped into context twice. Root cause visible in the log header:

```
REVIEWER FEEDBACK FROM A PREVIOUS REJECTED ATTEMPT:
do_work: claude killed: agentor shutdown
```

The agentor harness was killed mid-run and relaunched. The resumed agent got the *plan* and the *reviewer feedback* string, but nothing about which files the prior agent had already Read, Grep'd, or edited. So it did cold-start discovery on ground the prior agent had already covered — burning ~18k tokens on that one file alone.

Resume happens whenever the daemon is killed mid-work (laptop sleep, OOM, ctrl-c, `agentor shutdown`) and recovery (`agentor/recovery.py`) brings WORKING items back. The recovered agent currently inherits the session id (so Claude prompt cache may hit) but not a *structured* summary of prior investigation.

Add a **prior-run primer** to the resume-execute prompt. When a session is being resumed:

1. Parse the killed run's transcript (`agentor/transcript.py` once deduplicate-transcript-parsing.md lands).
2. Extract: files Read (with observed offset/limit ranges), Grep patterns that returned matches and which files they hit, files the agent edited.
3. Inject a primer block ahead of the existing resume prompt, for example:

```
## Prior run (killed: agentor shutdown) already investigated:

Files read end-to-end:
- scripts/main/game_world.gd
- scripts/autoload/wind_manager.gd

Files read partially:
- scripts/ui/hud.gd:120-180 (zoom handling)

Greps that mattered:
- "zoom_level" → hud.gd, camera.gd, game_world.gd
- "WIND_VECTOR" → wind_manager.gd only

Do NOT re-read these files unless you have specific reason. Their content
is still in your approved plan.
```

Cheaper alternative: forward the *entire* prior message history to the resumed run behind a "resumed-from" marker. The prompt cache stays warm from the prior run, so this is nearly free token-wise. Downside: carries forward whatever wrong turns the killed run made. Pick one approach explicitly.

Scope:

- Only primes *execute* resumes after a kill — plan→execute handoff already works and is approved by a human.
- Only fires when the killed run has meaningful tool history (≥3 assistant turns). Below that, just proceed fresh.
- Do not include prior Bash outputs in the primer — those were ephemeral observations, and their results may now be stale.

Verification:

- Unit test: feed a synthetic killed-run transcript with 10 Read calls and 5 Greps into the primer builder; assert the primer markdown lists all distinct files and grep patterns.
- End-to-end: kill a running agent mid-plan-execute, restart, confirm the primer appears in the new invocation's prompt and that the next tool call is NOT a Read of an already-seen file.

Pair with:

- `deduplicate-transcript-parsing.md` — the shared transcript parser should emit the structured events this primer consumes.
- The dashboard already parses stream-json; reusing that code avoids a second walk.

Evidence file: `/Users/szczygiel/StudioProjects/lancelot/tmp/agentor_analysis_2026-04-19.md`. The specific incident lives in `.agentor/transcripts/d0966d8e680f.execute.log` (lancelot).
