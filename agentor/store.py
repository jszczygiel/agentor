import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import __version__ as AGENTOR_VERSION
from .models import Item, ItemStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    source_line   INTEGER NOT NULL,
    tags_json     TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL,
    worktree_path TEXT,
    branch        TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    feedback      TEXT,
    result_json   TEXT,
    session_id    TEXT,
    agentor_version TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE TABLE IF NOT EXISTS transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL REFERENCES items(id),
    from_status TEXT,
    to_status  TEXT NOT NULL,
    note       TEXT,
    at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            TEXT NOT NULL REFERENCES items(id),
    attempt            INTEGER NOT NULL,
    phase              TEXT,
    error              TEXT NOT NULL,
    error_sig          TEXT,
    num_turns          INTEGER,
    duration_ms        INTEGER,
    files_changed_json TEXT,
    transcript_path    TEXT,
    at                 REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_item ON failures(item_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent migrations for DBs created before new columns existed."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "session_id" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN session_id TEXT")
    if "feedback" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN feedback TEXT")
    if "agentor_version" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN agentor_version TEXT")


def _encode_status(status: ItemStatus) -> str:
    """Serialize an ItemStatus for storage in SQLite.

    Every write that crosses the DB boundary must go through this helper so
    the encoding lives in one place; swapping the on-disk representation
    later (e.g. to an int enum) is then a single-file change."""
    return status.value


def _decode_status(raw: str) -> ItemStatus:
    """Deserialize a status string read from SQLite back into an ItemStatus.

    Raises ValueError if `raw` is not a known status — the daemon surfaces
    unknown statuses loudly rather than silently coercing them."""
    return ItemStatus(raw)


@dataclass
class Transition:
    """A row from the `transitions` table, with status fields decoded back
    into ItemStatus enums so callers never touch raw strings."""
    from_status: ItemStatus | None
    to_status: ItemStatus
    note: str | None
    at: float


@dataclass
class StoredItem:
    id: str
    title: str
    body: str
    source_file: str
    source_line: int
    tags: dict[str, str]
    status: ItemStatus
    worktree_path: str | None
    branch: str | None
    attempts: int
    last_error: str | None
    feedback: str | None
    result_json: str | None
    session_id: str | None
    agentor_version: str | None
    created_at: float
    updated_at: float


