"""Auto-queue a "fold agent log lessons" backlog item when `docs/agent-logs/`
has grown past a threshold.

The daemon calls `maybe_enqueue_fold_item` once per main-loop tick. It's a
cheap filesystem count + a DB title scan — no work happens unless the count
crosses `agent.fold_threshold`. The resulting backlog file is picked up by
the very next `scan_once`, so the fold work flows through the normal
review pipeline (no auto-merge of CLAUDE.md edits).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .config import Config
from .models import ItemStatus
from .store import Store


# Non-terminal statuses for the double-queue guard. MERGED/REJECTED/
# CANCELLED/ERRORED are terminal — a prior fold item in one of those
# should NOT block a new one from being queued.
_NON_TERMINAL_STATUSES: tuple[ItemStatus, ...] = (
    ItemStatus.QUEUED,
    ItemStatus.WORKING,
    ItemStatus.AWAITING_PLAN_REVIEW,
    ItemStatus.AWAITING_REVIEW,
    ItemStatus.APPROVED,
    ItemStatus.CONFLICTED,
    ItemStatus.DEFERRED,
)

_TITLE_PREFIX = "Fold agent log lessons"


def _existing_fold_item(store: Store) -> bool:
    for st in _NON_TERMINAL_STATUSES:
        for item in store.list_by_status(st):
            if item.title.startswith(_TITLE_PREFIX):
                return True
    return False


def _fold_body(log_paths: list[str], today: str) -> str:
    """Build the body for the auto-queued fold item. Lists the log files
    the agent should consider and spells out the expected output — the
    agent deletes the consumed logs as part of the same commit so the
    next tick's count resets."""
    lines = [
        f"Auto-generated on {today}. `docs/agent-logs/` has accumulated "
        f"{len(log_paths)} findings files; fold their durable lessons "
        "into CLAUDE.md (or the relevant skill file) and delete the "
        "consumed logs so the count resets.",
        "",
        "## Logs to consider",
        "",
    ]
    lines.extend(f"- `{p}`" for p in log_paths)
    lines.extend([
        "",
        "## Expected output (one commit)",
        "",
        "- A CLAUDE.md (and/or skills) diff that captures recurring "
        "Surprises / Gotchas — cluster, don't copy verbatim.",
        "- `git rm` on every log file listed above that you folded in. "
        "Keep anything still too raw to promote, but prefer to fold "
        "rather than hoard.",
        "- One commit containing both the docs update and the "
        "deletions. The normal review flow gates the merge — do not "
        "auto-merge CLAUDE.md changes.",
    ])
    return "\n".join(lines) + "\n"


def maybe_enqueue_fold_item(config: Config, store: Store) -> Path | None:
    """Create a `docs/backlog/fold-agent-lessons-YYYY-MM-DD.md` backlog
    item if `docs/agent-logs/` has enough files and no fold item is
    already in flight.

    Returns the path of the created (or pre-existing) backlog file, or
    None if no action was taken this tick. Safe to call every tick —
    idempotent on a same-day retry, and short-circuits cheaply when the
    threshold is unmet.
    """
    threshold = config.agent.fold_threshold
    if threshold <= 0:
        return None

    logs_dir = config.project_root / "docs" / "agent-logs"
    if not logs_dir.is_dir():
        return None

    log_files = sorted(p for p in logs_dir.glob("*.md") if p.is_file())
    if len(log_files) < threshold:
        return None

    if _existing_fold_item(store):
        return None

    backlog_dir = config.project_root / "docs" / "backlog"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    target = backlog_dir / f"fold-agent-lessons-{today}.md"

    # Same-day retry: if the file already exists but no non-terminal item
    # in the store tracks it yet (e.g. scan_once hasn't run), leave it be
    # — the next scan will pick it up. Prevents an in-flight rewrite.
    if target.exists():
        return target

    root = config.project_root.resolve()
    rel_paths = [
        str(p.resolve().relative_to(root)) for p in log_files
    ]
    body = _fold_body(rel_paths, today)
    frontmatter = (
        "---\n"
        f"title: Fold agent log lessons ({today})\n"
        "category: meta\n"
        "state: available\n"
        "---\n\n"
    )
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(frontmatter + body)
    tmp.replace(target)
    return target
