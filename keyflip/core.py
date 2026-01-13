from __future__ import annotations

import logging
import random
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .cache import PriceCache
from .config import (
    RunConfig,
    BUFFER_FIXED_GBP,
    BUFFER_PCT_OF_BUY,
    FANATICAL_SOURCES,  # kept name for compatibility (sources are Loaded/CDKeys now)
    MIN_PROFIT_GBP,
    MIN_ROI,
    PRICE_FAIL_TTL_S,
    PRICE_OK_TTL_S,
    SELL_FEE_PCT,
)
from .eneba import make_store_search_url, read_price_gbp, resolve_product_url_from_store
from .fanatical_pw import FanaticalPWClient

log = logging.getLogger("keyflip.core")
log.info("Buy backend: Playwright (reused client)")

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
    seen: set[str] = set()
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


def _ttl_fail(cfg: RunConfig, fail_ttl_override: Optional[int] = None) -> int:
    """
    Resolve fail TTL in a compatibility-friendly way.
    Priority:
      1) override parameter
      2) cfg.cache_fail_ttl or cfg.cache_fail_ttl_s
      3) PRICE_FAIL_TTL_S constant
    """
    if fail_ttl_override is not None:
        try:
            v = int(fail_ttl_override)
            if v > 0:
                return v
        except Exception:
            pass

    for name in ("cache_fail_ttl", "cache_fail_ttl_s"):
        if hasattr(cfg, name):
            try:
                v = int(getattr(cfg, name) or 0)
                if v > 0:
                    return v
            except Exception:
                pass

    return int(PRICE_FAIL_TTL_S)


def _join_notes(*xs: str) -> str:
    return "; ".join(x.strip() for x in xs if x and str(x).strip())


