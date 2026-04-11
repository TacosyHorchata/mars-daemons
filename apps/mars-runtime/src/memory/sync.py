"""Periodic S3 sync for per-session memory bundles.

Scope (Story 6.3):

* Tarball ``/workspace/<session-id>/memory/`` into a single
  ``.tar.gz`` blob in memory.
* Upload to
  ``s3://<bucket>/<key_prefix>/<session-id>/<iso-timestamp>.tar.gz``
  so repeated syncs produce a growing history (not overwritten).
* Sync on a fixed interval (default 300s = 5 minutes) via a
  background asyncio task.
* Graceful start/stop; tolerates transient upload failures with
  structured log warnings — the session never blocks on sync.

The implementation wraps a synchronous ``boto3`` client through
:func:`asyncio.to_thread` so we do not pull in ``aioboto3``'s
transitive dependency footprint. Tests either inject a moto-backed
client directly or stub the uploader callable entirely.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_SYNC_INTERVAL_S",
    "S3MemorySync",
    "build_memory_tarball",
]

_log = logging.getLogger("mars.memory.sync")

#: Default interval between full syncs. Matches the v1 plan: "every
#: 5 min". Short enough to survive a crash with ≤5 min of lost
#: memory, long enough to keep S3 PUT costs bounded.
DEFAULT_SYNC_INTERVAL_S: float = 300.0


def build_memory_tarball(memory_dir: Path) -> bytes:
    """Return the gzipped tarball bytes of a session's memory dir.

    Missing directories return an empty tarball rather than
    raising — a session that never produced any memory (e.g. died
    in initialization) should still sync cleanly.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        if memory_dir.is_dir():
            for entry in sorted(memory_dir.rglob("*")):
                if entry.is_file():
                    arcname = entry.relative_to(memory_dir).as_posix()
                    tar.add(str(entry), arcname=arcname)
    return buffer.getvalue()


class S3MemorySync:
    """Background task that uploads memory tarballs to S3 on an interval.

    Args:
        bucket: S3 bucket name. Required.
        key_prefix: Path segment prepended to every object key. Usually
            ``<workspace>/<agent>``. Combines with session id + iso
            timestamp to produce the final key.
        memory_root: Root of the session memory dirs — typically
            ``/workspace`` so that ``<memory_root>/<session_id>/memory``
            is the per-session dir written by :class:`MemoryCapture`.
        interval_s: Seconds between syncs. Default 300 (5 min).
        s3_client: Optional pre-built boto3 ``s3`` client. Tests inject
            a moto-backed client; production passes ``None`` and the
            class builds its own from default credentials.
        clock: Optional callable returning the current UTC time as an
            ISO string. Tests pin the clock for deterministic keys.

    Lifecycle:
        1. Register each session via :meth:`track`.
        2. Call :meth:`start` to launch the background task.
        3. Call :meth:`stop` on supervisor shutdown — flushes one
           final sync of every tracked session before returning.
    """

    def __init__(
        self,
        *,
        bucket: str,
        key_prefix: str,
        memory_root: str | Path,
        interval_s: float = DEFAULT_SYNC_INTERVAL_S,
        s3_client: Any | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3MemorySync requires a non-empty bucket")
        self._bucket = bucket
        self._key_prefix = key_prefix.strip("/") if key_prefix else ""
        self._memory_root = Path(memory_root)
        self._interval_s = interval_s
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )

        if s3_client is not None:
            self._s3 = s3_client
        else:
            if boto3 is None:  # pragma: no cover
                raise RuntimeError(
                    "boto3 not installed — pass s3_client explicitly in tests"
                )
            self._s3 = boto3.client("s3")

        self._tracked: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_upload_error: Exception | None = None
        self._upload_count = 0
        self._failure_count = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def upload_count(self) -> int:
        return self._upload_count

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def last_error(self) -> Exception | None:
        return self._last_upload_error

    @property
    def tracked_sessions(self) -> frozenset[str]:
        return frozenset(self._tracked)

    # ------------------------------------------------------------------
    # Session registration
    # ------------------------------------------------------------------

    def track(self, session_id: str) -> None:
        """Register a session id so it is synced on every tick.

        Calling with an id that is already tracked is a no-op.
        """
        self._tracked.add(session_id)

    def untrack(self, session_id: str) -> None:
        self._tracked.discard(session_id)

    # ------------------------------------------------------------------
    # Sync primitives
    # ------------------------------------------------------------------

    def build_s3_key(self, session_id: str) -> str:
        """Build the S3 key for one sync of one session.

        Shape: ``<key_prefix>/<session_id>/<timestamp>.tar.gz``
        """
        timestamp = self._clock()
        parts = [p for p in (self._key_prefix, session_id) if p]
        parts.append(f"{timestamp}.tar.gz")
        return "/".join(parts)

    async def sync_session(self, session_id: str) -> str | None:
        """Upload one session's memory tarball. Returns the S3 key on
        success, ``None`` on failure (the failure is logged + tracked
        via :attr:`last_error`)."""
        memory_dir = self._memory_root / session_id / "memory"
        tarball = build_memory_tarball(memory_dir)
        key = self.build_s3_key(session_id)
        try:
            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=tarball,
                ContentType="application/gzip",
            )
        except Exception as exc:  # noqa: BLE001
            self._failure_count += 1
            self._last_upload_error = exc
            _log.warning(
                "S3 memory sync failed for session=%s key=%s: %s",
                session_id,
                key,
                exc,
            )
            return None
        self._upload_count += 1
        return key

    async def sync_all_tracked(self) -> dict[str, str | None]:
        """Sync every tracked session once. Returns a mapping of
        session_id → S3 key (or None on failure)."""
        results: dict[str, str | None] = {}
        for sid in list(self._tracked):
            results[sid] = await self.sync_session(sid)
        return results

    # ------------------------------------------------------------------
    # Background task
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the periodic sync task. Idempotent."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name="mars-memory-sync"
        )

    async def stop(self) -> None:
        """Signal stop, wait for the loop to exit, do one final sync.

        Robust to a crashed background task — catches any exception
        raised from ``await task`` and proceeds with the final drain.
        """
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._interval_s + 10)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                _log.exception("S3 memory sync background task raised on stop")
            finally:
                self._task = None
        # Final drain
        await self.sync_all_tracked()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_s
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.sync_all_tracked()
            except Exception:  # noqa: BLE001
                _log.exception("S3 memory sync loop iteration failed")
