"""Local persistent workspace store scoped by org/user/conversation."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

_PATH_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._\- ]+$")
_SCOPE_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")
MAX_PATH_DEPTH = 20


@dataclass
class WorkspaceEntry:
    name: str
    path: str
    is_folder: bool
    size: int
    mimetype: str
    last_modified: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sanitize_scope_segment(value: str, *, fallback: str) -> str:
    cleaned = _SCOPE_SEGMENT_RE.sub("_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or fallback


def _sanitize_path(path: str) -> str:
    normalized = PurePosixPath(path).as_posix()
    parts = [part for part in normalized.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("Path traversal not allowed")
    if len(parts) > MAX_PATH_DEPTH:
        raise ValueError(f"Path too deep (max {MAX_PATH_DEPTH} levels)")
    for part in parts:
        if not _PATH_SEGMENT_RE.match(part):
            raise ValueError(
                f"Invalid path segment: '{part}'. Use letters, digits, spaces, dots, hyphens, or underscores.",
            )
    return "/".join(parts)


def _guess_mimetype(name: str, *, is_folder: bool) -> str:
    if is_folder:
        return "folder"
    mt, _ = mimetypes.guess_type(name)
    return mt or "application/octet-stream"


def _entry_from_path(path: Path, *, relative_path: str) -> WorkspaceEntry:
    stat = path.stat()
    is_folder = path.is_dir()
    return WorkspaceEntry(
        name=path.name,
        path=relative_path,
        is_folder=is_folder,
        size=0 if is_folder else stat.st_size,
        mimetype=_guess_mimetype(path.name, is_folder=is_folder),
        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


class LocalWorkspaceStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        public_base_path: str = "/api/v1/agents/conversations",
    ) -> None:
        self._root = Path(data_dir) / "workspaces"
        self._root.mkdir(parents=True, exist_ok=True)
        self._public_base_path = public_base_path.rstrip("/")

    async def list_objects(
        self,
        org_id: str,
        user_id: str,
        conversation_id: str,
        path: str = "",
    ) -> list[WorkspaceEntry]:
        directory = self.resolve_path(
            org_id,
            user_id,
            conversation_id,
            path,
            expect_directory=True,
            create=True,
        )
        entries: list[WorkspaceEntry] = []
        for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            rel_path = child.relative_to(self.workspace_root(org_id, user_id, conversation_id)).as_posix()
            entries.append(_entry_from_path(child, relative_path=rel_path))
        return entries

    async def create_folder(
        self,
        org_id: str,
        user_id: str,
        conversation_id: str,
        path: str,
    ) -> WorkspaceEntry:
        folder = self.resolve_path(
            org_id,
            user_id,
            conversation_id,
            path,
            expect_directory=True,
            create=True,
        )
        folder.mkdir(parents=True, exist_ok=True)
        rel_path = folder.relative_to(self.workspace_root(org_id, user_id, conversation_id)).as_posix()
        return _entry_from_path(folder, relative_path=rel_path)

    async def upload_content(
        self,
        org_id: str,
        user_id: str,
        conversation_id: str,
        path: str,
        content: bytes,
        mimetype: str,
        *,
        max_size: int = 50 * 1024 * 1024,
    ) -> WorkspaceEntry:
        if len(content) > max_size:
            raise ValueError(f"Content exceeds max size of {max_size} bytes")
        target = self.resolve_path(org_id, user_id, conversation_id, path, create=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, content)
        rel_path = target.relative_to(self.workspace_root(org_id, user_id, conversation_id)).as_posix()
        return WorkspaceEntry(
            name=target.name,
            path=rel_path,
            is_folder=False,
            size=len(content),
            mimetype=mimetype or "application/octet-stream",
            last_modified=datetime.now(timezone.utc).isoformat(),
        )

    async def get_url(self, org_id: str, user_id: str, conversation_id: str, path: str) -> str:
        clean_path = _sanitize_path(path)
        target = self.resolve_path(org_id, user_id, conversation_id, clean_path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return f"{self._public_base_path}/{conversation_id}/workspace/{clean_path}"

    async def delete(self, org_id: str, user_id: str, conversation_id: str, path: str) -> bool:
        target = self.resolve_path(org_id, user_id, conversation_id, path)
        if not target.exists():
            return False
        if target.is_dir():
            await asyncio.to_thread(shutil.rmtree, target)
        else:
            await asyncio.to_thread(target.unlink)
        return True

    async def open_file(
        self,
        org_id: str,
        user_id: str,
        conversation_id: str,
        path: str,
    ) -> tuple[Path, str]:
        target = self.resolve_path(org_id, user_id, conversation_id, path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return target, _guess_mimetype(target.name, is_folder=False)

    def workspace_root(self, org_id: str, user_id: str, conversation_id: str) -> Path:
        org_segment = _sanitize_scope_segment(org_id, fallback="default-org")
        user_segment = _sanitize_scope_segment(user_id, fallback="anonymous")
        conversation_segment = _sanitize_scope_segment(conversation_id, fallback="conversation")
        root = self._root / org_segment / user_segment / conversation_segment
        root.mkdir(parents=True, exist_ok=True)
        return root

    def resolve_path(
        self,
        org_id: str,
        user_id: str,
        conversation_id: str,
        path: str,
        *,
        expect_directory: bool = False,
        create: bool = False,
    ) -> Path:
        root = self.workspace_root(org_id, user_id, conversation_id).resolve()
        clean_path = _sanitize_path(path) if path else ""
        candidate = (root / clean_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Path traversal not allowed") from exc
        if create and expect_directory:
            candidate.mkdir(parents=True, exist_ok=True)
        return candidate
