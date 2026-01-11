from __future__ import annotations

import random
import time
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
from .fanatical import harvest_game_links, read_title_and_price_gbp


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


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _dedupe(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _buy_key(url: str) -> str:
    return url.split("?")[0].rstrip("/").lower()


def get_cached_or_fetch_buy(cache: PriceCache, url: str) -> Tuple[Optional[float], str]:
    c = cache.get(url)
    if c is not None:
        if c.ok and c.value is not None:
            return c.value, f"cache: {c.notes}"
        if not c.ok:
            return None, f"cache-fail: {c.notes}"

    title, price, notes = read_title_and_price_gbp(url)
    if price is None:
        cache.set(url, None, "GBP", ttl_s=PRICE_FAIL_TTL_S, ok=False, notes=notes)
        return None, notes

    cache.set(url, float(price), "GBP", ttl_s=PRICE_OK_TTL_S, ok=True, notes=notes)
    return float(price), notes


def get_cached_or_fetch_sell(cache: PriceCache, product_url: str) -> Tuple[Optional[float], str]:
    c = cache.get(product_url)
    if c is not None:
        if c.ok and c.value is not None:
            return c.value, f"cache: {c.notes}"
        if not c.ok:
            return None, f"cache-fail: {c.notes}"

    price, notes = read_price_gbp(product_url)
    if price is None:
        cache.set(product_url, None, "GBP", ttl_s=PRICE_FAIL_TTL_S, ok=False, notes=notes)
        return None, notes

    cache.set(product_url, float(price), "GBP", ttl_s=PRICE_OK_TTL_S, ok=True, notes=notes)
    return float(price), notes


def build_watchlist(cfg: RunConfig, cache: PriceCache, out_watchlist: Path) -> pd.DataFrame:
    """
    Harvest Fanatical links -> verify buy <= max_buy -> write watchlist.csv.
    IMPORTANT: Always writes headers even if 0 rows.
    """
    start = time.time()
    all_links: List[str] = []
    for _, src_url in FANATICAL_SOURCES.items():
        all_links.extend(harvest_game_links(src_url, pages=cfg.pages_per_source))

    all_links = _dedupe(all_links)
    random.shuffle(all_links)

    pool = all_links[: cfg.verify_candidates] if cfg.verify_candidates > 0 else all_links

    verified_rows: List[Dict[str, str]] = []
    checked = 0
    kept = 0

    hard_cap = cfg.verify_safety_cap if cfg.verify_safety_cap > 0 else 999999
    verify_limit = cfg.verify_limit if cfg.verify_limit > 0 else 999999
    target = cfg.watchlist_target

    for url in pool:
        if cfg.run_budget_s > 0 and (time.time() - start) > cfg.run_budget_s:
            break
        if checked >= verify_limit or checked >= hard_cap or kept >= target:
            break

        checked += 1
        key = _buy_key(url)
        if cache.is_recent(key, cfg.avoid_recent_days):
            continue

        buy_price, buy_notes = get_cached_or_fetch_buy(cache, url)
        if buy_price is None:
            continue
        if buy_price > cfg.max_buy_gbp:
            continue

        title, _, _ = read_title_and_price_gbp(url)
        title = (title or "").strip() or url.rsplit("/", 1)[-1]

        verified_rows.append(
            {
                "title": title,
                "buy_url": url,
                "buy_price_gbp": f"{buy_price:.2f}",
                "buy_notes": buy_notes,
            }
        )
        kept += 1
        cache.mark_recent(key)

    # ALWAYS include columns so pandas writes headers even when empty
    df = pd.DataFrame(verified_rows, columns=WATCHLIST_COLS)
    df.to_csv(out_watchlist, index=False)
    return df


def _read_watchlist_safe(watchlist_csv: Path) -> pd.DataFrame:
    """
    Reads watchlist.csv safely (handles totally empty file).
    Always returns a DF with expected columns.
    """
    if not watchlist_csv.exists():
        return pd.DataFrame(columns=WATCHLIST_COLS)

    try:
        df = pd.read_csv(watchlist_csv).fillna("")
        # if file exists but has wrong columns, normalize
        for c in WATCHLIST_COLS:
            if c not in df.columns:
                df[c] = ""
        return df[WATCHLIST_COLS]
    except Exception:
        return pd.DataFrame(columns=WATCHLIST_COLS)


def _ensure_scans_files(scans_csv: Path, passes_csv: Path) -> None:
    """
    Ensure scans.csv and passes.csv exist with headers.
    """
    empty_scans = pd.DataFrame(columns=SCANS_COLS)

    if not scans_csv.exists():
        empty_scans.to_csv(scans_csv, index=False)
    else:
        # if scans exists but is invalid/empty without headers, rewrite with headers
        try:
            pd.read_csv(scans_csv)
        except Exception:
            empty_scans.to_csv(scans_csv, index=False)

    if not passes_csv.exists():
        empty_scans.to_csv(passes_csv, index=False)
    else:
        try:
            pd.read_csv(passes_csv)
        except Exception:
            empty_scans.to_csv(passes_csv, index=False)


def scan_watchlist(
    cfg: RunConfig,
    cache: PriceCache,
    watchlist_csv: Path,
    scans_csv: Path,
    passes_csv: Path,
) -> pd.DataFrame:
    """
    Read watchlist.csv -> resolve Eneba -> compute edge -> append scans.csv and write passes.csv (latest batch).
    Gracefully handles empty watchlist (no crash).
    """
    w = _read_watchlist_safe(watchlist_csv)

    # make sure output files exist with headers for the UI
    _ensure_scans_files(scans_csv, passes_csv)

    if w.empty:
        # Return empty batch (with headers) and do nothing else
        return pd.DataFrame(columns=SCANS_COLS)

    rows_out: List[Dict[str, object]] = []
    start = time.time()
    scanned = 0
    limit = cfg.scan_limit if cfg.scan_limit > 0 else 999999

    for _, row in w.iterrows():
        if cfg.run_budget_s > 0 and (time.time() - start) > cfg.run_budget_s:
            break
        if scanned >= limit:
            break

        title = str(row.get("title", "")).strip() or "Unknown"
        buy_url = str(row.get("buy_url", "")).strip()
        if not buy_url:
            continue

        scanned += 1
        t0 = time.time()

        buy_price, buy_notes = get_cached_or_fetch_buy(cache, buy_url)

        # resolve eneba product
        store_url = make_store_search_url(title)
        prod_url, resolve_notes = resolve_product_url_from_store(store_url)

        sell_price = None
        sell_notes = ""
        if prod_url:
            sell_price, sell_notes = get_cached_or_fetch_sell(cache, prod_url)
        else:
            sell_notes = resolve_notes

        # compute edge
        market_after_fee = None
        buffer = None
        profit = None
        roi = None
        passes = False

        if buy_price is not None and sell_price is not None:
            market_after_fee = sell_price * (1.0 - SELL_FEE_PCT)
            buffer = BUFFER_FIXED_GBP + (BUFFER_PCT_OF_BUY * buy_price)
            profit = market_after_fee - buy_price - buffer
            roi = (profit / buy_price) if buy_price and buy_price > 0 else None
            if profit is not None and roi is not None:
                passes = (profit >= MIN_PROFIT_GBP) and (roi >= MIN_ROI)

        rows_out.append(
            {
                "timestamp": _now_iso(),
                "title": title,
                "buy_price": buy_price,
                "market_price": sell_price,
                "market_after_fee": market_after_fee,
                "buffer": buffer,
                "edge": profit,
                "edge_pct": roi,
                "passes": passes,
                "buy_url": buy_url,
                "market_url": prod_url or store_url,
                "buy_notes": buy_notes,
                "market_notes": f"{resolve_notes}; {sell_notes}".strip("; "),
                "elapsed_s": round(time.time() - t0, 2),
            }
        )

    batch = pd.DataFrame(rows_out, columns=SCANS_COLS)

    # append batch to scans.csv (but keep scans.csv valid if it was empty/invalid)
    try:
        old = pd.read_csv(scans_csv).fillna("")
        df_all = pd.concat([old, batch], ignore_index=True)
    except Exception:
        df_all = batch

    df_all.to_csv(scans_csv, index=False)

    # passes.csv = only latest batch passes
    if not batch.empty:
        latest_ts = batch["timestamp"].iloc[-1]
        latest = batch[batch["timestamp"] == latest_ts].copy()
        df_pass = latest[latest["passes"] == True].copy()
    else:
        df_pass = pd.DataFrame(columns=SCANS_COLS)

    df_pass.to_csv(passes_csv, index=False)
    return batch
