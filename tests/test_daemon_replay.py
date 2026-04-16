from __future__ import annotations

from pathlib import Path

from mars_runtime.daemon import replay, turns


def test_append_event_sequential(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    sid = "sess_000000000000000000000001"

    s1 = replay.append_event(events_dir, sid, {"type": "a"})
    s2 = replay.append_event(events_dir, sid, {"type": "b"})
    s3 = replay.append_event(events_dir, sid, {"type": "c"})

    assert s1 == 1
    assert s2 == 2
    assert s3 == 3


def test_replay_after_returns_missed(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    sid = "sess_000000000000000000000002"
    for i in range(10):
        replay.append_event(events_dir, sid, {"type": f"e{i}"})

    out = replay.replay_after(events_dir, sid, after=5)

    assert [e["sequence"] for e in out] == [6, 7, 8, 9, 10]


def test_replay_after_zero_returns_all(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    sid = "sess_000000000000000000000003"
    for i in range(3):
        replay.append_event(events_dir, sid, {"type": f"e{i}"})

    out = replay.replay_after(events_dir, sid, after=0)

    assert [e["sequence"] for e in out] == [1, 2, 3]


def test_crash_recovery_appends_synthetic_event(tmp_path: Path) -> None:
    db_path = tmp_path / "turns.db"
    events_dir = tmp_path / "events"
    conn = turns.connect(db_path)
    try:
        turns.init_db(conn)
        sid = "sess_000000000000000000000004"
        turns.create_exclusive(conn, turn_id="t1", session_id=sid)
        stale = turns.recover_stale_with_ids(conn)
        assert stale == [("t1", sid)]
    finally:
        conn.close()

    for turn_id, sess_id in stale:
        replay.append_event(
            events_dir,
            sess_id,
            {"type": "turn_aborted", "reason": "daemon_restart", "turn_id": turn_id},
        )

    out = replay.replay_after(events_dir, sid, after=0)
    assert len(out) == 1
    assert out[0]["type"] == "turn_aborted"
    assert out[0]["reason"] == "daemon_restart"
    assert out[0]["turn_id"] == "t1"
