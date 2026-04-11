"""Unit tests for ``mars memory export`` (Story 6.3)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

from mars.memory import (
    _pick_latest_bundle,
    _timestamp_from_key,
    memory_export_command,
)

BUCKET = "mars-memory-test"
WORKSPACE = "default"
AGENT = "pr-reviewer"


def _make_bundle(text: str = "hello") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="session_history.jsonl")
        payload = text.encode("utf-8")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.fixture
def moto_s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_timestamp_from_key_strips_path_and_suffix():
    key = "default/pr-reviewer/sess-a/20260411T160000Z.tar.gz"
    assert _timestamp_from_key(key) == "20260411T160000Z"


def test_pick_latest_bundle_returns_none_when_empty(moto_s3):
    result = _pick_latest_bundle(moto_s3, bucket=BUCKET, prefix="missing/")
    assert result is None


def test_pick_latest_bundle_picks_most_recent(moto_s3):
    # Upload two bundles for the same agent
    moto_s3.put_object(
        Bucket=BUCKET,
        Key=f"{WORKSPACE}/{AGENT}/sess/20260411T100000Z.tar.gz",
        Body=_make_bundle("old"),
    )
    moto_s3.put_object(
        Bucket=BUCKET,
        Key=f"{WORKSPACE}/{AGENT}/sess/20260411T160000Z.tar.gz",
        Body=_make_bundle("new"),
    )
    latest = _pick_latest_bundle(
        moto_s3, bucket=BUCKET, prefix=f"{WORKSPACE}/{AGENT}/"
    )
    assert latest is not None
    assert latest["Key"].endswith("20260411T160000Z.tar.gz")


def test_pick_latest_bundle_ignores_non_tarball_objects(moto_s3):
    """A stray .txt file in the prefix shouldn't be picked."""
    moto_s3.put_object(
        Bucket=BUCKET,
        Key=f"{WORKSPACE}/{AGENT}/sess/README.txt",
        Body=b"hello",
    )
    moto_s3.put_object(
        Bucket=BUCKET,
        Key=f"{WORKSPACE}/{AGENT}/sess/20260411T160000Z.tar.gz",
        Body=_make_bundle(),
    )
    latest = _pick_latest_bundle(
        moto_s3, bucket=BUCKET, prefix=f"{WORKSPACE}/{AGENT}/"
    )
    assert latest is not None
    assert latest["Key"].endswith(".tar.gz")


# ---------------------------------------------------------------------------
# memory_export_command
# ---------------------------------------------------------------------------


def test_export_downloads_latest_bundle(moto_s3, tmp_path: Path):
    """Direct-callback invocation so we can inject the moto-backed S3
    client. Click doesn't let us serialize a live object through a
    CLI option, so the full ``mars memory export agent`` path is
    covered in an optional live contract test instead of here."""
    key = f"{WORKSPACE}/{AGENT}/sess-a/20260411T160000Z.tar.gz"
    moto_s3.put_object(Bucket=BUCKET, Key=key, Body=_make_bundle("canonical"))

    dest = tmp_path / "out.tar.gz"
    from mars import memory as memory_mod

    memory_mod.memory_export_command.callback(
        subcommand="export",
        agent_name=AGENT,
        bucket=BUCKET,
        workspace=None,
        dest=dest,
        s3_client=moto_s3,
    )
    assert dest.exists()
    with tarfile.open(dest, "r:gz") as tar:
        member = tar.extractfile("session_history.jsonl")
        assert member is not None
        assert member.read() == b"canonical"


def test_export_errors_when_bucket_not_configured():
    runner = CliRunner(env={})
    result = runner.invoke(memory_export_command, ["export", AGENT])
    assert result.exit_code != 0
    assert "bucket not configured" in result.output


def test_export_errors_when_no_bundles_exist(moto_s3, tmp_path: Path):
    from mars import memory as memory_mod

    dest = tmp_path / "out.tar.gz"
    with pytest.raises(Exception) as exc:
        memory_mod.memory_export_command.callback(
            subcommand="export",
            agent_name="missing-agent",
            bucket=BUCKET,
            workspace=None,
            dest=dest,
            s3_client=moto_s3,
        )
    assert "no memory bundles" in str(exc.value)
    assert not dest.exists()


def test_export_respects_workspace_override(moto_s3, tmp_path: Path):
    """A --workspace override reads from a different S3 prefix."""
    moto_s3.put_object(
        Bucket=BUCKET,
        Key=f"team-alpha/{AGENT}/sess/20260411T160000Z.tar.gz",
        Body=_make_bundle("team-alpha bundle"),
    )
    from mars import memory as memory_mod

    dest = tmp_path / "out.tar.gz"
    memory_mod.memory_export_command.callback(
        subcommand="export",
        agent_name=AGENT,
        bucket=BUCKET,
        workspace="team-alpha",
        dest=dest,
        s3_client=moto_s3,
    )
    with tarfile.open(dest, "r:gz") as tar:
        assert (
            tar.extractfile("session_history.jsonl").read()
            == b"team-alpha bundle"
        )
