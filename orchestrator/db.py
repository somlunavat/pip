"""SQLite storage: one local file, WAL mode, UTC timestamps.

The eight tables follow docs/ARCHITECTURE.md ("Data model in SQLite"). Vectors are
float32 blobs; see orchestrator/vectors.py.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    user_text   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS subtasks (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    kind        TEXT NOT NULL,
    path        TEXT,
    worker_id   TEXT,
    session_id  TEXT,
    response_id TEXT,
    goal        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    task_id     TEXT,
    subtask_id  TEXT,
    event       TEXT NOT NULL,
    path        TEXT,
    latency_ms  REAL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    cost_usd    REAL,
    source      TEXT,
    status      TEXT,
    error_code  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON task_events(ts);

CREATE TABLE IF NOT EXISTS workers (
    instance_id    TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    in_flight      INTEGER NOT NULL DEFAULT 0,
    last_health_at TEXT
);

CREATE TABLE IF NOT EXISTS watchers (
    id         TEXT PRIMARY KEY,
    target     TEXT NOT NULL,
    extractor  TEXT NOT NULL,
    interval_s INTEGER NOT NULL,
    threshold  REAL NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    watcher_id   TEXT NOT NULL REFERENCES watchers(id),
    fields       TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_watcher ON snapshots(watcher_id, ts);

CREATE TABLE IF NOT EXISTS change_events (
    id          TEXT PRIMARY KEY,
    watcher_id  TEXT NOT NULL REFERENCES watchers(id),
    ts          TEXT NOT NULL,
    description TEXT NOT NULL,
    diff        TEXT NOT NULL,
    embedding   BLOB,
    score       REAL,
    alerted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_watcher ON change_events(watcher_id, ts);

CREATE TABLE IF NOT EXISTS preferences (
    user_id    TEXT PRIMARY KEY,
    vector     BLOB,
    updated_at TEXT NOT NULL,
    reactions  TEXT NOT NULL DEFAULT '[]'
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Database:
    """Thin, thread-safe wrapper over one sqlite3 connection.

    The orchestrator is a single asyncio process; every query here is short, so a
    lock around one connection is simpler than a pool and fast enough.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # -- tasks ---------------------------------------------------------------

    def create_task(self, user_text: str) -> str:
        task_id = new_id("task")
        self.execute(
            "INSERT INTO tasks (id, user_text, status, created_at) VALUES (?, ?, 'running', ?)",
            (task_id, user_text, utcnow()),
        )
        return task_id

    def finish_task(self, task_id: str, status: str) -> None:
        self.execute(
            "UPDATE tasks SET status = ?, finished_at = ? WHERE id = ?",
            (status, utcnow(), task_id),
        )

    def create_subtask(self, task_id: str, subtask_id: str, kind: str, goal: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO subtasks (id, task_id, kind, goal) VALUES (?, ?, ?, ?)",
            (subtask_id, task_id, kind, goal),
        )

    def update_subtask(self, subtask_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE subtasks SET {cols} WHERE id = ?", (*fields.values(), subtask_id))

    # -- events --------------------------------------------------------------

    def log_event(
        self,
        event: str,
        *,
        task_id: str | None = None,
        subtask_id: str | None = None,
        path: str | None = None,
        latency_ms: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        source: str | None = None,
        status: str = "ok",
        error_code: str | None = None,
    ) -> None:
        self.execute(
            """INSERT INTO task_events
               (ts, task_id, subtask_id, event, path, latency_ms, tokens_in, tokens_out,
                cost_usd, source, status, error_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                utcnow(), task_id, subtask_id, event, path, latency_ms, tokens_in,
                tokens_out, cost_usd, source, status, error_code,
            ),
        )

    def spend_since(self, since_iso: str) -> float:
        row = self.one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM task_events WHERE ts >= ?",
            (since_iso,),
        )
        return float(row["total"]) if row else 0.0

    # -- workers -------------------------------------------------------------

    def upsert_worker(self, instance_id: str, status: str, in_flight: int = 0) -> None:
        self.execute(
            """INSERT INTO workers (instance_id, status, in_flight, last_health_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(instance_id) DO UPDATE SET
                 status = excluded.status, in_flight = excluded.in_flight,
                 last_health_at = excluded.last_health_at""",
            (instance_id, status, in_flight, utcnow()),
        )

    # -- watchers / snapshots ------------------------------------------------

    def create_watcher(self, target: str, extractor: str, interval_s: int, threshold: float) -> str:
        watcher_id = new_id("w")
        self.execute(
            """INSERT INTO watchers (id, target, extractor, interval_s, threshold, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (watcher_id, target, extractor, interval_s, threshold, utcnow()),
        )
        return watcher_id

    def active_watchers(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM watchers WHERE active = 1")

    def last_snapshot(self, watcher_id: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM snapshots WHERE watcher_id = ? ORDER BY id DESC LIMIT 1",
            (watcher_id,),
        )

    def add_snapshot(self, watcher_id: str, fields: dict[str, Any], content_hash: str) -> None:
        self.execute(
            "INSERT INTO snapshots (ts, watcher_id, fields, content_hash) VALUES (?, ?, ?, ?)",
            (utcnow(), watcher_id, json.dumps(fields, sort_keys=True), content_hash),
        )

    def prune_snapshots(self, keep_days: int = 3) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        return self.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,)).rowcount
