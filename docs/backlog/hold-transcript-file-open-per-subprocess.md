---
title: Hold transcript file open per subprocess instead of reopen-per-line
state: available
tags: [perf, runner]
---

`_run_stream_json_subprocess` in `agentor/runner.py:776` reopens the
transcript file for every stream-json line from the claude/codex child:

```python
for line in iter(p.stdout.readline, ""):
    stdout_buf.append(line)
    with transcript_path.open("a") as fh:
        fh.write(line)
```

At typical agent event cadence (tens of events/sec during active tool use)
and with multiple live workers in the pool, this is the largest syscall
sink in the daemon — open + fstat + write + close per event line.

Fix: open the transcript once (`fh = transcript_path.open("a")`) before
the read loop, `fh.write(line); fh.flush()` per line (flush preserves the
live-tail guarantee that `dashboard/transcript.py:iter_events` relies on),
close in the same `finally:` block that already handles `p.stdout.close()`
and the stderr-drain thread join (`runner.py:797-811`).

Keep the existing `with transcript_path.open("a")` blocks for the
one-shot writes at `runner.py:773` (args banner) and `runner.py:815`
(stderr tail + exit code) — those fire once per run and aren't hot.

Validate with `tests/test_runner_stream_subprocess.py` (exists; covers
transcript framing) plus a quick manual run against a noisy agent to
eyeball Activity Monitor CPU.
