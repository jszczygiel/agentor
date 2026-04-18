import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.dashboard.transcript import (
    _TAIL_BYTES,
    _session_activity,
    _tail_lines,
)
from agentor.transcript import iter_raw_events


def _write_large_transcript(path: Path, n_events: int) -> None:
    """Stream n_events JSON lines to `path`. The last one is a RunResult so
    callers can distinguish 'we read the tail' from 'we read nothing'."""
    with path.open("w", encoding="utf-8") as fh:
        fh.write("args: ['claude']\n\nstdout:\n")
        for i in range(n_events - 1):
            fh.write(json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": f"step {i}"},
                    ],
                },
            }) + "\n")
        fh.write(json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "all done",
        }) + "\n")


class TestTailReads(unittest.TestCase):
    """Inspect view calls these helpers once per second on live transcripts
    that routinely exceed 10MB. Anything that scales with file size here
    freezes the curses main thread — the bug this test guards against."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.path = Path(self.td.name) / "big.log"

    def tearDown(self):
        self.td.cleanup()

    def test_session_activity_reads_tail_not_whole_file(self):
        # 50k events — well over 1MB, enough that a whole-file read would
        # dwarf the 100ms budget on slow disks. We don't assert a hard time
        # because CI hardware is inconsistent; instead assert the output
        # is bounded and the function completes promptly.
        _write_large_transcript(self.path, 50_000)
        size = self.path.stat().st_size
        self.assertGreater(size, _TAIL_BYTES,
                           "fixture must exceed tail window")

        t0 = time.monotonic()
        out = _session_activity(self.path, limit=25)
        elapsed = time.monotonic() - t0

        self.assertLessEqual(len(out), 25)
        self.assertLess(elapsed, 1.0,
                        f"tail-bounded read should not take {elapsed:.2f}s")
        # Last event is the RunResult — which shows up in the activity
        # feed as a `=` line. Confirms we actually saw the end of file.
        self.assertTrue(any(line.startswith("=  ") for line in out),
                        f"expected RunResult in tail, got: {out}")

    def test_tail_lines_caps_at_tail_bytes(self):
        # 20k short lines ≈ a couple hundred KB; just enough to exceed the
        # tail window so we exercise the seek-and-trim branch.
        with self.path.open("w", encoding="utf-8") as fh:
            for i in range(200_000):
                fh.write(f"line {i}\n")
        size = self.path.stat().st_size
        self.assertGreater(size, _TAIL_BYTES)

        out = _tail_lines(self.path, limit=12)
        self.assertEqual(len(out), 12)
        # Must contain the tail of the file, not the head.
        last = out[-1]
        self.assertTrue(last.startswith("line 199"),
                        f"expected tail of file, got {last!r}")

    def test_tail_lines_small_file_preserved(self):
        with self.path.open("w", encoding="utf-8") as fh:
            fh.write("only\nlines\nhere\n")
        out = _tail_lines(self.path, limit=10)
        self.assertEqual(out, ["only", "lines", "here"])

    def test_iter_raw_events_tail_drops_partial_first_line(self):
        # Build a file where a full JSON event straddles the tail boundary;
        # the tail read should drop that truncated line rather than emit a
        # broken dict.
        full_line = json.dumps({"type": "assistant", "message": {}}) + "\n"
        pad = full_line * 2000  # ~50KB of valid events, well past the tail
        partial_prefix = '{"type": "assistant", "message": {"content":'
        boundary = partial_prefix + full_line + pad
        self.path.write_text(boundary, encoding="utf-8")

        # Use a tail window that lands inside the partial prefix.
        events = list(iter_raw_events(self.path, tail_bytes=len(pad) + 10))
        # No crash, no stray dict from the partial line.
        for ev in events:
            self.assertIsInstance(ev, dict)
        self.assertGreater(len(events), 0)


if __name__ == "__main__":
    unittest.main()
