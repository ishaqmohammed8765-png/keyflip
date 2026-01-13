# =========================
# keyflip/core.py (rewrite)
# - Persists eneba_url/eneba_notes to watchlist.csv
# - Robust NaN handling (never tries to fetch "nan")
# - Passes allow_eur + eur_to_gbp into read_price_gbp()
# - Optional: avoid store+product burst by skipping price fetch on newly resolved URLs
# =========================
from __future__ import annotations

import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from .cache import PriceCache
from .config import (
    RunConfig,
    BUFFER_FIXED_GBP,
    BUFFER_PCT_OF_BUY,
    FANATICAL_SOURCES,
    MIN_PROFIT_GBP,
    MIN_ROI,
    SELL_FEE_PCT,
)
from .eneba import make_store_search_url, read_price_gbp, resolve_product_url_from_store
from .fanatical_pw import FanaticalPWClient

log = logging.getLogger("keyflip.core")

WATCHLIST_COLS = ["title", "buy_url", "buy_price_gbp", "buy_notes", "eneba_url", "eneba_notes"]

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


def _to_str(x: Any) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    return "" if s.lower() == "nan" else s


def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        try:
            if pd.isna(x):
                return None
        except Exception:
            pass
        s = str(x).strip()
        if not s or s.lower() == "nan":
            return None
        return float(s)
    except Exception:
        return None


