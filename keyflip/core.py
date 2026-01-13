from __future__ import annotations

import logging
import random
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .cache import PriceCache
# IMPORTANT: use the COMPAT RunConfig from config.py (prevents TypeError in app.py)
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
log.info("Fanatical backend: Playwright (reused client)")

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
# Small helpers
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
    return (url.split("?")[0].rstrip("/").lower()) if url else ""


def _budget_ok(start: float, limit_s: float) -> bool:
    return limit_s <= 0 or (time.time() - start) <= limit_s


def _ttl_fail(cfg: RunConfig) -> int:
    # optional override
    ttl = getattr(cfg, "cache_fail_ttl", None)
    if ttl is None:
        ttl = getattr(cfg, "cache_fail_ttl_s", None)
    if ttl is not None:
        try:
            ttl_i = int(ttl)
            if ttl_i > 0:
                return ttl_i
        except Exception:
            pass
    return int(PRICE_FAIL_TTL_S)


def _join_notes(*xs: str) -> str:
    return "; ".join(x.strip() for x in xs if x and str(x).strip())


def _effective_verify_cap(cfg: RunConfig) -> int:
    """
    Effective cap on verify attempts:
    - prefer cfg.effective_verify_limit() if present
    - else compute using verify_limit + safety cap fields
    """
    if hasattr(cfg, "effective_verify_limit"):
        try:
            return int(cfg.effective_verify_limit())
        except Exception:
            pass

    verify_limit = int(getattr(cfg, "verify_limit", 0) or 0)
    safety_cap = int(getattr(cfg, "verify_safety_cap", getattr(cfg, "safety_cap", 0)) or 0)

    if verify_limit <= 0:
        # unlimited requested => still apply safety cap if present
        return safety_cap if safety_cap > 0 else 0

    if safety_cap > 0:
        return min(verify_limit, safety_cap)
    return verify_limit


def _recent_key(namespace: str, key: str) -> str:
    return f"{namespace}:{key}"


def _read_csv_safe(path: Path) -> pd.DataFrame:
    try:
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_csv(path)
        return df
    except Exception:
        return pd.DataFrame()


