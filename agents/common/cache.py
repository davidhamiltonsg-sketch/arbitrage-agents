"""SQLite key/value cache with TTL, used to avoid re-paying for enrichment lookups."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

SEVEN_DAYS = 7 * 24 * 3600


def cache_key(namespace: str, identifier: str) -> str:
    digest = hashlib.sha256(identifier.strip().lower().encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


class Cache:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        self._conn.commit()

    def get(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT value, expires_at FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at < time.time():
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            self._conn.commit()
            return None
        return json.loads(value)

    def set(self, key: str, value: Any, ttl_seconds: float = SEVEN_DAYS) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time() + ttl_seconds),
        )
        self._conn.commit()

    def remember(self, namespace: str, identifier: str, fn: Callable[[], Any], ttl_seconds: float = SEVEN_DAYS) -> Any:
        key = cache_key(namespace, identifier)
        hit = self.get(key)
        if hit is not None:
            return hit
        value = fn()
        self.set(key, value, ttl_seconds)
        return value

    def close(self) -> None:
        self._conn.close()
