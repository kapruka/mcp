"""Shared in-process TTL cache used by the API client.

Single instance, bounded size (LRU-evicted on overflow), per-entry TTL.
Suitable for a single-process MCP server. Move to Redis if we ever scale
horizontally.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Any


class TTLCache:
    def __init__(self, max_entries: int = 2000) -> None:
        self._max = max_entries
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        expires_at = time.monotonic() + ttl_seconds
        with self._lock:
            self._store[key] = (expires_at, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._store),
                "max": self._max,
                "hits": self._hits,
                "misses": self._misses,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


cache = TTLCache(max_entries=2000)
