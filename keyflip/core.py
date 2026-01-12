from __future__ import annotations

import logging
import random
import time
import uuid
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
from .fanatical_pw import FanaticalPWClient

log = logging.getLogger("keyflip.core")
log.info("Fanatical backend: Playwright (reused client)")

# ============================================================
# Config
# ============================================================

@dataclass
class RunConfig:
    """Configuration parameters for a Keyflip run."""
    root: Path
    max_buy_gbp: float
    watchlist_target: int
    verify_candidates: int
    pages_per_source: int
    verify_limit: int              # 0 = unlimited (if 0, will use verify_safety_cap)
    verify_safety_cap: int         # hard cap for verify_candidates if >0
    scan_limit: int
    avoid_recent_days: int         # number of days to avoid re-processing same item
    allow_eur: bool
    eur_to_gbp: float
    item_budget_s: float           # max time per item (seconds) before skipping
    run_budget_s: float            # max run duration (seconds) before aborting
    cache_fail_ttl_s: int          # override TTL for failed price lookups (seconds)

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
    """Return current timestamp string in YYYY-mm-dd HH:MM:SS format."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def _dedupe(items: List[str]) -> List[str]:
    """Remove duplicates from a list while preserving order."""
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _buy_key(url: str) -> str:
    """Normalize a URL for use as a cache key (strip query, trailing slash, lowercase)."""
    return (url.split("?")[0].rstrip("/").lower()) if url else ""

def _budget_ok(start: float, limit: float) -> bool:
    """Check if time elapsed since `start` is within `limit` seconds (0 means no limit)."""
    return limit <= 0 or (time.time() - start) <= limit

def _ttl_fail(cfg: RunConfig) -> int:
    """Determine TTL for a failed price lookup (use config override if set)."""
    return int(cfg.cache_fail_ttl_s) if cfg.cache_fail_ttl_s > 0 else int(PRICE_FAIL_TTL_S)

def _join_notes(*xs: str) -> str:
    """Join multiple note strings with "; ", skipping empties."""
    return "; ".join(x.strip() for x in xs if x and str(x).strip())

def _effective_verify_cap(cfg: RunConfig) -> int:
    """
    Compute the effective cap on verify attempts for watchlist building.
    - If cfg.verify_limit > 0, use that.
    - Always enforce cfg.verify_safety_cap if it is >0.
    Returns 0 if unlimited.
    """
    cap = None
    if cfg.verify_limit and cfg.verify_limit > 0:
        cap = cfg.verify_limit
    if cfg.verify_safety_cap and cfg.verify_safety_cap > 0:
        cap = min(cap, cfg.verify_safety_cap) if cap is not None else cfg.verify_safety_cap
    return int(cap) if cap is not None else 0  # 0 => unlimited

def _recent_key(namespace: str, key: str) -> str:
    """Create a namespaced key for tracking recent items (to avoid repeats)."""
    return f"{namespace}:{key}"

# ============================================================
# Cache wrappers (consistent keying for prices)
# ============================================================

def get_buy(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
    *,
    fan: Optional[FanaticalPWClient] = None,
) -> Tuple[Optional[str], Optional[float], str]:
    """
    Get the current buy price (in GBP) and title for a given Fanatical product URL, using the price cache.
    If a `fan` client is provided, reuse its open browser session for faster retrieval; otherwise open a new 
    short-lived browser client for this request.
    
    Returns:
        (title, buy_price_gbp, notes)
        - title (str or None): The product title if available.
        - buy_price_gbp (float or None): Current price in GBP, or None if price not found.
        - notes (str): Details about how the price was obtained (e.g., "cache: ..." or error info).
    """
    key = _buy_key(url)
    c = cache.get(key)
    if c:
        if c.ok and c.value is not None:
            return None, float(c.value), f"cache: {c.notes}"
        # Cached failure
        return None, None, f"cache-fail: {c.notes}"

    # Price not cached or expired: fetch from source
    if fan is not None:
        title, price, notes = fan.read_title_and_price_gbp(url)
    else:
        # Use a fresh browser client for this fetch
        with FanaticalPWClient(headless=True) as tmp:
            title, price, notes = tmp.read_title_and_price_gbp(url)

    if price is None:
        # Price lookup failed, cache failure for a shorter duration
        cache.set(key, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return title, None, notes

    # Price fetched successfully, cache it
    cache.set(key, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return title, float(price), notes

def get_sell(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
) -> Tuple[Optional[float], str]:
    """
    Get the current sell price (in GBP) for a given marketplace product URL (e.g., Eneba), using the price cache.
    
    Returns:
        (sell_price_gbp, notes)
        - sell_price_gbp (float or None): Current sell price in GBP, or None if not found.
        - notes (str): Details about how the price was obtained or why it failed.
    """
    key = (url or "").strip()
    c = cache.get(key)
    if c:
        if c.ok and c.value is not None:
            return float(c.value), f"cache: {c.notes}"
        # Cached failure
        return None, f"cache-fail: {c.notes}"

    price, notes = read_price_gbp(url)
    if price is None:
        # Price lookup failed, cache failure
        cache.set(key, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return None, notes

    # Price fetched successfully, cache it
    cache.set(key, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return float(price), notes

# ============================================================
# BUILD — Fanatical (uses Playwright for dynamic content)
# ============================================================

def build_watchlist(cfg: RunConfig, cache: PriceCache, out_csv: Path) -> pd.DataFrame:
    """
    Harvest game deal links from Fanatical and build a watchlist of candidate items.
    Filters out items that are too expensive or recently processed, until reaching the target count.
    Writes the resulting watchlist to `out_csv` and returns the DataFrame.
    """
    start = time.time()
    links: List[str] = []

    # Use one browser session to gather all links for efficiency
    with FanaticalPWClient(headless=True) as fan:
        for src_url in FANATICAL_SOURCES.values():
            if not _budget_ok(start, cfg.run_budget_s):
                break
            # Harvest links from each source page
            new_links = fan.harvest_game_links(
                src_url,
                pages=cfg.pages_per_source,
                max_links=800,
                sleep_range_s=(0.25, 0.75),
            )
            links.extend(new_links)

        # Deduplicate and shuffle the collected links
        links = _dedupe(links)
        random.shuffle(links)

        # Limit number of candidates to verify, if specified
        pool = links[: cfg.verify_candidates] if cfg.verify_candidates > 0 else links

        rows: List[Dict[str, object]] = []
        kept = 0
        attempted = 0
        verify_cap = _effective_verify_cap(cfg)  # 0 => unlimited

        for url in pool:
            if not _budget_ok(start, cfg.run_budget_s):
                break
            if kept >= cfg.watchlist_target:
                break
            if verify_cap > 0 and attempted >= verify_cap:
                break

            key = _buy_key(url)
            # Skip if this item was recently built into a watchlist (avoid duplicates across runs)
            if cfg.avoid_recent_days > 0 and cache.is_recent(_recent_key("build", key), cfg.avoid_recent_days):
                continue

            attempted += 1
            t0 = time.time()

            title, buy_price, notes = get_buy(cfg, cache, url, fan=fan)

            # Enforce per-item time budget
            if cfg.item_budget_s > 0 and (time.time() - t0) > cfg.item_budget_s:
                continue
            # Skip if price is missing or above max buy threshold
            if buy_price is None or buy_price > cfg.max_buy_gbp:
                continue

            rows.append({
                "title": title or url.rsplit("/", 1)[-1],
                "buy_url": url,
                "buy_price_gbp": float(buy_price),
                "buy_notes": notes,
            })
            kept += 1
            # Mark item as recently built to avoid immediate reuse in future runs
            cache.mark_recent(_recent_key("build", key))

    # Save watchlist to CSV and return DataFrame
    df = pd.DataFrame(rows, columns=WATCHLIST_COLS)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)
    log.info(
        "Built watchlist: harvested %d links, attempted %d, kept %d items. Output saved to %s",
        len(links), attempted, kept, out_csv
    )
    return df

# ============================================================
# SCAN — Eneba (checks marketplace prices for ROI)
# ============================================================

def scan_watchlist(
    cfg: RunConfig,
    cache: PriceCache,
    watchlist_csv: Path,
    scans_csv: Path,
    passes_csv: Path,
) -> pd.DataFrame:
    """
    Scan the current watchlist for potential profitable flips.
    For each item in the watchlist, re-checks the current buy price and finds the current market sell price.
    Calculates net profit and ROI, and determines if the item meets the minimum profit criteria.
    Appends the results to `scans_csv` (scan history) and writes passing items to `passes_csv`.
    Returns the DataFrame of the current scan batch.
    """
    # Load watchlist from CSV
    if not watchlist_csv.exists():
        log.error("Watchlist file not found: %s", watchlist_csv)
        return pd.DataFrame(columns=SCANS_COLS)
    try:
        watch = pd.read_csv(watchlist_csv).fillna("")
    except Exception as e:
        log.error("Failed to read watchlist CSV: %s", e)
        return pd.DataFrame(columns=SCANS_COLS)
    if watch.empty:
        log.warning("Watchlist is empty. Skipping scan.")
        return pd.DataFrame(columns=SCANS_COLS)
    # Ensure required columns exist
    for col in ("title", "buy_url"):
        if col not in watch.columns:
            log.error("Watchlist missing required column '%s'. Skipping scan.", col)
            return pd.DataFrame(columns=SCANS_COLS)

    # Prioritize items not scanned recently (rotate order)
    def _was_scanned_recently(buy_url: str) -> bool:
        key = _buy_key(buy_url)
        if cfg.avoid_recent_days <= 0:
            return False
        return cache.is_recent(_recent_key("scan", key), cfg.avoid_recent_days)

    watch["__recent_scan"] = watch["buy_url"].astype(str).apply(_was_scanned_recently)
    fresh = watch[watch["__recent_scan"] == False].sample(frac=1.0, random_state=None)
    stale = watch[watch["__recent_scan"] == True].sample(frac=1.0, random_state=None)
    watch = pd.concat([fresh, stale], ignore_index=True).drop(columns=["__recent_scan"])

    rows: List[Dict[str, object]] = []
    start = time.time()
    batch_id = uuid.uuid4().hex[:12]

    # Use one browser session for all buy price checks
    with FanaticalPWClient(headless=True) as fan:
        for _, row in watch.iterrows():
            if not _budget_ok(start, cfg.run_budget_s):
                break
            if len(rows) >= cfg.scan_limit:
                break

            title = str(row.get("title", ""))
            buy_url = str(row.get("buy_url", "")).strip()
            if not buy_url:
                continue

            t0 = time.time()
            # Re-check buy price on Fanatical (likely cached from build step)
            _, buy_price, buy_notes = get_buy(cfg, cache, buy_url, fan=fan)

            # Find the best matching product on Eneba for this title
            store_url = make_store_search_url(title)
            prod_url, resolve_notes = resolve_product_url_from_store(store_url, title)

            sell_price, sell_notes = None, ""
            if prod_url:
                sell_price, sell_notes = get_sell(cfg, cache, prod_url)

            market_after_fee = buffer = profit = roi = None
            passes = False

            if buy_price is not None and sell_price is not None:
                # Calculate net sell after marketplace fee, buffer, profit, and ROI
                market_after_fee = sell_price * (1 - SELL_FEE_PCT)
                buffer = BUFFER_FIXED_GBP + BUFFER_PCT_OF_BUY * buy_price
                profit = market_after_fee - buy_price - buffer
                roi = profit / buy_price if buy_price > 0 else None
                passes = (profit is not None and roi is not None 
                          and profit >= MIN_PROFIT_GBP and roi >= MIN_ROI)

            rows.append({
                "batch_id": batch_id,
                "timestamp": _now(),
                "title": title,
                "buy_price": buy_price,
                "market_price": sell_price,
                "market_after_fee": market_after_fee,
                "buffer": buffer,
                "edge": profit,
                "edge_pct": roi,
                "passes": bool(passes),
                "buy_url": buy_url,
                "market_url": prod_url or store_url,
                "buy_notes": buy_notes,
                "market_notes": _join_notes(resolve_notes, sell_notes),
                "elapsed_s": round(time.time() - t0, 2),
            })

            # Mark as scanned to deprioritize this item in the next scan
            cache.mark_recent(_recent_key("scan", _buy_key(buy_url)))

    # Save scan results to CSVs
    batch = pd.DataFrame(rows, columns=SCANS_COLS)
    scans_csv.parent.mkdir(parents=True, exist_ok=True)
    passes_csv.parent.mkdir(parents=True, exist_ok=True)
    # Append new batch to scans history (if file exists)
    try:
        prev = pd.read_csv(scans_csv)
        pd.concat([prev, batch], ignore_index=True).to_csv(scans_csv, index=False)
    except Exception:
        batch.to_csv(scans_csv, index=False)
    # Write passing items to separate CSV
    batch[batch["passes"] == True].to_csv(passes_csv, index=False)

    # Log summary of this scan run
    passes_count = int(batch["passes"].sum()) if not batch.empty else 0
    log.info(
        "Scan complete: %d items scanned, %d items passed criteria. Results saved to %s (passes to %s)",
        len(rows), passes_count, scans_csv, passes_csv
    )
    return batch
