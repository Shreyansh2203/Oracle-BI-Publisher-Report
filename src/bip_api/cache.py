from __future__ import annotations

import time
from dataclasses import dataclass

from bip_api.models import DownloadRequest


@dataclass
class _Entry:
    filename: str
    data: bytes
    expires_at: float  # monotonic clock


class ReportCache:
    """Thread-safe in-memory cache with per-entry TTL."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, ...], _Entry] = {}

    def _key(self, req: DownloadRequest) -> tuple[str, ...]:
        return (req.report_path, req.customer_name or "", req.from_date or "", req.to_date or "")

    def get(self, req: DownloadRequest) -> tuple[str, bytes] | None:
        entry = self._store.get(self._key(req))
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            self._store.pop(self._key(req), None)
            return None
        return entry.filename, entry.data

    def set(self, req: DownloadRequest, filename: str, data: bytes) -> None:
        self._store[self._key(req)] = _Entry(
            filename=filename,
            data=data,
            expires_at=time.monotonic() + self._ttl,
        )
