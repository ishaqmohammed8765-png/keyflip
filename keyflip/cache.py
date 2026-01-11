from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class CacheEntry:
    value: Optional[float]
    currency: str
    expires_at: float
    ok: bool
    notes: str


class PriceCache:
    """
    SQLite price cache with TTL + 'recent seen' tracking.

    Key improvements vs your current version:
    - Single persistent connection (less overhead/lock churn)
    - PRAGMA busy_timeout + small retry for transient "database is locked"
    - Centralized normalization helpers
    - Optional convenience helpers: get_or_set(), set_many(), stats(), close()
    """

    def __init__(
        self,
        db_path: Path,
        *,
        timeout_s: float = 10.0,
        busy_timeout_ms: int = 8000,
        retries: int = 3,
        retry_sleep_s: float = 0.12,
    ):
        self.db_path = Path(db_path)
        self.timeout_s = float(timeout_s)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.retries = int(retries)
        self.retry_sleep_s = float(retry_sleep_s)

        self._con = self._connect()
        self._init_db()

    # ---------- Connection / PRAGMAs ----------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self.timeout_s)
        con.row_factory = sqlite3.Row

        # WAL reduces writer/reader contention, but does not eliminate write locks.
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA foreign_keys=ON;")

        # Helps when a lock exists briefly (common with concurrent reads/writes)
        con.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms};")
        return con

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass

    def __enter__(self) -> "PriceCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------- Helpers ----------
    @staticmethod
    def _norm_currency(currency: str) -> str:
        return (currency or "GBP").strip().upper()

    @staticmethod
    def _norm_key(key: str) -> str:
        return (key or "").strip()

    @staticmethod
    def _norm_url(url: str) -> str:
        # Keep it simple: trim whitespace. (Don’t over-normalize URLs unless you’re sure.)
        return (url or "").strip()

    def _with_retry(self, fn: Callable[[], T]) -> T:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "database is locked" in msg or "database table is locked" in msg:
                    last_err = e
                    if attempt < self.retries:
                        time.sleep(self.retry_sleep_s * (attempt + 1))
                        continue
                raise
        assert last_err is not None
        raise last_err

    def _init_db(self) -> None:
        def _do() -> None:
            with self._con:
                self._con.execute(
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
                self._con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recent_seen (
                        key     TEXT PRIMARY KEY,
                        seen_at REAL NOT NULL
                    )
                    """
                )
                self._con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_price_cache_expires ON price_cache(expires_at)"
                )
                self._con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_recent_seen_seen_at ON recent_seen(seen_at)"
                )

        self._with_retry(_do)

    # ---------- Maintenance ----------
    def clear_cache(self) -> None:
        def _do() -> None:
            with self._con:
                self._con.execute("DELETE FROM price_cache")
        self._with_retry(_do)

    def clear_recent(self) -> None:
        def _do() -> None:
            with self._con:
                self._con.execute("DELETE FROM recent_seen")
        self._with_retry(_do)

    def prune(self) -> int:
        """Remove expired cache entries. Returns number of rows deleted."""
        now = time.time()

        def _do() -> int:
            with self._con:
                cur = self._con.execute("DELETE FROM price_cache WHERE expires_at < ?", (now,))
                return int(cur.rowcount)

        return self._with_retry(_do)

    def prune_recent(self, *, keep_days: int = 30) -> int:
        """Remove old 'recent seen' rows. Returns number of rows deleted."""
        if keep_days <= 0:
            return 0
        cutoff = time.time() - int(keep_days) * 86400

        def _do() -> int:
            with self._con:
                cur = self._con.execute("DELETE FROM recent_seen WHERE seen_at < ?", (cutoff,))
                return int(cur.rowcount)

        return self._with_retry(_do)

    def vacuum(self) -> None:
        """Optional: reclaim disk space (can be slow)."""
        def _do() -> None:
            # VACUUM cannot run inside a transaction
            self._con.execute("VACUUM")
        self._with_retry(_do)

    # ---------- Cache ----------
    def get(self, url: str) -> Optional[CacheEntry]:
        u = self._norm_url(url)
        if not u:
            return None

        now = time.time()

        def _do() -> Optional[CacheEntry]:
            row = self._con.execute(
                "SELECT value, currency, expires_at, ok, notes FROM price_cache WHERE url=?",
                (u,),
            ).fetchone()

            if not row:
                return None

            expires_at = float(row["expires_at"])
            if expires_at < now:
                # Opportunistic cleanup
                with self._con:
                    self._con.execute("DELETE FROM price_cache WHERE url=?", (u,))
                return None

            value = row["value"]
            return CacheEntry(
                value=None if value is None else float(value),
                currency=self._norm_currency(row["currency"]),
                expires_at=expires_at,
                ok=bool(int(row["ok"])),
                notes=(row["notes"] or ""),
            )

        # Reads usually don't need retries, but it’s harmless and helps rare lock edge cases.
        return self._with_retry(_do)

    def set(
        self,
        url: str,
        value: Optional[float],
        currency: str,
        ttl_s: int,
        ok: bool,
        notes: str,
    ) -> None:
        u = self._norm_url(url)
        if not u:
            return

        now = time.time()
        expires_at = now + max(0, int(ttl_s))
        cur = self._norm_currency(currency)
        note = notes or ""

        def _do() -> None:
            with self._con:
                self._con.execute(
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
                    (u, value, cur, expires_at, int(bool(ok)), note, now),
                )

        self._with_retry(_do)

    def set_many(self, items: list[tuple[str, Optional[float], str, int, bool, str]]) -> None:
        """
        Bulk set in a single transaction.
        items: [(url, value, currency, ttl_s, ok, notes), ...]
        """
        now = time.time()

        rows = []
        for url, value, currency, ttl_s, ok, notes in items:
            u = self._norm_url(url)
            if not u:
                continue
            expires_at = now + max(0, int(ttl_s))
            rows.append(
                (u, value, self._norm_currency(currency), expires_at, int(bool(ok)), notes or "", now)
            )

        if not rows:
            return

        def _do() -> None:
            with self._con:
                self._con.executemany(
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
                    rows,
                )

        self._with_retry(_do)

    def get_or_set(
        self,
        url: str,
        *,
        currency: str,
        ttl_s: int,
        fetch: Callable[[], tuple[Optional[float], bool, str]],
    ) -> Optional[CacheEntry]:
        """
        Convenience:
        - If cached & not expired -> return it
        - Else call fetch() -> (value, ok, notes), store -> return stored entry
        """
        hit = self.get(url)
        if hit is not None:
            return hit

        value, ok, notes = fetch()
        self.set(url, value, currency=currency, ttl_s=ttl_s, ok=ok, notes=notes)
        return self.get(url)

    # ---------- Recent tracking ----------
    def mark_recent(self, key: str) -> None:
        k = self._norm_key(key)
        if not k:
            return
        seen_at = time.time()

        def _do() -> None:
            with self._con:
                self._con.execute(
                    """
                    INSERT INTO recent_seen(key, seen_at) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET seen_at=excluded.seen_at
                    """,
                    (k, seen_at),
                )

        self._with_retry(_do)

    def is_recent(self, key: str, avoid_days: int) -> bool:
        if avoid_days <= 0:
            return False
        k = self._norm_key(key)
        if not k:
            return False

        cutoff = time.time() - int(avoid_days) * 86400

        def _do() -> bool:
            row = self._con.execute("SELECT seen_at FROM recent_seen WHERE key=?", (k,)).fetchone()
            if not row:
                return False
            return float(row["seen_at"]) >= cutoff

        return self._with_retry(_do)

    # ---------- Diagnostics ----------
    def stats(self) -> dict[str, int]:
        """Quick counts for debugging/health checks."""
        def _do() -> dict[str, int]:
            pc = self._con.execute("SELECT COUNT(*) AS n FROM price_cache").fetchone()["n"]
            rs = self._con.execute("SELECT COUNT(*) AS n FROM recent_seen").fetchone()["n"]
            return {"price_cache": int(pc), "recent_seen": int(rs)}
        return self._with_retry(_do)
