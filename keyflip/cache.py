from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class CacheEntry:
    value: Optional[float]
    currency: str
    expires_at: float
    ok: bool
    notes: str


class PriceCache:
    """
    Simple SQLite cache with TTL + a "recent seen" table.

    Improvements vs original:
    - Adds timeout to reduce "database is locked"
    - Normalizes currency
    - Provides prune() and opportunistic cleanup of expired entries
    - Adds indexes for scale
    - Makes expires_at always non-null
    """

    def __init__(self, db_path: Path, *, timeout_s: float = 10.0):
        self.db_path = Path(db_path)
        self.timeout_s = float(timeout_s)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self.timeout_s)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS price_cache (
                    url        TEXT PRIMARY KEY,
                    value      REAL,
                    currency   TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    ok         INTEGER NOT NULL,
                    notes      TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS recent_seen (
                    key     TEXT PRIMARY KEY,
                    seen_at REAL NOT NULL
                )
                """
            )
            # Helpful indexes when DB grows
            con.execute("CREATE INDEX IF NOT EXISTS idx_price_cache_expires ON price_cache(expires_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_recent_seen_seen_at ON recent_seen(seen_at)")

    # ---------- Maintenance ----------
    def clear_cache(self) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM price_cache")

    def clear_recent(self) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM recent_seen")

    def prune(self) -> None:
        """Remove expired cache entries."""
        now = time.time()
        with self._connect() as con:
            con.execute("DELETE FROM price_cache WHERE expires_at < ?", (now,))

    def prune_recent(self, *, keep_days: int = 30) -> None:
        """Optional: prevent recent_seen from growing forever."""
        if keep_days <= 0:
            return
        cutoff = time.time() - keep_days * 86400
        with self._connect() as con:
            con.execute("DELETE FROM recent_seen WHERE seen_at < ?", (cutoff,))

    # ---------- Cache ----------
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
            expires_at = float(expires_at)

            if expires_at < now:
                # Opportunistic cleanup so DB doesn't grow forever
                con.execute("DELETE FROM price_cache WHERE url=?", (url,))
                return None

        return CacheEntry(
            value=None if value is None else float(value),
            currency=(currency or "GBP").strip().upper(),
            expires_at=expires_at,
            ok=bool(int(ok)),
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
        expires_at = now + max(0, int(ttl_s))
        cur = (currency or "GBP").strip().upper()
        note = notes or ""

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
                """,
                (url, value, cur, expires_at, int(bool(ok)), note, now),
            )

    # ---------- Recent tracking ----------
    def mark_recent(self, key: str) -> None:
        k = (key or "").strip()
        if not k:
            return
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO recent_seen(key, seen_at) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET seen_at=excluded.seen_at
                """,
                (k, time.time()),
            )

    def is_recent(self, key: str, avoid_days: int) -> bool:
        if avoid_days <= 0:
            return False
        k = (key or "").strip()
        if not k:
            return False

        cutoff = time.time() - int(avoid_days) * 86400

        with self._connect() as con:
            row = con.execute("SELECT seen_at FROM recent_seen WHERE key=?", (k,)).fetchone()

        if not row:
            return False
        return float(row[0]) >= cutoff
