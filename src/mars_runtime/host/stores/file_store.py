"""Local filesystem storage for conversation attachments."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.datastructures import UploadFile


@dataclass(frozen=True)
class LocalFileRef:
    key: str
    filename: str
    mimetype: str
    size: int
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FileSizeExceededError(RuntimeError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def sanitize_filename(filename: str | None) -> str:
    safe_name = Path(filename or "upload.bin").name.strip()
    return safe_name or "upload.bin"


class LocalFileStore:
    def __init__(self, data_dir: Path, *, public_base_path: str = "/api/v1/agents/files") -> None:
        self._root = Path(data_dir) / "files"
        self._root.mkdir(parents=True, exist_ok=True)
        self._public_base_path = public_base_path.rstrip("/")

    async def upload(self, conversation_id: str, filename: str, content: bytes, mimetype: str) -> LocalFileRef:
        safe_name = sanitize_filename(filename)
        key = self._build_key(conversation_id, safe_name)
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, content)
        return LocalFileRef(
            key=key,
            filename=safe_name,
            mimetype=mimetype,
            size=len(content),
            url=await self.get_url(key),
        )

    async def upload_file(
        self,
        conversation_id: str,
        filename: str,
        upload: "UploadFile",
        mimetype: str,
        *,
        max_size: int,
    ) -> LocalFileRef:
        safe_name = sanitize_filename(filename)
        key = self._build_key(conversation_id, safe_name)
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        total_size = 0
        with path.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max(max_size, 1):
                    handle.close()
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                    raise FileSizeExceededError(
                        f"File '{safe_name}' exceeds max size of {max_size} bytes",
                    )
                await asyncio.to_thread(handle.write, chunk)
        return LocalFileRef(
            key=key,
            filename=safe_name,
            mimetype=mimetype,
            size=total_size,
            url=await self.get_url(key),
        )

    async def get_url(self, key: str) -> str:
        return f"{self._public_base_path}/{key}"

    async def delete(self, key: str) -> bool:
        path = self._path_for(key)
        if not path.exists():
            return False
        await asyncio.to_thread(path.unlink)
        return True

    async def open(self, key: str) -> tuple[Path, str]:
        path = self._path_for(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return path, _guess_media_type(path.name)

    def _build_key(self, conversation_id: str, filename: str) -> str:
        return "/".join([conversation_id, f"{uuid.uuid4().hex}-{filename}"])

    def _path_for(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError(key) from exc
        return candidate


def _guess_media_type(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
