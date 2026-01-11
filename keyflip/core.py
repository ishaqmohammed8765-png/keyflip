from __future__ import annotations

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


# ---------- time / notes helpers ----------

def _now_iso_ms() -> str:
    t = time.time()
    lt = time.localtime(t)
    ms = int((t - int(t)) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", lt) + f".{ms:03d}"


def _dedupe(seq: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _buy_key(url: str) -> str:
    """Stable key for 'recently scanned' tracking (separate from price-cache key)."""
    return url.split("?")[0].rstrip("/").lower()


def _join_notes(*parts: str) -> str:
    clean = [p.strip() for p in parts if p and str(p).strip()]
    return "; ".join(clean)


def _budget_ok(start: float, budget_s: float) -> bool:
    return (budget_s <= 0) or ((time.time() - start) <= budget_s)


def _ttl_fail(cfg: RunConfig) -> int:
    return int(cfg.cache_fail_ttl_s) if int(cfg.cache_fail_ttl_s) > 0 else int(PRICE_FAIL_TTL_S)


# ---------- cache wrappers (no double fetch) ----------

def get_cached_or_fetch_buy_full(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
) -> Tuple[Optional[str], Optional[float], str]:
    """
    Returns (title, price_gbp, notes). Title may be None when served from cache.
    """
    c = cache.get(url)
    if c is not None:
        if c.ok and c.value is not None:
            return None, float(c.value), f"cache: {c.notes}"
        if not c.ok:
            return None, None, f"cache-fail: {c.notes}"

    title, price, notes = read_title_and_price_gbp(url)
    if price is None:
        cache.set(url, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return title, None, notes

    cache.set(url, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return title, float(price), notes


def get_cached_or_fetch_sell_full(
    cfg: RunConfig,
    cache: PriceCache,
    product_url: str,
) -> Tuple[Optional[float], str]:
    """
    Returns (price_gbp, notes).
    """
    c = cache.get(product_url)
    if c is not None:
        if c.ok and c.value is not None:
            return float(c.value), f"cache: {c.notes}"
        if not c.ok:
            return None, f"cache-fail: {c.notes}"

    price, notes = read_price_gbp(product_url)
    if price is None:
        cache.set(product_url, None, "GBP", ttl_s=_ttl_fail(cfg), ok=False, notes=notes)
        return None, notes

    cache.set(product_url, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return float(price), notes


# ---------- build ----------

def build_watchlist(cfg: RunConfig, cache: PriceCache, out_watchlist: Path) -> pd.DataFrame:
    """
    Harvest Fanatical links -> verify buy <= max_buy -> write watchlist.csv.
    Always writes headers even if 0 rows.
    """
    run_start = time.time()

    all_links: List[str] = []
    for _, src_url in FANATICAL_SOURCES.items():
        if not _budget_ok(run_start, cfg.run_budget_s):
            break
        all_links.extend(harvest_game_links(src_url, pages=cfg.pages_per_source))

    all_links = _dedupe(all_links)
    random.shuffle(all_links)

    pool = all_links[: cfg.verify_candidates] if cfg.verify_candidates > 0 else all_links

    verified_rows: List[Dict[str, str]] = []
    attempted = 0
    kept = 0

    hard_cap = cfg.verify_safety_cap if cfg.verify_safety_cap > 0 else 999_999
    verify_limit = cfg.verify_limit if cfg.verify_limit > 0 else 999_999
    target = cfg.watchlist_target if cfg.watchlist_target > 0 else 999_999

    for url in pool:
        if not _budget_ok(run_start, cfg.run_budget_s):
            break
        if attempted >= verify_limit or attempted >= hard_cap or kept >= target:
            break

        recent_key = _buy_key(url)
        if cache.is_recent(recent_key, cfg.avoid_recent_days):
            # Recency skips do NOT consume verify budget
            continue

        attempted += 1
        item_start = time.time()

        title, buy_price, buy_notes = get_cached_or_fetch_buy_full(cfg, cache, url)

        # Best-effort item budget (does not cancel in-flight network calls)
        if cfg.item_budget_s > 0 and (time.time() - item_start) > cfg.item_budget_s:
            continue

        if buy_price is None:
            continue
        if buy_price > cfg.max_buy_gbp:
            continue

        clean_title = (title or "").strip() or url.rsplit("/", 1)[-1]

        verified_rows.append(
            {
                "title": clean_title,
                "buy_url": url,
                "buy_price_gbp": f"{buy_price:.2f}",
                "buy_notes": buy_notes,
            }
        )
        kept += 1
        cache.mark_recent(recent_key)

    df = pd.DataFrame(verified_rows, columns=WATCHLIST_COLS)

    # Always write headers
    out_watchlist.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_watchlist, index=False)

    return df


# ---------- read/ensure files ----------

def _read_watchlist_safe(watchlist_csv: Path) -> pd.DataFrame:
    """
    Reads watchlist.csv safely (handles totally empty/corrupt file).
    Always returns a DF with expected columns.
    """
    if not watchlist_csv.exists():
        return pd.DataFrame(columns=WATCHLIST_COLS)

    try:
        df = pd.read_csv(watchlist_csv).fillna("")
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
    empty = pd.DataFrame(columns=SCANS_COLS)

    scans_csv.parent.mkdir(parents=True, exist_ok=True)
    passes_csv.parent.mkdir(parents=True, exist_ok=True)

    if not scans_csv.exists():
        empty.to_csv(scans_csv, index=False)
    else:
        try:
            pd.read_csv(scans_csv)
        except Exception:
            empty.to_csv(scans_csv, index=False)

    if not passes_csv.exists():
        empty.to_csv(passes_csv, index=False)
    else:
        try:
            pd.read_csv(passes_csv)
        except Exception:
            empty.to_csv(passes_csv, index=False)


# ---------- scan ----------

def scan_watchlist(
    cfg: RunConfig,
    cache: PriceCache,
    watchlist_csv: Path,
    scans_csv: Path,
    passes_csv: Path,
) -> pd.DataFrame:
    """
    Read watchlist.csv -> resolve Eneba -> compute edge
    -> append scans.csv and write passes.csv (latest batch).

    Gracefully handles empty watchlist (no crash).
    """
    w = _read_watchlist_safe(watchlist_csv)
    _ensure_scans_files(scans_csv, passes_csv)

    if w.empty:
        return pd.DataFrame(columns=SCANS_COLS)

    # Shuffle scan order so “play” feels less repetitive
    w = w.sample(frac=1.0, random_state=None).reset_index(drop=True)

    rows_out: List[Dict[str, object]] = []
    run_start = time.time()
    scanned = 0
    limit = cfg.scan_limit if cfg.scan_limit > 0 else 999_999
    batch_id = uuid.uuid4().hex[:12]

    for _, row in w.iterrows():
        if not _budget_ok(run_start, cfg.run_budget_s):
            break
        if scanned >= limit:
            break

        title = str(row.get("title", "")).strip() or "Unknown"
        buy_url = str(row.get("buy_url", "")).strip()
        if not buy_url:
            continue

        scanned += 1
        item_start = time.time()

        # BUY (Fanatical)
        _, buy_price, buy_notes = get_cached_or_fetch_buy_full(cfg, cache, buy_url)

        if cfg.item_budget_s > 0 and (time.time() - item_start) > cfg.item_budget_s:
            continue

        store_url = make_store_search_url(title)

        # If buy is missing, skip resolving Eneba (wastes time)
        if buy_price is None:
            rows_out.append(
                {
                    "batch_id": batch_id,
                    "timestamp": _now_iso_ms(),
                    "title": title,
                    "buy_price": None,
                    "market_price": None,
                    "market_after_fee": None,
                    "buffer": None,
                    "edge": None,
                    "edge_pct": None,
                    "passes": False,
                    "buy_url": buy_url,
                    "market_url": store_url,
                    "buy_notes": buy_notes,
                    "market_notes": "skipped: buy price unavailable",
                    "elapsed_s": round(time.time() - item_start, 2),
                }
            )
            continue

        # RESOLVE (Eneba)  ✅ FIX: pass title as required by resolver
        prod_url, resolve_notes = resolve_product_url_from_store(store_url, title)

        if cfg.item_budget_s > 0 and (time.time() - item_start) > cfg.item_budget_s:
            rows_out.append(
                {
                    "batch_id": batch_id,
                    "timestamp": _now_iso_ms(),
                    "title": title,
                    "buy_price": buy_price,
                    "market_price": None,
                    "market_after_fee": None,
                    "buffer": None,
                    "edge": None,
                    "edge_pct": None,
                    "passes": False,
                    "buy_url": buy_url,
                    "market_url": prod_url or store_url,
                    "buy_notes": buy_notes,
                    "market_notes": _join_notes(resolve_notes, "skipped: item budget exceeded"),
                    "elapsed_s": round(time.time() - item_start, 2),
                }
            )
            continue

        # SELL (Eneba price)
        sell_price: Optional[float] = None
        sell_notes = ""
        if prod_url:
            sell_price, sell_notes = get_cached_or_fetch_sell_full(cfg, cache, prod_url)
        else:
            sell_notes = resolve_notes

        # Compute edge
        market_after_fee = None
        buffer = None
        profit = None
        roi = None
        passes = False

        if sell_price is not None:
            market_after_fee = sell_price * (1.0 - SELL_FEE_PCT)
            buffer = BUFFER_FIXED_GBP + (BUFFER_PCT_OF_BUY * buy_price)
            profit = market_after_fee - buy_price - buffer
            roi = (profit / buy_price) if buy_price > 0 else None
            if profit is not None and roi is not None:
                passes = (profit >= MIN_PROFIT_GBP) and (roi >= MIN_ROI)

        rows_out.append(
            {
                "batch_id": batch_id,
                "timestamp": _now_iso_ms(),
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
                "market_notes": _join_notes(resolve_notes, sell_notes),
                "elapsed_s": round(time.time() - item_start, 2),
            }
        )

    batch = pd.DataFrame(rows_out, columns=SCANS_COLS)

    # Append to scans.csv safely
    try:
        old = pd.read_csv(scans_csv).fillna("")
        df_all = pd.concat([old, batch], ignore_index=True)
    except Exception:
        df_all = batch

    df_all.to_csv(scans_csv, index=False)

    # passes.csv = only this run's passes
    if not batch.empty:
        df_pass = batch[batch["passes"] == True].copy()
    else:
        df_pass = pd.DataFrame(columns=SCANS_COLS)

    df_pass.to_csv(passes_csv, index=False)
    return batch
