from __future__ import annotations

import csv
import io
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from bip_api.models import DownloadRequest


@dataclass
class _Entry:
    filename: str
    data: bytes
    expires_at: float


class ReportCache:
    def __init__(self, ttl_seconds: int, maxsize: int = 128) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._store: OrderedDict[tuple[str, ...], _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, req: DownloadRequest) -> tuple[str, ...]:
        return (req.report_path, req.customer_name or "", req.from_date or "", req.to_date or "")

    def get(self, req: DownloadRequest) -> tuple[str, bytes] | None:
        key = self._key(req)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return (entry.filename, entry.data)

    def set(self, req: DownloadRequest, filename: str, data: bytes) -> None:
        key = self._key(req)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = _Entry(
                filename=filename, data=data, expires_at=time.monotonic() + self._ttl
            )
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)


class ParsedCSVCache:
    def __init__(self, maxsize: int = 8) -> None:
        self._lock = threading.Lock()
        self._store: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
        self._maxsize = maxsize

    def get(self, filename: str, csv_bytes: bytes) -> list[dict[str, str]]:
        with self._lock:
            if filename in self._store:
                self._store.move_to_end(filename)
                return self._store[filename]
            rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8", errors="replace"))))
            self._store[filename] = rows
            if len(self._store) > self._maxsize:
                self._store.popitem(last=False)
            return rows
