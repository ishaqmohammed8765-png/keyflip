from __future__ import annotations

import sqlite3
import time

from ebayflip.cache import CacheStore


def test_purge_blocked_responses_removes_matching_rows(tmp_path) -> None:
    cache = CacheStore(str(tmp_path / "cache.sqlite"), ttl_seconds=3600)
    now = time.time()
    with sqlite3.connect(str(tmp_path / "cache.sqlite")) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO http_cache (cache_key, response_text, status_code, headers_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("k1", "normal content", 200, "{}", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO http_cache (cache_key, response_text, status_code, headers_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("k2", "Please verify you are human before continuing", 200, "{}", now),
        )
    removed = cache.purge_blocked_responses(["verify you are human"])
    assert removed == 1
    assert cache.get("k1") is not None
    assert cache.get("k2") is None
