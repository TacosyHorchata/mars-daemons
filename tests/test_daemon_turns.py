import sqlite3
from pathlib import Path

from mars_runtime.daemon import turns


def _connect(tmp_path: Path) -> sqlite3.Connection:
    conn = turns.connect(tmp_path / "turns.db")
    turns.init_db(conn)
    return conn


def test_idempotency_duplicate_turn_id(tmp_path: Path) -> None:
    conn = _connect(tmp_path)

    created, state = turns.create_exclusive(
        conn,
        turn_id="8d3a7c14-2601-4ea6-90db-bfe93f10bb5c",
        session_id="sess_aaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert (created, state) == (True, "accepted")

    turns.update_state(conn, turn_id="8d3a7c14-2601-4ea6-90db-bfe93f10bb5c", state="running")

    created, state = turns.create_exclusive(
        conn,
        turn_id="8d3a7c14-2601-4ea6-90db-bfe93f10bb5c",
        session_id="sess_bbbbbbbbbbbbbbbbbbbbbbbb",
    )
    assert (created, state) == (False, "running")


def test_session_busy_via_partial_index(tmp_path: Path) -> None:
    conn = _connect(tmp_path)

    created, state = turns.create_exclusive(
        conn,
        turn_id="2f7b2d3a-9b89-4974-b66d-a3f60e8cfe3f",
        session_id="sess_cccccccccccccccccccccccc",
    )
    assert (created, state) == (True, "accepted")

    created, state = turns.create_exclusive(
        conn,
        turn_id="4e2db7de-7ec0-489a-bfa6-767bf293b6f1",
        session_id="sess_cccccccccccccccccccccccc",
    )
    assert (created, state) == (False, "session_busy")


def test_recover_stale_flips_to_failed(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    conn.executemany(
        """
        INSERT INTO turns(turn_id, session_id, state)
        VALUES(?, ?, ?)
        """,
        [
            ("d7dedb30-5d82-4d0e-8744-af954a0f9c8d", "sess_dddddddddddddddddddddddd", "accepted"),
            ("7594900f-1bfe-44ab-99bf-b6f4cf2e2a08", "sess_eeeeeeeeeeeeeeeeeeeeeeee", "running"),
            ("1f9fa7b8-6724-48a7-b24b-64002b60f9c8", "sess_ffffffffffffffffffffffff", "completed"),
        ],
    )
    conn.commit()

    recovered = turns.recover_stale(conn)
    assert recovered == 2

    rows = conn.execute(
        "SELECT turn_id, state, error FROM turns ORDER BY turn_id"
    ).fetchall()
    assert rows == [
        ("1f9fa7b8-6724-48a7-b24b-64002b60f9c8", "completed", None),
        ("7594900f-1bfe-44ab-99bf-b6f4cf2e2a08", "failed", "daemon_restart"),
        ("d7dedb30-5d82-4d0e-8744-af954a0f9c8d", "failed", "daemon_restart"),
    ]


def test_wal_enabled(tmp_path: Path) -> None:
    conn = turns.connect(tmp_path / "turns.db")

    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
    assert journal_mode == ("wal",)