def _row_to_stored(row: sqlite3.Row) -> StoredItem:
    return StoredItem(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        source_file=row["source_file"],
        source_line=row["source_line"],
        tags=json.loads(row["tags_json"]),
        status=_decode_status(row["status"]),
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        feedback=row["feedback"],
        result_json=row["result_json"],
        session_id=row["session_id"],
        agentor_version=row["agentor_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Store:
    """SQLite-backed state store. One connection per Store instance.

    Not thread-safe — the daemon should own a single Store and serialize access,
    or open separate Store instances per thread (sqlite3 connections aren't
    shareable across threads by default).
    """

    # --- lifecycle ---

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        _migrate(self.conn)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # --- ingestion ---

    def upsert_discovered(self, item: Item) -> bool:
        """Insert an item seen in a source file if not already present.
        Items start in BACKLOG — the caller (scan_once + config) decides
        whether to auto-promote them to QUEUED.
        Returns True if this was a new item, False if it already existed."""
        now = time.time()
        cur = self.conn.execute(
            "SELECT id FROM items WHERE id = ?", (item.id,)
        )
        if cur.fetchone() is not None:
            return False
        with self.tx() as c:
            c.execute(
                """INSERT INTO items
                   (id, title, body, source_file, source_line, tags_json,
                    status, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    item.id, item.title, item.body, item.source_file,
                    item.source_line, json.dumps(item.tags),
                    _encode_status(ItemStatus.BACKLOG), now, now,
                ),
            )
            c.execute(
                """INSERT INTO transitions (item_id, from_status, to_status, at)
                   VALUES (?, NULL, ?, ?)""",
                (item.id, _encode_status(ItemStatus.BACKLOG), now),
            )
        return True

    # --- reads ---

    def get(self, item_id: str) -> StoredItem | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        return _row_to_stored(row) if row else None

    def list_by_status(self, status: ItemStatus) -> list[StoredItem]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM items WHERE status = ? ORDER BY created_at",
                (_encode_status(status),),
            ).fetchall()
        return [_row_to_stored(r) for r in rows]

    def count_by_status(self, status: ItemStatus) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE status = ?",
                (_encode_status(status),),
            ).fetchone()
        return row["n"]

    def ids_with_errors(self, statuses: list[ItemStatus] | None = None,
                        ) -> set[str]:
        """Return the set of item ids where `last_error` is non-null,
        optionally filtered to a subset of statuses. Used by the dashboard
        error filter and the `!` marker in rows."""
        params: list[object] = []
        sql = "SELECT id FROM items WHERE last_error IS NOT NULL"
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(_encode_status(s) for s in statuses)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return {r["id"] for r in rows}

    # --- queue / dispatch ---

    def claim_next_queued(self, worktree_path: str, branch: str) -> StoredItem | None:
        """Atomically pick the oldest queued item and mark it working.
        Returns the claimed item or None if the queue is empty.
        Caller enforces the pool cap via pool_has_slot()."""
        queued = _encode_status(ItemStatus.QUEUED)
        working = _encode_status(ItemStatus.WORKING)
        with self.tx() as c:
            row = c.execute(
                """SELECT id FROM items
                   WHERE status = ?
                   ORDER BY created_at
                   LIMIT 1""",
                (queued,),
            ).fetchone()
            if row is None:
                return None
            item_id = row["id"]
            now = time.time()
            c.execute(
                """UPDATE items
                   SET status = ?, worktree_path = ?, branch = ?,
                       attempts = attempts + 1, agentor_version = ?,
                       updated_at = ?
                   WHERE id = ? AND status = ?""",
                (working, worktree_path, branch,
                 AGENTOR_VERSION, now,
                 item_id, queued),
            )
            c.execute(
                """INSERT INTO transitions (item_id, from_status, to_status, at)
                   VALUES (?, ?, ?, ?)""",
                (item_id, queued, working, now),
            )
        return self.get(item_id)

    def pool_has_slot(self, pool_size: int) -> bool:
        return self.count_by_status(ItemStatus.WORKING) < pool_size

    # --- transitions ---

    def transition(
        self,
        item_id: str,
        to: ItemStatus,
        note: str | None = None,
        **fields: object,
    ) -> None:
        """Transition an item to a new status and optionally update columns
        like worktree_path, branch, attempts, last_error, result_json."""
        now = time.time()
        allowed = {"worktree_path", "branch", "attempts", "last_error",
                   "feedback", "result_json", "session_id"}
        sets = ["status = ?", "updated_at = ?"]
        params: list[object] = [_encode_status(to), now]
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update field: {k}")
            sets.append(f"{k} = ?")
            params.append(v)
        params.append(item_id)

        with self.tx() as c:
            cur = c.execute(
                "SELECT status FROM items WHERE id = ?", (item_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"no such item: {item_id}")
            from_status = row["status"]
            c.execute(
                f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params
            )
            c.execute(
                """INSERT INTO transitions (item_id, from_status, to_status, note, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (item_id, from_status, _encode_status(to), note, now),
            )

    def update_result_json(self, item_id: str, blob: str) -> None:
        """Write a fresh result_json for an item WITHOUT recording a status
        transition. Used by the streaming claude runner to publish live
        usage/cost/iterations data mid-run so the dashboard reflects the
        current state without waiting for the phase to end."""
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE items SET result_json = ?, updated_at = ? WHERE id = ?",
                (blob, now, item_id),
            )

    # --- history ---

    def transitions_for(self, item_id: str) -> list[Transition]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT from_status, to_status, note, at
                   FROM transitions WHERE item_id = ? ORDER BY id""",
                (item_id,),
            ).fetchall()
        return [
            Transition(
                from_status=_decode_status(r["from_status"]) if r["from_status"] else None,
                to_status=_decode_status(r["to_status"]),
                note=r["note"],
                at=r["at"],
            )
            for r in rows
        ]

    def recent_failure_notes(self, item_id: str, n: int = 3) -> list[str]:
        """Return the most recent N transition notes whose from_status was
        WORKING and to_status was QUEUED — i.e. the "bounce back after a
        do_work failure" transitions. Used by the runner to detect when
        the same error has fired multiple attempts in a row (a loop) so
        it can auto-revert instead of reject."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT note FROM transitions
                   WHERE item_id = ? AND from_status = ? AND to_status = ?
                   ORDER BY id DESC LIMIT ?""",
                (item_id, _encode_status(ItemStatus.WORKING),
                 _encode_status(ItemStatus.QUEUED), n),
            ).fetchall()
        return [r["note"] or "" for r in rows]

    def previous_settled_status(self, item_id: str) -> ItemStatus | None:
        """Find the most recent "settled" status this item was in, other
        than the current one. Settled = anything except WORKING (which is
        a transient in-flight state). Returns None if no prior settled
        state exists.

        Used by the manual revert command and by crash recovery to restore
        an item without losing user-visible progress — e.g. an item that
        had reached AWAITING_PLAN_REVIEW, then was re-queued for execute,
        then crashed mid-work, gets restored to QUEUED (the execute-phase
        wait, with the approved plan still in result_json) rather than
        starting from BACKLOG. For a fresh first-time crash, this returns
        QUEUED. For a rejection cascade, it skips the WORKING bounces and
        returns the QUEUED state that preceded them."""
        rows = self.transitions_for(item_id)
        if len(rows) < 2:
            return None
        current = rows[-1].to_status
        for row in reversed(rows[:-1]):
            to = row.to_status
            if to == ItemStatus.WORKING or to == current:
                continue
            return to
        return None

    # --- failures ---

    def record_failure(
        self,
        item_id: str,
        attempt: int,
        phase: str | None,
        error: str,
        error_sig: str | None = None,
        num_turns: int | None = None,
        duration_ms: int | None = None,
        files_changed: list[str] | None = None,
        transcript_path: str | None = None,
    ) -> None:
        """Persist one failure attempt. Separate table from transitions so
        rich diagnostics (turns, duration, files touched, transcript
        pointer) survive across the next attempt's bounce-back, which
        would otherwise overwrite `last_error` on the item."""
        now = time.time()
        files_json = json.dumps(files_changed) if files_changed else None
        with self._lock:
            self.conn.execute(
                """INSERT INTO failures
                   (item_id, attempt, phase, error, error_sig, num_turns,
                    duration_ms, files_changed_json, transcript_path, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, attempt, phase, error, error_sig, num_turns,
                 duration_ms, files_json, transcript_path, now),
            )

    def list_failures(self, item_id: str, limit: int = 20) -> list[dict]:
        """Recent failures for an item, newest first."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM failures WHERE item_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (item_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_failures(self, item_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM failures WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return row["n"] if row else 0

    def note_infra_failure(self, item_id: str, err: str) -> None:
        """Record an infrastructure-level failure (broken worktree, missing
        repo, etc.) without changing item status or charging an attempt.

        Decrements attempts to undo the increment claim_next_queued did at
        dispatch time — the failure isn't the item's fault, so it shouldn't
        burn a retry slot. Writes a self-loop transition row (from==to)
        with a marker note so the history reflects what happened."""
        now = time.time()
        with self.tx() as c:
            row = c.execute(
                "SELECT status, attempts FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no such item: {item_id}")
            cur_status = row["status"]
            new_attempts = max(0, int(row["attempts"]) - 1)
            c.execute(
                "UPDATE items SET attempts = ?, last_error = ?, "
                "updated_at = ? WHERE id = ?",
                (new_attempts, err, now, item_id),
            )
            note = f"infra failure (no attempt charged): {err[:300]}"
            c.execute(
                """INSERT INTO transitions
                   (item_id, from_status, to_status, note, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (item_id, cur_status, cur_status, note, now),
            )

    # --- recovery ---

    def clear_error_and_reset_attempts(self, item_id: str) -> None:
        """Clear last_error and zero the attempts counter without moving
        status. Used by the startup auto-recovery sweep for items whose
        error is known benign (operator ^C, obsolete cap, recoverable
        session loss) — they should re-enter normal dispatch as if fresh."""
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE items SET last_error = NULL, attempts = 0, "
                "updated_at = ? WHERE id = ?",
                (now, item_id),
            )
