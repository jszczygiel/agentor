from pathlib import Path

from ..config import Config
from ..providers import detect_provider
from ..store import StoredItem

from .formatters import _phase_for


def _transcript_path_for(cfg: Config, item: StoredItem) -> Path:
    phase = _phase_for(item)
    if not phase:
        phase = "execute" if item.session_id else "plan"
    return (
        cfg.project_root / ".agentor" / "transcripts" / f"{item.id}.{phase}.log"
    )


# ~256KB of tail is enough to cover thousands of stream-json events and
# tens of thousands of raw log lines — far past the dashboard's render
# budget on anything but a pathological long line. Stays in RAM comfortably
# while capping the per-tick work at O(tail_bytes), not O(file_size).
_TAIL_BYTES = 256 * 1024


def _tail_lines(path: Path, limit: int = 12) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size <= _TAIL_BYTES:
                fh.seek(0)
            else:
                fh.seek(size - _TAIL_BYTES)
            data = fh.read()
    except FileNotFoundError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # Drop the first line when we seeked into the middle of the file — it's
    # almost certainly truncated at the seek boundary and would show up as
    # a garbled tail row.
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]
    return lines[-limit:]


def _session_activity(
    cfg: Config, path: Path, limit: int = 25,
) -> list[str]:
    """Render a compact activity feed for the inspect view.

    Provider-aware: sniffs the transcript header to pick the right parser
    (Claude stream-json vs. Codex JSONL) so a daemon `[M]` provider flip
    mid-run doesn't misparse an in-flight transcript written under the
    prior runner. The rendered strings are concatenated verbatim by the
    dashboard — it stays vendor-agnostic."""
    return detect_provider(cfg, path).activity_feed(path, limit)
