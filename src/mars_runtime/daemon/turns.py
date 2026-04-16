from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS turns (
          turn_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('accepted','running','completed','failed')),
          error TEXT,
          created_at INTEGER NOT NULL DEFAULT (unixepoch()),
          updated_at INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE INDEX IF NOT EXISTS idx_turns_state ON turns(state);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_turn_per_session
          ON turns(session_id) WHERE state IN ('accepted','running');
        """
    )
    conn.commit()


def create_exclusive(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    session_id: str,
) -> tuple[bool, str]:
    try:
        conn.execute(
            """
            INSERT INTO turns(turn_id, session_id, state)
            VALUES(?, ?, 'accepted')
            """,
            (turn_id, session_id),
        )
        conn.commit()
        return True, "accepted"
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT state FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if existing is not None:
            return False, str(existing[0])
        busy = conn.execute(
            """
            SELECT 1
            FROM turns
            WHERE session_id = ?
              AND state IN ('accepted', 'running')
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if busy is not None:
            return False, "session_busy"
        raise


def update_state(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    state: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE turns
        SET state = ?, error = ?, updated_at = unixepoch()
        WHERE turn_id = ?
        """,
        (state, error, turn_id),
    )
    conn.commit()


def cas_update_state(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    from_state: str,
    to_state: str,
    error: str | None = None,
) -> bool:
    """Compare-and-swap state transition. Returns True if the update matched."""
    cursor = conn.execute(
        """
        UPDATE turns
        SET state = ?, error = ?, updated_at = unixepoch()
        WHERE turn_id = ? AND state = ?
        """,
        (to_state, error, turn_id, from_state),
    )
    conn.commit()
    return int(cursor.rowcount) > 0


def load_turn(conn: sqlite3.Connection, turn_id: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT session_id, state FROM turns WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def session_status(conn: sqlite3.Connection, session_id: str) -> tuple[str, int]:
    row = conn.execute(
        "SELECT 1 FROM turns WHERE session_id = ? AND state IN ('accepted','running') LIMIT 1",
        (session_id,),
    ).fetchone()
    status = "running" if row else "idle"
    count_row = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id = ? AND state = 'completed'",
        (session_id,),
    ).fetchone()
    turn_count = int(count_row[0]) if count_row else 0
    return status, turn_count


def recover_stale(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        UPDATE turns
        SET state = 'failed',
            error = 'daemon_restart',
            updated_at = unixepoch()
        WHERE state IN ('accepted', 'running')
        """
    )
    conn.commit()
    return int(cursor.rowcount)


def recover_stale_with_ids(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT turn_id, session_id
        FROM turns
        WHERE state IN ('accepted', 'running')
        """
    ).fetchall()
    conn.execute(
        """
        UPDATE turns
        SET state = 'failed',
            error = 'daemon_restart',
            updated_at = unixepoch()
        WHERE state IN ('accepted', 'running')
        """
    )
    conn.commit()
    return [(str(r[0]), str(r[1])) for r in rows]
