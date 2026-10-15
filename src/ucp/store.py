"""Checkout sessions and idempotency records, in a small SQLite file.

UCP checkouts are edited over several calls (create, update…, complete), so
they need state that survives a restart. Rows expire with the session; a
purge runs on writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from src.config.settings import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkouts (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status INTEGER NOT NULL,
    response TEXT NOT NULL,
    expires REAL NOT NULL
);
"""

_IDEMPOTENCY_TTL = 24 * 3600


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._writes = 0

    def get(self, checkout_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM checkouts WHERE id = ? AND expires > ?", (checkout_id, time.time())
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, state: dict, expires: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO checkouts (id, data, expires) VALUES (?, ?, ?)",
                (state["id"], json.dumps(state, separators=(",", ":")), expires),
            )
            self._maybe_purge()

    def idem_get(self, key: str) -> Optional[tuple[str, int, dict]]:
        with self._lock:
            row = self._db.execute(
                "SELECT request_hash, status, response FROM idempotency WHERE key = ? AND expires > ?",
                (key, time.time()),
            ).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    def idem_put(self, key: str, request_hash: str, status: int, response: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO idempotency (key, request_hash, status, response, expires) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, request_hash, status, json.dumps(response, separators=(",", ":")),
                 time.time() + _IDEMPOTENCY_TTL),
            )
            self._maybe_purge()

    def _maybe_purge(self) -> None:
        self._writes += 1
        if self._writes % 200 == 0:
            now = time.time()
            self._db.execute("DELETE FROM checkouts WHERE expires < ?", (now - 86400,))
            self._db.execute("DELETE FROM idempotency WHERE expires < ?", (now,))


_store: Optional[Store] = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.ucp_db_path)
    return _store


def set_store(s: Optional[Store]) -> None:
    """Tests: use an in-memory store."""
    global _store
    _store = s
