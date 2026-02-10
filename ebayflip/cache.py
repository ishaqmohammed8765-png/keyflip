from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


@dataclass(slots=True)
class CachedResponse:
    text: str
    status_code: int
    headers: dict[str, str]

    def raise_for_status(self) -> None:
        response = requests.Response()
        response.status_code = self.status_code
        if self.status_code >= 400:
            raise requests.HTTPError(f"Status code {self.status_code}")

    def json(self) -> dict:
        return json.loads(self.text)


class CacheStore:
    def __init__(self, path: str, ttl_seconds: int = 600) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._init_db()

    def _init_db(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS http_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_text TEXT,
                    status_code INTEGER,
                    headers_json TEXT,
                    created_at REAL
                )
                """
            )

    def get(self, key: str) -> Optional[CachedResponse]:
        now = time.time()
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT response_text, status_code, headers_json, created_at FROM http_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        created_at = row[3]
        if now - created_at > self.ttl_seconds:
            self.delete(key)
            return None
        headers = json.loads(row[2]) if row[2] else {}
        return CachedResponse(text=row[0], status_code=row[1], headers=headers)

    def set(self, key: str, response: requests.Response) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO http_cache
                    (cache_key, response_text, status_code, headers_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    key,
                    response.text,
                    response.status_code,
                    json.dumps(dict(response.headers)),
                    time.time(),
                ),
            )

    def delete(self, key: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM http_cache WHERE cache_key = ?", (key,))

    def purge_blocked_responses(self, tokens: list[str]) -> int:
        if not tokens:
            return 0
        normalized = [token.strip().lower() for token in tokens if token and token.strip()]
        if not normalized:
            return 0
        where_clause = " OR ".join("LOWER(response_text) LIKE ?" for _ in normalized)
        params = tuple(f"%{token}%" for token in normalized)
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(f"DELETE FROM http_cache WHERE {where_clause}", params)
            return int(cursor.rowcount or 0)
