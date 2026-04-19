# Preserve file-seen context across resumed sessions — 2026-04-19

## Surprises
- `.execute.log` is overwritten on every run start by `_run_stream_json_subprocess` (`transcript_path.write_text(...)`). So the primer MUST be built before `_invoke_claude` launches the subprocess — placed inside `_do_execute`, not inside the subprocess helper.
- `_invoke_claude` sets `phase_tag = "execute" if had_session else "plan"`, which conflates "killed plan resume" with "execute". For this task we only inject the primer in the `_do_execute` branch, so killed-plan resumes are not primed (matches scope).

## Gotchas for future runs
- Prior-run-transcript detection: plan→execute handoff writes `{item.id}.plan.log`, never `.execute.log`. A killed-execute resume is the only natural way for `.execute.log` to exist at `_do_execute` entry. Handy distinguisher; record this in CLAUDE.md if the primer grows new triggers.
- The shared `iter_events` parser tolerates truncated final lines (tail/seek behaviour) — no extra robustness needed in the primer even for killed-mid-write logs.

## Follow-ups
- Successive kill-resumes lose the primer from the oldest run: each subprocess launch overwrites the transcript, so by the 2nd kill the primer only summarises the 1st resume. Mitigation (rename `.execute.log` → `.execute.prior-N.log` before overwrite) is out of scope here; noted in docs/IMPROVEMENTS.md if it becomes a repeat issue.
- Codex kill-resume is not primed — Codex's JSONL shape and thread_id model differ; revisit if we start running Codex at scale.

## Stop if
- `iter_events` starts erroring mid-stream for real transcripts — would indicate a deeper transcript-format drift that should be fixed in `agentor/transcript.py` before any primer work.
