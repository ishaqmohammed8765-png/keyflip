from __future__ import annotations

import random
import time
import uuid
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .cache import PriceCache
from .config import (
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
from .fanatical_pw import harvest_game_links, read_title_and_price_gbp


log = logging.getLogger("keyflip.core")
log.info("Fanatical backend: Playwright")


# ============================================================
# Config
# ============================================================

@dataclass
class RunConfig:
    root: Path
    max_buy_gbp: float
    watchlist_target: int
    verify_candidates: int
    pages_per_source: int
    verify_limit: int
    verify_safety_cap: int
    scan_limit: int
    avoid_recent_days: int
    allow_eur: bool
    eur_to_gbp: float
    item_budget_s: float
    run_budget_s: float
    cache_fail_ttl_s: int


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
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _buy_key(url: str) -> str:
    return url.split("?")[0].rstrip("/").lower()


def _budget_ok(start: float, limit: float) -> bool:
    return limit <= 0 or (time.time() - start) <= limit


def _ttl_fail(cfg: RunConfig) -> int:
    return int(cfg.cache_fail_ttl_s) if cfg.cache_fail_ttl_s > 0 else int(PRICE_FAIL_TTL_S)


def _join_notes(*xs: str) -> str:
    return "; ".join(x.strip() for x in xs if x and str(x).strip())


# ============================================================
# Cache wrappers
# ============================================================

def get_buy(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
) -> Tuple[Optional[str], Optional[float], str]:

    c = cache.get(url)
    if c:
        if c.ok and c.value is not None:
            return None, float(c.value), f"cache: {c.notes}"
        return None, None, f"cache-fail: {c.notes}"

    title, price, notes = read_title_and_price_gbp(url)

    if price is None:
        cache.set(url, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return title, None, notes

    cache.set(url, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return title, float(price), notes


def get_sell(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
) -> Tuple[Optional[float], str]:

    c = cache.get(url)
    if c:
        if c.ok and c.value is not None:
            return float(c.value), f"cache: {c.notes}"
        return None, f"cache-fail: {c.notes}"

    price, notes = read_price_gbp(url)

    if price is None:
        cache.set(url, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return None, notes

    cache.set(url, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return float(price), notes


# ============================================================
# BUILD — Fanatical (Playwright)
# ============================================================

def build_watchlist(cfg: RunConfig, cache: PriceCache, out_csv: Path) -> pd.DataFrame:
    start = time.time()
    links: List[str] = []

    for src in FANATICAL_SOURCES.values():
        if not _budget_ok(start, cfg.run_budget_s):
            break
        links.extend(harvest_game_links(src, pages=cfg.pages_per_source))

    links = _dedupe(links)
    random.shuffle(links)

    pool = links[: cfg.verify_candidates] if cfg.verify_candidates > 0 else links

    rows: List[Dict[str, str]] = []
    kept = attempted = 0

    for url in pool:
        if not _budget_ok(start, cfg.run_budget_s):
            break
        if kept >= cfg.watchlist_target:
            break
        if attempted >= cfg.verify_limit:
            break

        key = _buy_key(url)
        if cache.is_recent(key, cfg.avoid_recent_days):
            continue

        attempted += 1
        t0 = time.time()

        title, buy, notes = get_buy(cfg, cache, url)

        if cfg.item_budget_s > 0 and (time.time() - t0) > cfg.item_budget_s:
            continue
        if buy is None or buy > cfg.max_buy_gbp:
            continue

        rows.append(
            {
                "title": title or url.rsplit("/", 1)[-1],
                "buy_url": url,
                "buy_price_gbp": f"{buy:.2f}",
                "buy_notes": notes,
            }
        )
        kept += 1
        cache.mark_recent(key)

    df = pd.DataFrame(rows, columns=WATCHLIST_COLS)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)
    return df


# ============================================================
# SCAN — Eneba
# ============================================================

def scan_watchlist(
    cfg: RunConfig,
    cache: PriceCache,
    watchlist_csv: Path,
    scans_csv: Path,
    passes_csv: Path,
) -> pd.DataFrame:

    if not watchlist_csv.exists():
        return pd.DataFrame(columns=SCANS_COLS)

    try:
        watch = pd.read_csv(watchlist_csv).fillna("")
    except Exception:
        return pd.DataFrame(columns=SCANS_COLS)

    if watch.empty:
        return pd.DataFrame(columns=SCANS_COLS)

    watch = watch.sample(frac=1.0).reset_index(drop=True)

    rows: List[Dict[str, object]] = []
    start = time.time()
    batch_id = uuid.uuid4().hex[:12]

    for _, row in watch.iterrows():
        if not _budget_ok(start, cfg.run_budget_s):
            break
        if len(rows) >= cfg.scan_limit:
            break

        title = str(row["title"])
        buy_url = str(row["buy_url"])
        t0 = time.time()

        _, buy, buy_notes = get_buy(cfg, cache, buy_url)

        store_url = make_store_search_url(title)
        prod_url, resolve_notes = resolve_product_url_from_store(store_url, title)

        sell, sell_notes = (None, "")
        if prod_url:
            sell, sell_notes = get_sell(cfg, cache, prod_url)

        market_after_fee = buffer = profit = roi = None
        passes = False

        if buy and sell:
            market_after_fee = sell * (1 - SELL_FEE_PCT)
            buffer = BUFFER_FIXED_GBP + BUFFER_PCT_OF_BUY * buy
            profit = market_after_fee - buy - buffer
            roi = profit / buy if buy > 0 else None
            passes = profit >= MIN_PROFIT_GBP and roi >= MIN_ROI

        rows.append(
            {
                "batch_id": batch_id,
                "timestamp": _now(),
                "title": title,
                "buy_price": buy,
                "market_price": sell,
                "market_after_fee": market_after_fee,
                "buffer": buffer,
                "edge": profit,
                "edge_pct": roi,
                "passes": passes,
                "buy_url": buy_url,
                "market_url": prod_url or store_url,
                "buy_notes": buy_notes,
                "market_notes": _join_notes(resolve_notes, sell_notes),
                "elapsed_s": round(time.time() - t0, 2),
            }
        )

    batch = pd.DataFrame(rows, columns=SCANS_COLS)
    scans_csv.parent.mkdir(parents=True, exist_ok=True)
    passes_csv.parent.mkdir(parents=True, exist_ok=True)

    try:
        prev = pd.read_csv(scans_csv)
        pd.concat([prev, batch]).to_csv(scans_csv, index=False)
    except Exception:
        batch.to_csv(scans_csv, index=False)

    batch[batch["passes"] == True].to_csv(passes_csv, index=False)
    return batch
