"""Host ↔ sandbox file transfer CLI tests.

Covers `mars push` and `mars pull` subcommands. Local paths use the
bind-mounted workspace directly; fly:// URLs would shell out to `fly
ssh sftp shell` (mocked here since no real fly app is available).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mars_runtime.__main__ import main
from mars_runtime.cli.files import (
    _confined_workspace_path,
    _parse_fly_url,
)


# --- Helpers ---------------------------------------------------------------


def test_parse_fly_url_accepts_valid():
    assert _parse_fly_url("fly://my-app/file.txt") == ("my-app", "file.txt")
    assert _parse_fly_url("fly://prod-123/sub/dir/x.pdf") == ("prod-123", "sub/dir/x.pdf")


def test_parse_fly_url_rejects_invalid():
    assert _parse_fly_url("http://example.com/file") is None
    assert _parse_fly_url("fly:file") is None
    assert _parse_fly_url("/tmp/file.txt") is None
    assert _parse_fly_url("fly://Uppercase-App/x") is None  # Fly app names are lowercase


def test_confined_path_allows_simple_relative(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    resolved = _confined_workspace_path(ws, "foo.txt")
    assert resolved == (ws / "foo.txt").resolve()


def test_confined_path_allows_nested_relative(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    resolved = _confined_workspace_path(ws, "sub/dir/file.md")
    assert resolved == (ws / "sub/dir/file.md").resolve()


def test_confined_path_rejects_absolute(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with pytest.raises(ValueError, match="must be relative"):
        _confined_workspace_path(ws, "/etc/passwd")


def test_confined_path_rejects_escape(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with pytest.raises(ValueError, match="escapes workspace"):
        _confined_workspace_path(ws, "../../../etc/passwd")


def test_confined_path_rejects_protected_basename(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    for protected in ("CLAUDE.md", "AGENTS.md", "agent.yaml"):
        with pytest.raises(ValueError, match="protected agent config"):
            _confined_workspace_path(ws, protected)


# --- push / pull local roundtrip ------------------------------------------


def test_push_local_roundtrip(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))

    src = tmp_path / "hello.txt"
    src.write_text("world")

    rc = main(["push", str(src), "hello.txt"])
    assert rc == 0

    dst = data_dir / "workspace" / "hello.txt"
    assert dst.exists()
    assert dst.read_text() == "world"


def test_pull_local_roundtrip(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))

    (data_dir / "workspace").mkdir(parents=True)
    (data_dir / "workspace" / "report.txt").write_text("output")

    out = tmp_path / "report.txt"
    rc = main(["pull", "report.txt", str(out)])
    assert rc == 0
    assert out.read_text() == "output"


def test_push_creates_nested_dirs(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))

    src = tmp_path / "a.txt"
    src.write_text("nested")

    rc = main(["push", str(src), "deep/nested/a.txt"])
    assert rc == 0
    assert (data_dir / "workspace" / "deep" / "nested" / "a.txt").read_text() == "nested"


def test_push_preserves_mtime(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))

    src = tmp_path / "ts.txt"
    src.write_text("x")
    import os
    os.utime(src, (1000000, 1000000))

    main(["push", str(src), "ts.txt"])

    dst = data_dir / "workspace" / "ts.txt"
    assert int(dst.stat().st_mtime) == 1000000


# --- push error paths ------------------------------------------------------


def test_push_missing_source(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    rc = main(["push", str(tmp_path / "does-not-exist.txt"), "x.txt"])
    assert rc == 1
    assert "source not found" in capsys.readouterr().err


def test_push_rejects_protected(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    src = tmp_path / "evil.md"
    src.write_text("replacement")
    rc = main(["push", str(src), "CLAUDE.md"])
    assert rc == 1
    assert "protected agent config" in capsys.readouterr().err


def test_push_rejects_escape(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    src = tmp_path / "a.txt"
    src.write_text("x")
    rc = main(["push", str(src), "../../../tmp/evil.txt"])
    assert rc == 1
    assert "escapes workspace" in capsys.readouterr().err


def test_pull_missing_source(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    (data_dir / "workspace").mkdir(parents=True)
    rc = main(["pull", "does-not-exist.txt", str(tmp_path / "out.txt")])
    assert rc == 1
    assert "source not found" in capsys.readouterr().err


# --- Fly path (mocked) -----------------------------------------------------


def test_push_fly_invokes_sftp(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    src = tmp_path / "upload.txt"
    src.write_text("payload")

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "input": kwargs.get("input")})
        return subprocess.CompletedProcess(cmd, 0)

    with patch("mars_runtime.cli.files.subprocess.run", side_effect=_fake_run):
        rc = main(["push", str(src), "fly://prod-app/remote/file.txt"])

    assert rc == 0
    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    assert cmd[0] == "fly"
    assert "-a" in cmd and "prod-app" in cmd
    script = calls[0]["input"]
    assert f"put {src}" in script
    assert "/data/workspace/remote/file.txt" in script


def test_pull_fly_invokes_sftp(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("mars_runtime.cli.files.subprocess.run", side_effect=_fake_run):
        rc = main(["pull", "fly://prod-app/reports/q3.md", str(tmp_path / "q3.md")])

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == "fly"


def test_push_fly_surfaces_exit_code(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    src = tmp_path / "upload.txt"
    src.write_text("payload")

    def _fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(42, cmd, stderr="unauthorized")

    with patch("mars_runtime.cli.files.subprocess.run", side_effect=_fake_run):
        rc = main(["push", str(src), "fly://app/x.txt"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "fly sftp failed" in err
    assert "42" in err


def test_push_fly_without_flyctl_installed(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    src = tmp_path / "upload.txt"
    src.write_text("payload")

    def _fake_run(cmd, **kwargs):
        raise FileNotFoundError("fly not found")

    with patch("mars_runtime.cli.files.subprocess.run", side_effect=_fake_run):
        rc = main(["push", str(src), "fly://app/x.txt"])

    assert rc == 1
    assert "flyctl not found" in capsys.readouterr().err


def test_push_fly_rejects_protected(tmp_path, monkeypatch, capsys):
    src = tmp_path / "evil.md"
    src.write_text("x")
    rc = main(["push", str(src), "fly://app/CLAUDE.md"])
    assert rc == 1
    assert "protected" in capsys.readouterr().err


def test_push_fly_rejects_absolute_remote(tmp_path, monkeypatch, capsys):
    src = tmp_path / "x.txt"
    src.write_text("x")
    rc = main(["push", str(src), "fly://app//etc/passwd"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "absolute" in err or "relative" in err


def test_push_fly_rejects_parent_traversal(tmp_path, monkeypatch, capsys):
    """fly://app/../../etc/passwd would escape /data/workspace when the
    SFTP server normalizes path segments. Reject at CLI."""
    src = tmp_path / "x.txt"
    src.write_text("x")
    rc = main(["push", str(src), "fly://app/../../etc/passwd"])
    assert rc == 1
    assert ".." in capsys.readouterr().err


def test_pull_fly_rejects_parent_traversal(tmp_path, monkeypatch, capsys):
    rc = main(["pull", "fly://app/../../etc/passwd", str(tmp_path / "out")])
    assert rc == 1
    assert ".." in capsys.readouterr().err


def test_push_fly_rejects_whitespace_in_remote(tmp_path, monkeypatch, capsys):
    """Whitespace/CR/LF in the remote path would split the `put` command
    on `fly ssh sftp shell`'s line-based parser."""
    src = tmp_path / "x.txt"
    src.write_text("x")
    rc = main(["push", str(src), "fly://app/with space.txt"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "whitespace" in err or "control" in err


def test_push_fly_rejects_whitespace_in_local(tmp_path, monkeypatch, capsys):
    src = tmp_path / "has space.txt"
    src.write_text("x")
    rc = main(["push", str(src), "fly://app/dest.txt"])
    assert rc == 1
    assert "whitespace" in capsys.readouterr().err


def test_dispatch_preserves_yaml_named_push(tmp_path, monkeypatch):
    """Dispatch regression guard: a yaml literally named 'push' or 'pull'
    must NOT trigger the file-transfer subcommand. Verified by asserting
    the subcommand handlers are NOT called when the first arg resolves
    to an existing file."""
    yaml = tmp_path / "push"
    yaml.write_text("name: x\ndescription: x\nsystem_prompt_path: x\n")

    push_called = []
    pull_called = []

    with patch("mars_runtime.cli.files.cmd_push", side_effect=lambda a: push_called.append(a) or 0), \
         patch("mars_runtime.cli.files.cmd_pull", side_effect=lambda a: pull_called.append(a) or 0), \
         patch("mars_runtime.__main__._parse_args") as parse_args, \
         patch("mars_runtime.__main__._ingest_secrets_fd"), \
         patch("mars_runtime.__main__._harden_broker"):
        parse_args.return_value = type("X", (), {"list_sessions": True, "resume": None, "yaml_path": None, "data_dir": None})()
        # Patch session_store.list_recent to avoid touching disk
        with patch("mars_runtime.__main__.session_store.list_recent", return_value=[]):
            rc = main([str(yaml)])

    assert rc == 0
    assert not push_called, "push subcommand was incorrectly invoked"
    assert not pull_called, "pull subcommand was incorrectly invoked"


# --- Subcommand dispatch doesn't break existing session flags ---


def test_session_flags_still_work(tmp_path, monkeypatch, capsys):
    """push/pull subcommand dispatch must not break --list."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MARS_DATA_DIR", str(data_dir))
    rc = main(["--list"])
    assert rc == 0  # no sessions, empty output is fine