def _effective_verify_cap(cfg: RunConfig) -> int:
    """
    Some versions of RunConfig provide a method. Otherwise compute:
      - verify_limit (0 = unlimited)
      - verify_safety_cap (or safety_cap)
    """
    if hasattr(cfg, "effective_verify_limit"):
        try:
            return int(cfg.effective_verify_limit())
        except Exception:
            pass

    verify_limit = int(getattr(cfg, "verify_limit", 0) or 0)
    safety_cap = int(getattr(cfg, "verify_safety_cap", getattr(cfg, "safety_cap", 0)) or 0)

    if verify_limit <= 0:
        return safety_cap if safety_cap > 0 else 0
    if safety_cap > 0:
        return min(verify_limit, safety_cap)
    return verify_limit


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
    """
    Append while keeping backwards compatibility with schema drift:
    - If file doesn't exist: write with desired cols
    - If exists: union columns and append
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = _read_csv_safe(path)

    if prev.empty:
        batch.reindex(columns=cols).to_csv(path, index=False)
        return

    union_cols = list(dict.fromkeys(list(prev.columns) + cols))
    prev2 = prev.reindex(columns=union_cols)
    batch2 = batch.reindex(columns=union_cols)
    pd.concat([prev2, batch2], ignore_index=True).to_csv(path, index=False)


def _get_run_limits(cfg: RunConfig) -> tuple[float, float]:
    run_budget = float(getattr(cfg, "run_budget", getattr(cfg, "run_budget_s", 0.0)) or 0.0)
    item_budget = float(getattr(cfg, "item_budget", getattr(cfg, "item_budget_s", 0.0)) or 0.0)
    return run_budget, item_budget


def _get_thresholds(cfg: RunConfig) -> tuple[float, int]:
    max_buy = float(getattr(cfg, "max_buy", getattr(cfg, "max_buy_gbp", 0.0)) or 0.0)
    target = int(getattr(cfg, "target", getattr(cfg, "watchlist_target", 10)) or 10)
    return max_buy, target


def _resolve_db_path(cfg: RunConfig, out_csv: Path, db_path: Optional[Path] = None) -> Path:
    """
    Use stable DB location:
      - explicit db_path if provided
      - cfg.root/price_cache.sqlite if cfg.root exists
      - out_csv sibling price_cache.sqlite
    """
    if db_path is not None:
        return Path(db_path)

    root = getattr(cfg, "root", None)
    if root:
        try:
            return Path(root) / "price_cache.sqlite"
        except Exception:
            pass

    return out_csv.parent / "price_cache.sqlite"


def _open_cache(db_path: Optional[Path]) -> PriceCache:
    """
    Support multiple PriceCache constructor signatures without changing cache.py.
    """
    if db_path is None:
        db_path = Path("price_cache.sqlite")
    db_path = Path(db_path)

    for ctor in (
        lambda: PriceCache(db_path),
        lambda: PriceCache(path=db_path),
        lambda: PriceCache(str(db_path)),
        lambda: PriceCache(path=str(db_path)),
        lambda: PriceCache(db_path=str(db_path)),
    ):
        try:
            return ctor()
        except TypeError:
            continue

    raise TypeError(
        "Unsupported PriceCache constructor. Tried: PriceCache(path/str), PriceCache(path=...), PriceCache(db_path=...)."
    )


# ============================================================
# Cache wrappers
# ============================================================

def get_buy(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
    *,
    fan: Optional[FanaticalPWClient] = None,
    fail_ttl_override: Optional[int] = None,
) -> Tuple[Optional[str], Optional[float], str]:
    """
    Read buy price (GBP) with caching.
    Returns (title, price_gbp, notes).
    """
    key = _buy_key(url)
    if not key:
        return None, None, "failed (empty url)"

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
        cache.set(key, None, "GBP", ttl_s=_ttl_fail(cfg, fail_ttl_override), ok=False, notes=notes)
        return title, None, notes

    cache.set(key, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return title, float(price), notes


def get_sell(
    cfg: RunConfig,
    cache: PriceCache,
    url: str,
    *,
    fail_ttl_override: Optional[int] = None,
) -> Tuple[Optional[float], str]:
    """
    Read sell/market price (GBP) with caching.
    Returns (price_gbp, notes).
    """
    key = (url or "").strip()
    if not key:
        return None, "failed (empty url)"

    c = cache.get(key)
    if c:
        if c.ok and c.value is not None:
            return float(c.value), f"cache: {c.notes}"
        return None, f"cache-fail: {c.notes}"

    price, notes = read_price_gbp(url)
    if price is None:
        cache.set(key, None, "GBP", ttl_s=_ttl_fail(cfg, fail_ttl_override), ok=False, notes=notes)
        return None, notes

    cache.set(key, float(price), "GBP", ttl_s=int(PRICE_OK_TTL_S), ok=True, notes=notes)
    return float(price), notes


# ============================================================
# PUBLIC API
# ============================================================

def build_watchlist(cfg: RunConfig, out_csv: Path) -> int:
    """
    Build watchlist.csv by harvesting product links and verifying buy price in GBP.

    Fixes / improvements:
    - Stable DB path resolution (prevents accidental new DB per run)
    - Never writes an empty CSV with "no columns" (always includes headers)
    - Better pool ordering: cheapest cached first, then unknown
    - Avoid-recent logic is recorded in debug CSV
    - Verify cap is treated as a reporting cap; build still tries to hit target
    """
    start = time.time()
    run_budget, item_budget = _get_run_limits(cfg)
    max_buy, target = _get_thresholds(cfg)

    pages = int(getattr(cfg, "pages_per_source", 2) or 2)
    verify_candidates = int(getattr(cfg, "verify_candidates", 200) or 200)
    avoid_days = int(getattr(cfg, "avoid_recent_days", 0) or 0)

    verify_cap = _effective_verify_cap(cfg)  # 0 => unlimited (kept for logging)
    hard_try_cap = verify_candidates if verify_candidates > 0 else 500

    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    debug_csv = out_csv.parent / "watchlist_debug.csv"
    db_path = _resolve_db_path(cfg, out_csv)
    cache = _open_cache(db_path)

    links: List[str] = []
    rows: List[Dict[str, object]] = []
    debug_rows: List[Dict[str, object]] = []

    kept = 0
    attempted = 0

    with FanaticalPWClient(headless=True) as fan:
        # Harvest links from sources
        for name, src_url in FANATICAL_SOURCES.items():
            if not _budget_ok(start, run_budget):
                break
            try:
                new_links = fan.harvest_game_links(
                    src_url,
                    pages=pages,
                    max_links=1000,
                    sleep_range_s=(0.25, 0.75),
                )
                links.extend(new_links)
                log.info("Harvested %d links from %s", len(new_links), name)
            except Exception as e:
                log.warning("Harvest failed for %s: %s", name, e)

        links = _dedupe(links)

        # Always write headers to avoid downstream EmptyDataError
        if not links:
            pd.DataFrame([], columns=WATCHLIST_COLS).to_csv(out_csv, index=False)
            pd.DataFrame([], columns=["url", "title", "price", "decision", "notes"]).to_csv(debug_csv, index=False)
            log.warning("Built watchlist: harvested=0 (no links). out=%s debug=%s", out_csv, debug_csv)
            return 0

        random.shuffle(links)
        pool = links[:verify_candidates] if verify_candidates > 0 else links

        # Prioritize cached cheapest (improves hit rate) and respect avoid_recent_days
        cached_ok: List[Tuple[float, str]] = []
        unknown: List[str] = []

        for u in pool:
            key = _buy_key(u)
            if not key:
                debug_rows.append({"url": u, "title": "", "price": "", "decision": "skip_bad_url", "notes": ""})
                continue

            if avoid_days > 0 and cache.is_recent(_recent_key("build", key), avoid_days):
                debug_rows.append({"url": u, "title": "", "price": "", "decision": "skip_recent", "notes": ""})
                continue

            c = cache.get(key)
            if c and c.ok and c.value is not None:
                try:
                    cached_ok.append((float(c.value), u))
                except Exception:
                    unknown.append(u)
            else:
                unknown.append(u)

        cached_ok.sort(key=lambda t: t[0])
        ordered_pool = [u for _, u in cached_ok] + unknown

        # Main loop: fill target while respecting run budget / hard try cap
        for url in ordered_pool:
            if kept >= target:
                break
            if attempted >= hard_try_cap:
                break
            if not _budget_ok(start, run_budget):
                break

            attempted += 1
            t0 = time.time()

            title, buy_price, notes = get_buy(cfg, cache, url, fan=fan)

            elapsed = time.time() - t0
            if item_budget > 0 and elapsed > item_budget:
                debug_rows.append(
                    {"url": url, "title": title or "", "price": "", "decision": "skip_budget", "notes": notes}
                )
                continue

            if buy_price is None:
                debug_rows.append(
                    {"url": url, "title": title or "", "price": "", "decision": "reject_no_price", "notes": notes}
                )
                continue

            if buy_price > max_buy:
                debug_rows.append(
                    {
                        "url": url,
                        "title": title or "",
                        "price": float(buy_price),
                        "decision": "reject_too_expensive",
                        "notes": notes,
                    }
                )
                continue

            rows.append(
                {
                    "title": title or url.rsplit("/", 1)[-1],
                    "buy_url": url,
                    "buy_price_gbp": float(buy_price),
                    "buy_notes": notes,
                }
            )
            debug_rows.append(
                {"url": url, "title": title or "", "price": float(buy_price), "decision": "KEEP", "notes": notes}
            )
            kept += 1
            cache.mark_recent(_recent_key("build", _buy_key(url)))

    df = pd.DataFrame(rows, columns=WATCHLIST_COLS)
    df.to_csv(out_csv, index=False)

    dbg = pd.DataFrame(debug_rows, columns=["url", "title", "price", "decision", "notes"])
    dbg.to_csv(debug_csv, index=False)

    log.info(
        "Built watchlist: harvested=%d pool=%d attempted=%d kept=%d target=%d max_buy=%.2f verify_cap=%d out=%s debug=%s db=%s",
        len(links),
        len(pool),
        attempted,
        kept,
        target,
        max_buy,
        verify_cap,
        out_csv,
        debug_csv,
        db_path,
    )

    if kept == 0 and not dbg.empty and "decision" in dbg.columns:
        top = dbg["decision"].value_counts().head(3).to_dict()
        log.warning("Watchlist empty. Top reasons: %s (see %s)", top, debug_csv)

    return int(len(df))


def scan_watchlist(
    cfg: RunConfig,
    watchlist_path: Path,
    scans_path: Path,
    passes_path: Path,
    db_path: Path,
    fail_ttl: int,
) -> pd.DataFrame:
    """
    Scan watchlist.csv, compare buy vs market prices, append to scans.csv, and write passes.csv.

    Fixes / improvements:
    - Safe CSV reads (no EmptyDataError)
    - Randomized scan order with "fresh-first" based on avoid_recent_days
    - Respects scan_limit and run/item budgets
    - Always writes passes.csv with headers even when empty
    """
    cache = _open_cache(Path(db_path))

    run_budget, item_budget = _get_run_limits(cfg)
    avoid_days = int(getattr(cfg, "avoid_recent_days", 0) or 0)
    scan_limit = int(getattr(cfg, "scan_limit", 0) or 0)

    watchlist_path = Path(watchlist_path)
    scans_path = Path(scans_path)
    passes_path = Path(passes_path)

    if not watchlist_path.exists():
        log.error("Watchlist file not found: %s", watchlist_path)
        return pd.DataFrame(columns=SCANS_COLS)

    watch = _read_csv_safe(watchlist_path).fillna("")
    if watch.empty:
        log.warning("Watchlist is empty. Skipping scan.")
        return pd.DataFrame(columns=SCANS_COLS)

    for col in ("title", "buy_url"):
        if col not in watch.columns:
            log.error("Watchlist missing required column '%s'.", col)
            return pd.DataFrame(columns=SCANS_COLS)

    start = time.time()
    batch_id = uuid.uuid4().hex[:12]

    def was_scanned_recently(buy_url: str) -> bool:
        if avoid_days <= 0:
            return False
        return cache.is_recent(_recent_key("scan", _buy_key(buy_url)), avoid_days)

    # Fresh-first shuffle (so each run likely scans different items first)
    watch2 = watch.copy()
    watch2["__recent_scan"] = watch2["buy_url"].astype(str).apply(was_scanned_recently)

    fresh = watch2[watch2["__recent_scan"] == False].sample(frac=1.0, random_state=None)
    stale = watch2[watch2["__recent_scan"] == True].sample(frac=1.0, random_state=None)
    watch2 = pd.concat([fresh, stale], ignore_index=True).drop(columns=["__recent_scan"])

    rows: List[Dict[str, object]] = []

    with FanaticalPWClient(headless=True) as fan:
        for _, r in watch2.iterrows():
            if not _budget_ok(start, run_budget):
                break
            if scan_limit > 0 and len(rows) >= scan_limit:
                break

            title = str(r.get("title", "")).strip()
            buy_url = str(r.get("buy_url", "")).strip()
            if not buy_url:
                continue

            t0 = time.time()

            _, buy_price, buy_notes = get_buy(cfg, cache, buy_url, fan=fan, fail_ttl_override=fail_ttl)

            store_url = make_store_search_url(title)
            prod_url, resolve_notes = resolve_product_url_from_store(store_url, title)

            sell_price: Optional[float] = None
            sell_notes = ""
            if prod_url:
                sell_price, sell_notes = get_sell(cfg, cache, prod_url, fail_ttl_override=fail_ttl)

            market_after_fee: Optional[float] = None
            buffer: Optional[float] = None
            profit: Optional[float] = None
            roi: Optional[float] = None
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

            elapsed = time.time() - t0
            if item_budget > 0 and elapsed > item_budget:
                continue

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
                    "elapsed_s": round(elapsed, 2),
                }
            )

            cache.mark_recent(_recent_key("scan", _buy_key(buy_url)))

    batch = pd.DataFrame(rows, columns=SCANS_COLS)
    _append_csv(scans_path, batch, SCANS_COLS)

    # Always write passes.csv with headers
    passes_path.parent.mkdir(parents=True, exist_ok=True)
    if not batch.empty and "passes" in batch.columns:
        batch[batch["passes"] == True].to_csv(passes_path, index=False)
    else:
        pd.DataFrame([], columns=SCANS_COLS).to_csv(passes_path, index=False)

    passes_count = int(batch["passes"].sum()) if not batch.empty and "passes" in batch.columns else 0
    log.info(
        "Scan complete: scanned=%d passes=%d scans=%s passes=%s",
        len(batch),
        passes_count,
        scans_path,
        passes_path,
    )
    return batch
