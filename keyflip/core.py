from __future__ import annotations

import logging
import random
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

import pandas as pd

from .cache import PriceCache
from .config import (
    RunConfig,
    BUFFER_FIXED_GBP,
    BUFFER_PCT_OF_BUY,
    FANATICAL_SOURCES,
    MIN_PROFIT_GBP,
    MIN_ROI,
    PRICE_FAIL_TTL_S,
    PRICE_OK_TTL_S,
    SELL_FEE_PCT,
)
from .eneba import make_store_search_url, read_price_gbp, resolve_product_url_from_store
from .fanatical_pw import FanaticalPWClient

log = logging.getLogger("keyflip.core")

WATCHLIST_COLS = ["title", "buy_url", "buy_price_gbp", "buy_notes"]
SCANS_COLS = [
    "batch_id",
    "timestamp",
    "title",
    "buy_price",
    "market_price",
    "market_after_fee",
    "buffer",
    "edge",
    "edge_pct",
    "passes",
    "buy_url",
    "market_url",
    "buy_notes",
    "market_notes",
    "elapsed_s",
]

# ============================================================
# Helpers
# ============================================================

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _dedupe(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _buy_key(url: str) -> str:
    return (url.split("?")[0].rstrip("/").lower()) if url else ""


def _budget_ok(start: float, limit_s: float) -> bool:
    return limit_s <= 0 or (time.time() - start) <= limit_s


def _recent_key(namespace: str, key: str) -> str:
    return f"{namespace}:{key}"


def _read_csv_safe(path: Path) -> pd.DataFrame:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return pd.DataFrame()
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _append_csv(path: Path, batch: pd.DataFrame, cols: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = _read_csv_safe(path)

    if prev.empty:
        batch.reindex(columns=cols).to_csv(path, index=False)
        return

    union_cols = list(dict.fromkeys(list(prev.columns) + cols))
    prev2 = prev.reindex(columns=union_cols)
    batch2 = batch.reindex(columns=union_cols)

    pd.concat([prev2, batch2], ignore_index=True).to_csv(path, index=False)


def _open_cache(db_path: Path) -> PriceCache:
    return PriceCache(db_path)


# ============================================================
# Watchlist builder
# ============================================================

def build_watchlist(cfg: RunConfig, out_csv: Path) -> int:
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    cache = _open_cache(out_csv.parent / "price_cache.sqlite")
    rows: List[Dict[str, object]] = []

    with FanaticalPWClient(headless=True) as fan:
        links: List[str] = []
        for src in FANATICAL_SOURCES.values():
            try:
                links.extend(fan.harvest_game_links(src, pages=2, max_links=200))
            except Exception as e:
                log.warning("Harvest failed: %s", e)

        links = _dedupe(links)
        random.shuffle(links)

        for url in links:
            title, price, notes = fan.read_title_and_price_gbp(url)
            if price is None:
                continue

            rows.append(
                {
                    "title": title,
                    "buy_url": url,
                    "buy_price_gbp": price,
                    "buy_notes": notes,
                }
            )

            if len(rows) >= getattr(cfg, "watchlist_target", 10):
                break

    pd.DataFrame(rows, columns=WATCHLIST_COLS).to_csv(out_csv, index=False)
    return len(rows)


# ============================================================
# Scan watchlist (FIXED: scans ALL items)
# ============================================================

def scan_watchlist(
    cfg: RunConfig,
    watchlist_path: Path,
    scans_path: Path,
    passes_path: Path,
    db_path: Path,
    fail_ttl: int,
) -> pd.DataFrame:
    cache = _open_cache(db_path)

    watch = _read_csv_safe(watchlist_path)
    if watch.empty:
        log.warning("Watchlist empty.")
        return pd.DataFrame(columns=SCANS_COLS)

    rows: List[Dict[str, object]] = []
    attempted = 0
    scan_limit = int(getattr(cfg, "scan_limit", 0) or 0)
    start = time.time()

    with FanaticalPWClient(headless=True) as fan:
        for _, r in watch.iterrows():
            if scan_limit > 0 and attempted >= scan_limit:
                break

            attempted += 1
            title = str(r.get("title", "")).strip()
            buy_url = str(r.get("buy_url", "")).strip()

            try:
                _, buy_price, buy_notes = fan.read_title_and_price_gbp(buy_url)
                store_url = make_store_search_url(title)
                prod_url, resolve_notes = resolve_product_url_from_store(store_url, title)

                sell_price = None
                sell_notes = ""
                if prod_url:
                    sell_price, sell_notes = read_price_gbp(prod_url)

                profit = roi = None
                passes = False
                if buy_price and sell_price:
                    after_fee = sell_price * (1 - SELL_FEE_PCT)
                    buffer = BUFFER_FIXED_GBP + BUFFER_PCT_OF_BUY * buy_price
                    profit = after_fee - buy_price - buffer
                    roi = profit / buy_price if buy_price else None
                    passes = profit >= MIN_PROFIT_GBP and roi >= MIN_ROI

                rows.append(
                    {
                        "batch_id": uuid.uuid4().hex[:12],
                        "timestamp": _now(),
                        "title": title,
                        "buy_price": buy_price,
                        "market_price": sell_price,
                        "market_after_fee": None,
                        "buffer": None,
                        "edge": profit,
                        "edge_pct": roi,
                        "passes": passes,
                        "buy_url": buy_url,
                        "market_url": prod_url or store_url,
                        "buy_notes": buy_notes,
                        "market_notes": resolve_notes or sell_notes,
                        "elapsed_s": round(time.time() - start, 2),
                    }
                )
            except Exception:
                log.exception("Scan failed for %s", buy_url)
                continue

    batch = pd.DataFrame(rows, columns=SCANS_COLS)
    _append_csv(scans_path, batch, SCANS_COLS)

    passes_path.parent.mkdir(parents=True, exist_ok=True)
    batch[batch["passes"] == True].to_csv(passes_path, index=False)

    return batch
