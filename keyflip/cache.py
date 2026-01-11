from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class CacheEntry:
    value: Optional[float]
    currency: str
    expires_at: float
    ok: bool
    notes: str


class PriceCache:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS price_cache (
                    url TEXT PRIMARY KEY,
                    value REAL,
                    currency TEXT,
                    expires_at REAL,
                    ok INTEGER,
                    notes TEXT,
                    updated_at REAL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS recent_seen (
                    key TEXT PRIMARY KEY,
                    seen_at REAL
                )
                """
            )

    def clear_cache(self) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM price_cache")

    def clear_recent(self) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM recent_seen")

    def get(self, url: str) -> Optional[CacheEntry]:
        now = time.time()
        with self._connect() as con:
            row = con.execute(
                "SELECT value, currency, expires_at, ok, notes FROM price_cache WHERE url=?",
                (url,),
            ).fetchone()
        if not row:
            return None
        value, currency, expires_at, ok, notes = row
        if expires_at is not None and float(expires_at) < now:
            return None
        return CacheEntry(
            value=None if value is None else float(value),
            currency=currency or "GBP",
            expires_at=float(expires_at) if expires_at is not None else now,
            ok=bool(ok),
            notes=notes or "",
        )

    def set(
        self,
        url: str,
        value: Optional[float],
        currency: str,
        ttl_s: int,
        ok: bool,
        notes: str,
    ) -> None:
        now = time.time()
        expires_at = now + ttl_s
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO price_cache(url, value, currency, expires_at, ok, notes, updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    value=excluded.value,
                    currency=excluded.currency,
                    expires_at=excluded.expires_at,
                    ok=excluded.ok,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """
                ,
                (url, value, currency, expires_at, int(ok), notes, now),
            )

    def mark_recent(self, key: str) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO recent_seen(key, seen_at) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET seen_at=excluded.seen_at
                """,
                (key, time.time()),
            )

    def is_recent(self, key: str, avoid_days: int) -> bool:
        if avoid_days <= 0:
            return False
        cutoff = time.time() - avoid_days * 86400
        with self._connect() as con:
            row = con.execute(
                "SELECT seen_at FROM recent_seen WHERE key=?",
                (key,),
            ).fetchone()
        if not row:
            return False
        return float(row[0]) >= cutoff

