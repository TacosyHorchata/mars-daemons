"""Unit tests for :mod:`memory.sync` — S3 memory sync (Story 6.3).

Uses :mod:`moto` to stand up an in-memory S3 server so tests exercise
the real boto3 client surface without touching the network.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from memory.sync import (
    DEFAULT_SYNC_INTERVAL_S,
    S3MemorySync,
    build_memory_tarball,
)


SESSION_ID = "mars-mem-sync-1"
BUCKET = "mars-memory-test"
REGION = "us-east-1"


# ---------------------------------------------------------------------------
# build_memory_tarball
# ---------------------------------------------------------------------------


def test_build_memory_tarball_empty_dir_returns_valid_tarball(tmp_path: Path):
    """A missing or empty memory dir must not raise."""
    data = build_memory_tarball(tmp_path / "nonexistent")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert tar.getmembers() == []


def test_build_memory_tarball_packs_session_history(tmp_path: Path):
    memdir = tmp_path / "sess" / "memory"
    memdir.mkdir(parents=True)
    (memdir / "session_history.jsonl").write_text('{"type":"session_started"}\n')
    (memdir / "tool_calls.jsonl").write_text('{"tool_use_id":"tu-1"}\n')

    data = build_memory_tarball(memdir)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
        assert "session_history.jsonl" in names
        assert "tool_calls.jsonl" in names
        extracted = tar.extractfile("session_history.jsonl").read().decode()
        assert '"session_started"' in extracted


def test_build_memory_tarball_uses_relative_arcnames(tmp_path: Path):
    """Tarball entries must be relative paths so extraction goes into
    the caller's current directory, not an absolute /workspace/..."""
    memdir = tmp_path / "sess-x" / "memory"
    memdir.mkdir(parents=True)
    (memdir / "session_history.jsonl").write_text("{}\n")

    data = build_memory_tarball(memdir)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            assert not member.name.startswith("/")
            assert not member.name.startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# S3MemorySync — construction + introspection
# ---------------------------------------------------------------------------


def test_empty_bucket_rejected():
    with pytest.raises(ValueError):
        S3MemorySync(
            bucket="",
            key_prefix="mars/default",
            memory_root="/tmp",
            s3_client=object(),
        )


def test_default_sync_interval_is_5_minutes():
    assert DEFAULT_SYNC_INTERVAL_S == 300.0


def test_build_s3_key_uses_prefix_session_and_timestamp():
    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="mars/default/pr-reviewer",
        memory_root="/workspace",
        s3_client=object(),
        clock=lambda: "20260411T160000Z",
    )
    key = sync.build_s3_key("sess-abc")
    assert key == "mars/default/pr-reviewer/sess-abc/20260411T160000Z.tar.gz"


def test_build_s3_key_handles_empty_prefix():
    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="",
        memory_root="/workspace",
        s3_client=object(),
        clock=lambda: "20260411T160000Z",
    )
    key = sync.build_s3_key("s1")
    assert key == "s1/20260411T160000Z.tar.gz"


def test_track_and_untrack_sessions():
    sync = S3MemorySync(
        bucket=BUCKET, key_prefix="p", memory_root="/", s3_client=object()
    )
    sync.track("s1")
    sync.track("s2")
    sync.track("s1")  # idempotent
    assert sync.tracked_sessions == {"s1", "s2"}
    sync.untrack("s1")
    assert sync.tracked_sessions == {"s2"}
    sync.untrack("s-none")  # no-op
    assert sync.tracked_sessions == {"s2"}


# ---------------------------------------------------------------------------
# sync_session — happy path via moto
# ---------------------------------------------------------------------------


@pytest.fixture
def moto_s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _populate_session(root: Path, session_id: str) -> None:
    memdir = root / session_id / "memory"
    memdir.mkdir(parents=True, exist_ok=True)
    (memdir / "session_history.jsonl").write_text('{"type":"session_started"}\n')
    (memdir / "tool_calls.jsonl").write_text('{"tool_use_id":"tu-1"}\n')


