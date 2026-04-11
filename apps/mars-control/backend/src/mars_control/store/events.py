"""SQLite store for Mars events received from the runtime.

Scope (Story 2.2):

* Schema: ``events(id, session_id, sequence, type, data_json,
  received_at)`` — one row per durable event. Ephemeral events
  (``assistant_chunk``, ``tool_started``) are NEVER persisted; they
  flow through SSE fan-out only (Story 2.3).
* WAL mode on file-backed databases — without it, concurrent ingests
  + reads lock-contend under load. Skipped for ``:memory:`` because
  WAL requires a real file.
* Single writer, sync ``sqlite3`` wrapped in ``asyncio.to_thread`` so
  the FastAPI handler stays non-blocking. No ``aiosqlite`` dependency.

Out of scope (deferred):

* Sequence-number assignment at the store layer — for v1 the supervisor
  is expected to stamp sequences before forwarding. The store records
  whatever sequence the ingest hands it (including ``None``) and orders
  playback by insertion id instead.
* Redis-backed fan-out for multi-node control planes — v1 is single
  host, one process, one SQLite file.
* ``Last-Event-ID`` replay — Story 2.3 or later will use ``id`` as the
  resume cursor; the index is already in place.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from events.types import DURABLE_EVENT_TYPES

__all__ = ["EventStore", "StoredEvent"]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    sequence    INTEGER,
    type        TEXT    NOT NULL,
    data_json   TEXT    NOT NULL,
    received_at TEXT    NOT NULL
)
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS events_session_idx
ON events(session_id, id)
"""


class StoredEvent(dict):
    """Typed dict shape returned by :meth:`EventStore.get_session_events`."""

    # Intentionally a plain dict subclass so TestClient responses can
    # compare directly against dicts. The fields are:
    #   id         : int — autoincrement primary key, ordering cursor
    #   session_id : str
    #   sequence   : int | None
    #   type       : str
    #   data       : dict — the full MarsEvent JSON payload
    #   received_at: str — ISO-8601 UTC timestamp when ingest wrote the row


class EventStore:
    """Durable event persistence for the control plane.

    One instance per process. ``init()`` is idempotent; ``close()`` is
    idempotent. The store may be constructed without immediate
    initialization so :func:`mars_control.api.routes.create_control_app`
    can inject a pre-built store into tests without triggering the
    lifespan hook twice.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._conn: sqlite3.Connection | None = None
        #: Serializes writes + reads against the shared sqlite3 connection.
        #: ``check_same_thread=False`` lets us call from asyncio.to_thread
        #: workers, but the connection itself is not safe for *concurrent*
        #: use. WAL helps contention across *connections*, not across
        #: concurrent calls on the same connection.
        self._db_lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_initialized(self) -> bool:
        return self._conn is not None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Create the connection + schema. Safe to call multiple times."""
        if self._conn is not None:
            return
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,  # autocommit mode
            check_same_thread=False,
        )
        if self._path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                # Older file without WAL support: log + continue.
                pass
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_SCHEMA_SQL)
        self._conn.execute(_INDEX_SQL)

    def close(self) -> None:
        """Close the connection. Safe to call multiple times."""
        if self._conn is None:
            return
        try:
            self._conn.close()
        finally:
            self._conn = None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def write_batch(self, events: list[dict[str, Any]]) -> int:
        """Persist the durable subset of ``events``.

        Ephemeral events (``assistant_chunk``, ``tool_started``) are
        filtered out here — they do not belong in the durable log.
        Returns the number of rows actually inserted.
        """
        if not events:
            return 0
        durables = [e for e in events if e.get("type") in DURABLE_EVENT_TYPES]
        if not durables:
            return 0
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                e.get("session_id", ""),
                e.get("sequence"),
                e["type"],
                json.dumps(e),
                now_iso,
            )
            for e in durables
        ]
        async with self._db_lock:
            await asyncio.to_thread(self._insert_many, rows)
        return len(rows)

    def _insert_many(self, rows: list[tuple]) -> None:
        if self._conn is None:
            raise RuntimeError("EventStore used before init()")
        self._conn.executemany(
            "INSERT INTO events (session_id, sequence, type, data_json, received_at)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_session_events(
        self, session_id: str, *, since_id: int | None = None
    ) -> list[StoredEvent]:
        """Return events for a session ordered by insertion id.

        ``since_id`` lets callers resume after a reconnect without
        replaying the whole log. Story 2.3's SSE endpoint uses this
        as the ``Last-Event-ID`` cursor.
        """
        async with self._db_lock:
            rows = await asyncio.to_thread(
                self._fetch_session, session_id, since_id
            )
        out: list[StoredEvent] = []
        for row in rows:
            out.append(
                StoredEvent(
                    id=row[0],
                    session_id=row[1],
                    sequence=row[2],
                    type=row[3],
                    data=json.loads(row[4]),
                    received_at=row[5],
                )
            )
        return out

    def _fetch_session(
        self, session_id: str, since_id: int | None
    ) -> list[tuple]:
        if self._conn is None:
            raise RuntimeError("EventStore used before init()")
        if since_id is None:
            cur = self._conn.execute(
                "SELECT id, session_id, sequence, type, data_json, received_at"
                " FROM events WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        else:
            cur = self._conn.execute(
                "SELECT id, session_id, sequence, type, data_json, received_at"
                " FROM events WHERE session_id = ? AND id > ? ORDER BY id",
                (session_id, since_id),
            )
        return cur.fetchall()

    async def count(self) -> int:
        """Return total row count. Used by tests and observability."""
        async with self._db_lock:
            return await asyncio.to_thread(self._count_sync)

    def _count_sync(self) -> int:
        if self._conn is None:
            raise RuntimeError("EventStore used before init()")
        cur = self._conn.execute("SELECT COUNT(*) FROM events")
        return int(cur.fetchone()[0])
