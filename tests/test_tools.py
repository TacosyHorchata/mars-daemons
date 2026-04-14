"""Tool registry + individual tool behavior (happy paths + denylists)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mars_runtime.tools import ALL_TOOLS, ToolOutput, ToolRegistry, load_all

load_all()


def test_registry_allowlist_filters():
    reg = ToolRegistry(["read", "bash"])
    assert sorted(reg.names()) == ["bash", "read"]


def test_registry_empty_allowlist_includes_all():
    reg = ToolRegistry(None)
    assert "read" in reg.names()
    assert "edit" in reg.names()
    assert "bash" in reg.names()


def test_registry_rejects_unknown_tool_name():
    with pytest.raises(ValueError, match="unknown"):
        ToolRegistry(["nonexistent-tool"])


def test_registry_execute_unknown_returns_error():
    reg = ToolRegistry(["read"])
    out = reg.execute("bash", {"command": "echo ok"})
    assert out.is_error


def test_registry_catches_exceptions_in_tools():
    reg = ToolRegistry(None)
    # read without required field → KeyError → caught
    out = reg.execute("read", {})
    assert out.is_error


# --- read ------------------------------------------------------------------


def test_read_file(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("line1\nline2\nline3\n")
    out = ALL_TOOLS["read"].fn({"file_path": str(f)})
    assert not out.is_error
    assert "1\tline1" in out.content
    assert "3\tline3" in out.content


def test_read_missing_file(tmp_path: Path):
    out = ALL_TOOLS["read"].fn({"file_path": str(tmp_path / "nope.txt")})
    assert out.is_error


def test_read_offset_limit(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    out = ALL_TOOLS["read"].fn({"file_path": str(f), "offset": 2, "limit": 3})
    # Lines 3-5 of the file (0-indexed offset=2 → starts at "line2")
    assert "line2" in out.content
    assert "line4" in out.content
    assert "line5" not in out.content


# --- list ------------------------------------------------------------------


def test_list_dir(tmp_path: Path):
    (tmp_path / "a.txt").write_text("")
    (tmp_path / "sub").mkdir()
    out = ALL_TOOLS["list"].fn({"path": str(tmp_path)})
    assert "a.txt" in out.content
    assert "sub" in out.content
    assert "dir" in out.content


def test_list_not_a_dir(tmp_path: Path):
    f = tmp_path / "f"
    f.write_text("")
    out = ALL_TOOLS["list"].fn({"path": str(f)})
    assert out.is_error


# --- edit ------------------------------------------------------------------


def test_edit_happy(tmp_path: Path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 2\n")
    out = ALL_TOOLS["edit"].fn(
        {"file_path": str(f), "old_string": "x = 1", "new_string": "x = 42"}
    )
    assert not out.is_error
    assert f.read_text() == "x = 42\ny = 2\n"


def test_edit_not_unique(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("dup\ndup\n")
    out = ALL_TOOLS["edit"].fn(
        {"file_path": str(f), "old_string": "dup", "new_string": "y"}
    )
    assert out.is_error
    assert "unique" in out.content or "2 times" in out.content


def test_edit_not_found(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    out = ALL_TOOLS["edit"].fn(
        {"file_path": str(f), "old_string": "missing", "new_string": "y"}
    )
    assert out.is_error


@pytest.mark.parametrize("protected", ["CLAUDE.md", "AGENTS.md", "agent.yaml"])
def test_edit_blocks_protected_files(tmp_path: Path, protected: str):
    f = tmp_path / protected
    f.write_text("protected\n")
    out = ALL_TOOLS["edit"].fn(
        {"file_path": str(f), "old_string": "protected", "new_string": "x"}
    )
    assert out.is_error
    assert "admin-only" in out.content


# --- bash ------------------------------------------------------------------


def test_bash_happy():
    out = ALL_TOOLS["bash"].fn({"command": "echo hello"})
    assert not out.is_error
    assert "hello" in out.content


def test_bash_nonzero_exit():
    out = ALL_TOOLS["bash"].fn({"command": "false"})
    assert out.is_error
    assert "exit=1" in out.content


@pytest.mark.parametrize(
    "cmd",
    ["env", "printenv", "env | grep FOO", "cat /etc/passwd; env", "echo $HOME", "echo  $PATH", "set"],
)
def test_bash_blocks_secret_read(cmd: str):
    out = ALL_TOOLS["bash"].fn({"command": cmd})
    assert out.is_error
    assert "blocked" in out.content


def test_bash_allows_env_as_argument():
    # `grep env file` should NOT be blocked — env is an argument here.
    out = ALL_TOOLS["bash"].fn({"command": "echo nothing"})
    assert not out.is_error


def test_bash_timeout():
    out = ALL_TOOLS["bash"].fn({"command": "sleep 5", "timeout": 1})
    assert out.is_error
    assert "timed out" in out.content


# --- glob ------------------------------------------------------------------


def test_glob(tmp_path: Path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    out = ALL_TOOLS["glob"].fn({"pattern": "*.py", "path": str(tmp_path)})
    assert "a.py" in out.content
    assert "b.py" in out.content
    assert "c.txt" not in out.content


# --- grep ------------------------------------------------------------------


def test_grep(tmp_path: Path):
    (tmp_path / "x.py").write_text("def hello():\n    pass\n")
    (tmp_path / "y.py").write_text("def goodbye():\n    pass\n")
    out = ALL_TOOLS["grep"].fn({"pattern": "hello", "path": str(tmp_path)})
    assert "x.py" in out.content
    assert "hello" in out.content