def test_sync_session_uploads_tarball_to_s3(moto_s3, tmp_path: Path):
    _populate_session(tmp_path, SESSION_ID)
    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="mars/default/pr-reviewer",
        memory_root=tmp_path,
        s3_client=moto_s3,
        clock=lambda: "20260411T160000Z",
    )

    async def _go() -> str | None:
        return await sync.sync_session(SESSION_ID)

    key = asyncio.run(_go())
    assert key is not None
    assert sync.upload_count == 1
    assert sync.failure_count == 0

    # Object exists in the mocked bucket
    head = moto_s3.head_object(Bucket=BUCKET, Key=key)
    assert head["ContentType"] == "application/gzip"

    # Download and inspect the tarball
    body = moto_s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
        assert "session_history.jsonl" in names
        assert "tool_calls.jsonl" in names


def test_sync_session_missing_dir_uploads_empty_tarball(moto_s3, tmp_path: Path):
    """A session that died before writing anything should still
    upload an empty tarball so the audit trail records the attempt."""
    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="p",
        memory_root=tmp_path,
        s3_client=moto_s3,
        clock=lambda: "20260411T160000Z",
    )

    async def _go() -> str | None:
        return await sync.sync_session("sess-died-early")

    key = asyncio.run(_go())
    assert key is not None
    assert sync.upload_count == 1

    body = moto_s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        assert tar.getmembers() == []


def test_sync_session_failure_recorded_and_continues():
    """Uploader exceptions must be caught so one bad session doesn't
    kill the background loop."""
    calls = {"n": 0}

    class _BrokenClient:
        def put_object(self, **kwargs):
            calls["n"] += 1
            raise RuntimeError("S3 is broken")

    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="p",
        memory_root="/tmp",
        s3_client=_BrokenClient(),
        clock=lambda: "t",
    )

    async def _go():
        return await sync.sync_session("s1")

    result = asyncio.run(_go())
    assert result is None
    assert sync.upload_count == 0
    assert sync.failure_count == 1
    assert isinstance(sync.last_error, RuntimeError)
    assert calls["n"] == 1


def test_sync_all_tracked_uploads_every_registered_session(
    moto_s3, tmp_path: Path
):
    _populate_session(tmp_path, "s1")
    _populate_session(tmp_path, "s2")
    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="p",
        memory_root=tmp_path,
        s3_client=moto_s3,
        clock=lambda: "20260411T160000Z",
    )
    sync.track("s1")
    sync.track("s2")

    async def _go():
        return await sync.sync_all_tracked()

    results = asyncio.run(_go())
    assert set(results.keys()) == {"s1", "s2"}
    assert all(v is not None for v in results.values())
    assert sync.upload_count == 2


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


def test_start_and_stop_runs_final_drain(moto_s3, tmp_path: Path):
    """On stop, the background task exits AND a final drain uploads
    any pending sessions — so no memory is lost on graceful shutdown."""
    _populate_session(tmp_path, "s-final")
    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="p",
        memory_root=tmp_path,
        interval_s=60.0,  # far longer than the test takes
        s3_client=moto_s3,
        clock=lambda: "20260411T160000Z",
    )
    sync.track("s-final")

    async def _go():
        await sync.start()
        await sync.stop()

    asyncio.run(_go())
    assert sync.upload_count >= 1  # final drain fired


def test_stop_without_start_just_drains(moto_s3, tmp_path: Path):
    _populate_session(tmp_path, "s-drain")
    sync = S3MemorySync(
        bucket=BUCKET,
        key_prefix="p",
        memory_root=tmp_path,
        s3_client=moto_s3,
        clock=lambda: "20260411T160000Z",
    )
    sync.track("s-drain")

    async def _go():
        await sync.stop()

    asyncio.run(_go())
    assert sync.upload_count == 1
