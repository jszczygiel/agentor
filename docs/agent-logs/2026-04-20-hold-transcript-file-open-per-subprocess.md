# Hold transcript file open per subprocess — 2026-04-20

## Gotchas for future runs
- `_run_stream_json_subprocess` now keeps one file handle open for the whole read loop; any new callback that wants to read the transcript mid-run relies on `fh.flush()` after every `fh.write(line)`. Batching writes for perf would silently break `dashboard/transcript.py::iter_events` live-tail — the new regression test `test_transcript_flushed_before_event_callback` pins this contract.
- Keep the args-banner and stderr-tail `with transcript_path.open("a")` blocks short-lived. They fire once per run and must close before/after the hot loop's handle to avoid interleaved writes on the same fd offset.

## Outcome
- Files touched: `agentor/runner.py`, `tests/test_runner.py`, `docs/backlog/hold-transcript-file-open-per-subprocess.md` (deleted).
- Tests added: `tests.test_runner.TestRunStreamJsonSubprocess.test_transcript_flushed_before_event_callback` — asserts the first event's callback already sees its JSON line on disk while the second line is still pending.
