import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

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
    result_json   TEXT,
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
"""


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
    result_json: str | None
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
        status=ItemStatus(row["status"]),
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        result_json=row["result_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Store:
    """SQLite-backed state store. One connection per Store instance.

    Not thread-safe — the daemon should own a single Store and serialize access,
    or open separate Store instances per thread (sqlite3 connections aren't
    shareable across threads by default).
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
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

    def upsert_discovered(self, item: Item) -> bool:
        """Insert an item seen in a source file if not already present.
        Returns True if this was a new item (inserted), False if it already existed."""
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
                    ItemStatus.QUEUED.value, now, now,
                ),
            )
            c.execute(
                """INSERT INTO transitions (item_id, from_status, to_status, at)
                   VALUES (?, NULL, ?, ?)""",
                (item.id, ItemStatus.QUEUED.value, now),
            )
        return True

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
                (status.value,),
            ).fetchall()
        return [_row_to_stored(r) for r in rows]

    def count_by_status(self, status: ItemStatus) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE status = ?", (status.value,)
            ).fetchone()
        return row["n"]

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
        allowed = {"worktree_path", "branch", "attempts", "last_error", "result_json"}
        sets = ["status = ?", "updated_at = ?"]
        params: list[object] = [to.value, now]
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
                (item_id, from_status, to.value, note, now),
            )

    def claim_next_queued(self, worktree_path: str, branch: str) -> StoredItem | None:
        """Atomically pick the oldest queued item and mark it working.
        Returns the claimed item or None if the queue is empty.
        Caller enforces the pool cap via pool_has_slot()."""
        with self.tx() as c:
            row = c.execute(
                """SELECT id FROM items
                   WHERE status = ?
                   ORDER BY created_at
                   LIMIT 1""",
                (ItemStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            item_id = row["id"]
            now = time.time()
            c.execute(
                """UPDATE items
                   SET status = ?, worktree_path = ?, branch = ?,
                       attempts = attempts + 1, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (ItemStatus.WORKING.value, worktree_path, branch, now,
                 item_id, ItemStatus.QUEUED.value),
            )
            c.execute(
                """INSERT INTO transitions (item_id, from_status, to_status, at)
                   VALUES (?, ?, ?, ?)""",
                (item_id, ItemStatus.QUEUED.value, ItemStatus.WORKING.value, now),
            )
        return self.get(item_id)

    def pool_has_slot(self, pool_size: int) -> bool:
        return self.count_by_status(ItemStatus.WORKING) < pool_size

    def transitions_for(self, item_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT from_status, to_status, note, at
                   FROM transitions WHERE item_id = ? ORDER BY id""",
                (item_id,),
            ).fetchall()
        return [dict(r) for r in rows]