def _dedupe(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


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
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return PriceCache(db_path)


def _compute_profit(buy_gbp: float, sell_gbp: float) -> Tuple[float, float, float, float]:
    market_after_fee = sell_gbp * (1.0 - SELL_FEE_PCT)
    buffer = BUFFER_FIXED_GBP + (BUFFER_PCT_OF_BUY * buy_gbp)
    profit = market_after_fee - buy_gbp - buffer
    roi = profit / buy_gbp if buy_gbp > 0 else -1.0
    return market_after_fee, buffer, profit, roi


def _cfg_int(cfg: RunConfig, name: str, default: int) -> int:
    try:
        return int(getattr(cfg, name, default))
    except Exception:
        return int(default)


def _cfg_float(cfg: RunConfig, name: str, default: float) -> float:
    try:
        return float(getattr(cfg, name, default))
    except Exception:
        return float(default)


def _cfg_bool(cfg: RunConfig, name: str, default: bool) -> bool:
    try:
        return bool(getattr(cfg, name, default))
    except Exception:
        return bool(default)


# ============================================================
# Watchlist builder
# ============================================================


def build_watchlist(cfg: RunConfig, out_csv: Path) -> int:
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    target = _cfg_int(cfg, "watchlist_target", 10)
    pages = _cfg_int(cfg, "pages_per_source", 2)
    max_links = _cfg_int(cfg, "max_links_per_source", 200)

    if target <= 0:
        log.warning("watchlist_target <= 0, nothing to build.")
        pd.DataFrame([], columns=WATCHLIST_COLS).to_csv(out_csv, index=False)
        return 0

    links: List[str] = []
    rows: List[Dict[str, object]] = []

    log.info("Building watchlist: target=%d pages_per_source=%d", target, pages)

    with FanaticalPWClient(headless=True) as fan:
        for name, src in FANATICAL_SOURCES.items():
            try:
                harvested = fan.harvest_game_links(src, pages=pages, max_links=max_links)
                log.info("Harvested %d links from %s", len(harvested), name)
                links.extend(harvested)
            except Exception as e:
                log.warning("Harvest failed (%s): %s", name, e)

        links = _dedupe(links)
        random.shuffle(links)

        for url in links:
            if len(rows) >= target:
                break

            try:
                title, price, notes = fan.read_title_and_price_gbp(url)
            except Exception:
                log.exception("Fanatical read failed: %s", url)
                continue

            if not title or price is None:
                continue

            rows.append(
                {
                    "title": str(title).strip(),
                    "buy_url": str(url).strip(),
                    "buy_price_gbp": float(price),
                    "buy_notes": str(notes or "").strip(),
                    "eneba_url": "",
                    "eneba_notes": "",
                }
            )

    df = pd.DataFrame(rows, columns=WATCHLIST_COLS)
    df.to_csv(out_csv, index=False)
    log.info("Wrote %d rows to %s", len(df), str(out_csv))
    return int(len(df))


# ============================================================
# Scan watchlist
# ============================================================


def scan_watchlist(
    cfg: RunConfig,
    watchlist_path: Path,
    scans_path: Path,
    passes_path: Path,
    db_path: Path,
    fail_ttl: int,
) -> pd.DataFrame:
    _ = _open_cache(db_path)  # kept
    watchlist_path = Path(watchlist_path)
    scans_path = Path(scans_path)
    passes_path = Path(passes_path)

    watch = _read_csv_safe(watchlist_path)
    if watch.empty:
        log.warning("Watchlist empty: %s", str(watchlist_path))
        return pd.DataFrame(columns=SCANS_COLS)

    for c in WATCHLIST_COLS:
        if c not in watch.columns:
            watch[c] = "" if c in ("eneba_url", "eneba_notes") else None

    scan_limit = _cfg_int(cfg, "scan_limit", 0)
    refresh_buy_price = _cfg_bool(cfg, "refresh_buy_price", False)
    per_item_sleep_s = _cfg_float(cfg, "scan_sleep_s", 0.0)

    # currency conversion settings (from cfg)
    allow_eur = _cfg_bool(cfg, "allow_eur", False)
    eur_to_gbp = _cfg_float(cfg, "eur_to_gbp", 0.86)

    # Optional burst control (default True): skip product fetch on newly resolved URL this run
    skip_price_on_new_resolve = _cfg_bool(cfg, "skip_price_on_new_resolve", True)

    batch_id = uuid.uuid4().hex[:12]
    start = time.time()

    attempted = 0
    rows: List[Dict[str, object]] = []
    watchlist_dirty = False

    log.info(
        "Scanning watchlist: size=%d scan_limit=%d refresh_buy_price=%s allow_eur=%s",
        int(len(watch)),
        int(scan_limit),
        bool(refresh_buy_price),
        bool(allow_eur),
    )

    fan: Optional[FanaticalPWClient] = None

    def _fan_start() -> Optional[FanaticalPWClient]:
        try:
            f = FanaticalPWClient(headless=True)
            f.__enter__()
            return f
        except Exception:
            log.exception("Playwright failed to start; continuing without buy refresh.")
            return None

    def _fan_stop(f: Optional[FanaticalPWClient]) -> None:
        if f is None:
            return
        try:
            f.__exit__(None, None, None)
        except Exception:
            pass

    if refresh_buy_price:
        fan = _fan_start()

    try:
        for idx, r in watch.iterrows():
            if scan_limit > 0 and attempted >= scan_limit:
                break
            attempted += 1

            title = _to_str(r.get("title", ""))
            buy_url = _to_str(r.get("buy_url", ""))

            buy_price = _to_float(r.get("buy_price_gbp", None))
            buy_notes = _to_str(r.get("buy_notes", ""))

            if refresh_buy_price and fan is not None and buy_url:
                try:
                    _t, p, n = fan.read_title_and_price_gbp(buy_url)
                    if p is not None:
                        buy_price = float(p)
                    if n:
                        buy_notes = str(n).strip()
                except Exception as e:
                    buy_notes = (buy_notes + " | " if buy_notes else "") + f"buy_refresh_failed:{type(e).__name__}"
                    _fan_stop(fan)
                    fan = _fan_start()

            existing_eneba_url = _to_str(r.get("eneba_url", ""))
            existing_eneba_notes = _to_str(r.get("eneba_notes", ""))

            store_url = make_store_search_url(title)
            prod_url: Optional[str] = existing_eneba_url or None
            resolve_notes = existing_eneba_notes or ""
            sell_price: Optional[float] = None
            sell_notes = ""

            newly_resolved = False

            if not prod_url:
                try:
                    prod_url, resolve_notes = resolve_product_url_from_store(store_url, title)
                except Exception as e:
                    resolve_notes = f"resolve_failed:{type(e).__name__}"
                    prod_url = None

                if prod_url:
                    watch.at[idx, "eneba_url"] = prod_url
                    watch.at[idx, "eneba_notes"] = resolve_notes
                    watchlist_dirty = True
                    newly_resolved = True

            if prod_url:
                if newly_resolved and skip_price_on_new_resolve:
                    sell_price, sell_notes = None, "skipped (newly resolved; price next scan)"
                else:
                    sell_price, sell_notes = read_price_gbp(
                        prod_url,
                        allow_eur=allow_eur,
                        eur_to_gbp=eur_to_gbp,
                    )

            market_after_fee = None
            buffer = None
            profit = None
            roi = None
            passes = False

            if buy_price is not None and sell_price is not None:
                market_after_fee, buffer, profit, roi = _compute_profit(buy_price, sell_price)
                passes = bool(profit >= MIN_PROFIT_GBP and roi >= MIN_ROI)

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
                    "market_notes": (resolve_notes or "") + ((" | " + sell_notes) if sell_notes else ""),
                    "elapsed_s": round(time.time() - start, 2),
                }
            )

            if per_item_sleep_s > 0:
                time.sleep(per_item_sleep_s)

    finally:
        _fan_stop(fan)

    batch = pd.DataFrame(rows, columns=SCANS_COLS)

    if watchlist_dirty:
        try:
            watch.reindex(columns=WATCHLIST_COLS).to_csv(watchlist_path, index=False)
            log.info("Updated watchlist with eneba_url/eneba_notes: %s", str(watchlist_path))
        except Exception:
            log.exception("Failed to persist updated watchlist: %s", str(watchlist_path))

    _append_csv(scans_path, batch, SCANS_COLS)

    passes_path.parent.mkdir(parents=True, exist_ok=True)
    batch[batch["passes"] == True].to_csv(passes_path, index=False)

    log.info(
        "Scan complete: attempted=%d wrote=%d passes=%d scans_csv=%s passes_csv=%s",
        int(attempted),
        int(len(batch)),
        int((batch["passes"] == True).sum()) if not batch.empty and "passes" in batch.columns else 0,
        str(scans_path),
        str(passes_path),
    )

    return batch