def _append_csv(path: Path, batch: pd.DataFrame, cols: List[str]) -> None:
    """
    Append batch to CSV. If existing CSV has different columns, we re-save as union.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = _read_csv_safe(path)

    if prev.empty:
        batch[cols].to_csv(path, index=False)
        return

    # union columns to avoid "No columns to parse" / mismatch issues
    union_cols = list(dict.fromkeys(list(prev.columns) + cols))
    prev2 = prev.reindex(columns=union_cols)
    batch2 = batch.reindex(columns=union_cols)

    pd.concat([prev2, batch2], ignore_index=True).to_csv(path, index=False)


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
    Get Fanatical buy price in GBP (cached). Returns (title, price, notes).
    """
    key = _buy_key(url)
    c = cache.get(key)
    if c:
        if c.ok and c.value is not None:
            return None, float(c.value), f"cache: {c.notes}"
        return None, None, f"cache-fail: {c.notes}"

    if fan is not None:
        title, price, notes = fan.read_title_and_price_gbp(url)
    else:
        with FanaticalPWClient(headless=True) as tmp:
            title, price, notes = tmp.read_title_and_price_gbp(url)

    if price is None:
        cache.set(key, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return title, None, notes

    cache.set(key, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return title, float(price), notes


def get_sell(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
) -> Tuple[Optional[float], str]:
    """
    Get marketplace sell price in GBP (cached). Returns (price, notes).
    """
    key = (url or "").strip()
    c = cache.get(key)
    if c:
        if c.ok and c.value is not None:
            return float(c.value), f"cache: {c.notes}"
        return None, f"cache-fail: {c.notes}"

    price, notes = read_price_gbp(url)
    if price is None:
        cache.set(key, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return None, notes

    cache.set(key, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return float(price), notes


# ============================================================
# BUILD — Fanatical (Playwright)
# ============================================================

def build_watchlist(cfg: RunConfig, cache: PriceCache, out_csv: Path) -> pd.DataFrame:
    start = time.time()
    links: List[str] = []

    pages = int(getattr(cfg, "pages_per_source", 2) or 2)
    verify_candidates = int(getattr(cfg, "verify_candidates", 200) or 200)

    # compatible field names
    max_buy = float(getattr(cfg, "max_buy", getattr(cfg, "max_buy_gbp", 0.0)))
    target = int(getattr(cfg, "target", getattr(cfg, "watchlist_target", 10)))
    avoid_days = int(getattr(cfg, "avoid_recent_days", 0) or 0)

    verify_cap = _effective_verify_cap(cfg)  # 0 => unlimited

    rows: List[Dict[str, object]] = []
    kept = 0
    attempted = 0

    with FanaticalPWClient(headless=True) as fan:
        for src_url in FANATICAL_SOURCES.values():
            if not _budget_ok(start, float(getattr(cfg, "run_budget", getattr(cfg, "run_budget_s", 0.0)) or 0.0)):
                break

            new_links = fan.harvest_game_links(
                src_url,
                pages=pages,
                max_links=800,
                sleep_range_s=(0.25, 0.75),
            )
            links.extend(new_links)

        links = _dedupe(links)
        random.shuffle(links)

        pool = links[:verify_candidates] if verify_candidates > 0 else links

        for url in pool:
            if kept >= target:
                break
            if verify_cap > 0 and attempted >= verify_cap:
                break
            if not _budget_ok(start, float(getattr(cfg, "run_budget", getattr(cfg, "run_budget_s", 0.0)) or 0.0)):
                break

            key = _buy_key(url)
            if avoid_days > 0 and cache.is_recent(_recent_key("build", key), avoid_days):
                continue

            attempted += 1
            t0 = time.time()

            title, buy_price, notes = get_buy(cfg, cache, url, fan=fan)

            item_budget = float(getattr(cfg, "item_budget", getattr(cfg, "item_budget_s", 0.0)) or 0.0)
            if item_budget > 0 and (time.time() - t0) > item_budget:
                continue

            if buy_price is None or buy_price > max_buy:
                continue

            rows.append(
                {
                    "title": title or url.rsplit("/", 1)[-1],
                    "buy_url": url,
                    "buy_price_gbp": float(buy_price),
                    "buy_notes": notes,
                }
            )
            kept += 1
            cache.mark_recent(_recent_key("build", key))

    df = pd.DataFrame(rows, columns=WATCHLIST_COLS)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)

    log.info(
        "Built watchlist: harvested=%d attempted=%d kept=%d target=%d out=%s",
        len(links), attempted, kept, target, out_csv
    )
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
        log.error("Watchlist file not found: %s", watchlist_csv)
        return pd.DataFrame(columns=SCANS_COLS)

    watch = _read_csv_safe(watchlist_csv).fillna("")
    if watch.empty:
        log.warning("Watchlist is empty. Skipping scan.")
        return pd.DataFrame(columns=SCANS_COLS)

    for col in ("title", "buy_url"):
        if col not in watch.columns:
            log.error("Watchlist missing required column '%s'.", col)
            return pd.DataFrame(columns=SCANS_COLS)

    # compatible field names
    avoid_days = int(getattr(cfg, "avoid_recent_days", 0) or 0)
    scan_limit = int(getattr(cfg, "scan_limit", 0) or 0)  # 0 = unlimited
    run_budget = float(getattr(cfg, "run_budget", getattr(cfg, "run_budget_s", 0.0)) or 0.0)

    start = time.time()
    batch_id = uuid.uuid4().hex[:12]

    def was_scanned_recently(buy_url: str) -> bool:
        if avoid_days <= 0:
            return False
        return cache.is_recent(_recent_key("scan", _buy_key(buy_url)), avoid_days)

    # Rotate order: prefer not-recently-scanned items first
    watch["__recent_scan"] = watch["buy_url"].astype(str).apply(was_scanned_recently)
    fresh = watch[watch["__recent_scan"] == False].sample(frac=1.0, random_state=None)
    stale = watch[watch["__recent_scan"] == True].sample(frac=1.0, random_state=None)
    watch = pd.concat([fresh, stale], ignore_index=True).drop(columns=["__recent_scan"])

    rows: List[Dict[str, object]] = []

    with FanaticalPWClient(headless=True) as fan:
        for _, r in watch.iterrows():
            if not _budget_ok(start, run_budget):
                break
            if scan_limit > 0 and len(rows) >= scan_limit:
                break

            title = str(r.get("title", "")).strip()
            buy_url = str(r.get("buy_url", "")).strip()
            if not buy_url:
                continue

            t0 = time.time()

            # Re-check buy price
            _, buy_price, buy_notes = get_buy(cfg, cache, buy_url, fan=fan)

            # Resolve product on Eneba
            store_url = make_store_search_url(title)
            prod_url, resolve_notes = resolve_product_url_from_store(store_url, title)

            sell_price: Optional[float] = None
            sell_notes = ""
            if prod_url:
                sell_price, sell_notes = get_sell(cfg, cache, prod_url)

            market_after_fee = buffer = profit = roi = None
            passes = False

            if buy_price is not None and sell_price is not None:
                market_after_fee = sell_price * (1 - SELL_FEE_PCT)
                buffer = BUFFER_FIXED_GBP + BUFFER_PCT_OF_BUY * buy_price
                profit = market_after_fee - buy_price - buffer
                roi = profit / buy_price if buy_price > 0 else None
                passes = (
                    profit is not None
                    and roi is not None
                    and profit >= MIN_PROFIT_GBP
                    and roi >= MIN_ROI
                )

            rows.append(
                {
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
                }
            )

            # mark as scanned to rotate next time
            cache.mark_recent(_recent_key("scan", _buy_key(buy_url)))

    batch = pd.DataFrame(rows, columns=SCANS_COLS)

    # Write scans history and passes
    _append_csv(scans_csv, batch, SCANS_COLS)
    passes_csv.parent.mkdir(parents=True, exist_ok=True)
    batch[batch["passes"] == True].to_csv(passes_csv, index=False)

    passes_count = int(batch["passes"].sum()) if not batch.empty else 0
    log.info(
        "Scan complete: scanned=%d passes=%d scans=%s passes=%s",
        len(batch), passes_count, scans_csv, passes_csv
    )
    return batch
