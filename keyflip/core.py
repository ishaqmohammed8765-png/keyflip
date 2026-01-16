from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from .cache import PriceCache
from .config import (
    DEFAULT_PASSES_COLUMNS,
    DEFAULT_SCANS_COLUMNS,
    DEFAULT_WATCHLIST_COLUMNS,
    RunConfig,
    compute_profit,
)
from .ebay_api import EbayApiClient, parse_iso8601

log = logging.getLogger("keyflip.core")

WATCHLIST_COLS = DEFAULT_WATCHLIST_COLUMNS
SCANS_COLS = DEFAULT_SCANS_COLUMNS


@dataclass(frozen=True)
class WatchlistQuery:
    query_id: str
    query_text: str
    category_id: Optional[str]
    condition: Optional[str]
    max_buy_gbp: Optional[float]
    keywords_include: list[str]
    keywords_exclude: list[str]
    min_sold_comp_gbp: Optional[float]
    min_roi: Optional[float]
    min_profit_gbp: Optional[float]


@dataclass(frozen=True)
class SoldCompStats:
    median_gbp: Optional[float]
    sample_size: int
    variance_ratio: float


WATCHLIST_TEMPLATE = [
    {
        "query_id": "q1",
        "query_text": "Nintendo Switch OLED",
        "category_id": "139971",
        "condition": "1000",
        "max_buy_gbp": "220",
        "keywords_include": "boxed,console",
        "keywords_exclude": "spares,parts",
        "min_sold_comp_gbp": "240",
        "min_roi": "0.15",
        "min_profit_gbp": "20",
    },
    {
        "query_id": "q2",
        "query_text": "Sony WH-1000XM5",
        "category_id": "112529",
        "condition": "3000",
        "max_buy_gbp": "180",
        "keywords_include": "",
        "keywords_exclude": "broken,spares",
        "min_sold_comp_gbp": "200",
        "min_roi": "0.2",
        "min_profit_gbp": "25",
    },
]


def build_watchlist(cfg: RunConfig, out_csv: Path, *, overwrite: bool = True) -> int:
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    if out_csv.exists() and not overwrite:
        log.info("Watchlist exists; skipping overwrite: %s", out_csv)
        return int(len(_read_csv_safe(out_csv)))

    df = pd.DataFrame(WATCHLIST_TEMPLATE, columns=DEFAULT_WATCHLIST_COLUMNS)
    df.to_csv(out_csv, index=False)
    log.info("Wrote watchlist template to %s", out_csv)
    return int(len(df))


def scan_watchlist(
    cfg: RunConfig,
    watchlist_path: Path,
    scans_path: Path,
    passes_path: Path,
    db_path: Path,
) -> pd.DataFrame:
    watchlist_path = Path(watchlist_path)
    scans_path = Path(scans_path)
    passes_path = Path(passes_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    watch = _read_csv_safe(watchlist_path)
    if watch.empty:
        log.warning("Watchlist empty: %s", watchlist_path)
        return pd.DataFrame(columns=DEFAULT_SCANS_COLUMNS)

    for col in DEFAULT_WATCHLIST_COLUMNS:
        if col not in watch.columns:
            watch[col] = ""

    queries = [_row_to_query(row, cfg) for row in watch.to_dict(orient="records")]
    queries = [q for q in queries if q.query_text]

    if not queries:
        log.warning("No valid watchlist rows found.")
        return pd.DataFrame(columns=DEFAULT_SCANS_COLUMNS)

    api = EbayApiClient(cfg)
    now_iso = _now_iso()
    scans_rows: list[Dict[str, Any]] = []
    passes_rows: list[Dict[str, Any]] = []

    with PriceCache(db_path) as cache:
        for query in queries[: cfg.scan_limit or None]:
            try:
                comp_stats = _get_sold_comps(query, cfg, api, cache)
            except Exception:
                log.exception("Failed to fetch sold comps for %s", query.query_text)
                comp_stats = SoldCompStats(median_gbp=None, sample_size=0, variance_ratio=0.0)

            listings = []
            try:
                listings = api.search_listings(
                    query_text=query.query_text,
                    category_id=query.category_id,
                    condition=query.condition,
                    max_entries=50,
                )
            except Exception:
                log.exception("Listing search failed for %s", query.query_text)

            for listing in listings:
                flags: Dict[str, Any] = {
                    "listing_id": listing.listing_id,
                    "listing_type": listing.listing_type,
                    "buy_it_now": listing.buy_it_now,
                    "comp_sample": comp_stats.sample_size,
                    "comp_variance": comp_stats.variance_ratio,
                }
                price_gbp, shipping_gbp, total_gbp, currency = _normalize_prices(
                    listing.price,
                    listing.shipping,
                    listing.currency,
                    cfg,
                )
                flags["currency"] = currency

                if total_gbp is None:
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="missing_price",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    scans_rows.append(_scan_row(now_iso, query, listing, comp_stats, None, None, None, flags))
                    continue

                if not _matches_keywords(listing.title, query.keywords_include, query.keywords_exclude):
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="keyword_filter",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    scans_rows.append(
                        _scan_row(now_iso, query, listing, comp_stats, price_gbp, shipping_gbp, total_gbp, flags)
                    )
                    continue

                max_buy = query.max_buy_gbp
                if max_buy is not None and total_gbp > max_buy:
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="over_max_buy",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    scans_rows.append(
                        _scan_row(now_iso, query, listing, comp_stats, price_gbp, shipping_gbp, total_gbp, flags)
                    )
                    continue

                if not comp_stats.median_gbp:
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="no_sold_comps",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    scans_rows.append(
                        _scan_row(now_iso, query, listing, comp_stats, price_gbp, shipping_gbp, total_gbp, flags)
                    )
                    continue

                min_comp = query.min_sold_comp_gbp
                if min_comp is not None and comp_stats.median_gbp < min_comp:
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="below_comp_floor",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    scans_rows.append(
                        _scan_row(now_iso, query, listing, comp_stats, price_gbp, shipping_gbp, total_gbp, flags)
                    )
                    continue

                profit, roi = compute_profit(
                    total_buy_gbp=total_gbp,
                    sold_comp_median_gbp=comp_stats.median_gbp,
                    fee_pct=cfg.fee_pct,
                    buffer_fixed_gbp=cfg.buffer_fixed_gbp,
                    buffer_pct_of_buy=cfg.buffer_pct_of_buy,
                )
                score = _compute_score(cfg, profit, roi, listing.start_time, comp_stats)

                scans_rows.append(
                    _scan_row(
                        now_iso,
                        query,
                        listing,
                        comp_stats,
                        price_gbp,
                        shipping_gbp,
                        total_gbp,
                        flags,
                        profit=profit,
                        roi=roi,
                        score=score,
                    )
                )

                min_profit = query.min_profit_gbp
                min_roi = query.min_roi
                if profit < (min_profit or cfg.min_profit_gbp):
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="min_profit",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    continue
                if roi < (min_roi or cfg.min_roi):
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="min_roi",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    continue

                if cache.is_recent(_listing_cache_key(listing), cfg.alert_cooldown_days):
                    passes_rows.append(
                        _pass_row(
                            now_iso,
                            query,
                            listing,
                            reason="duplicate_listing",
                            price_gbp=price_gbp,
                            shipping_gbp=shipping_gbp,
                            total_gbp=total_gbp,
                        )
                    )
                    continue

                cache.mark_recent(_listing_cache_key(listing))
                log.info(
                    "ALERT %s | %s | profit £%.2f roi %.2f score %.2f",
                    query.query_text,
                    listing.title,
                    profit,
                    roi,
                    score,
                )

    scans_df = pd.DataFrame(scans_rows, columns=DEFAULT_SCANS_COLUMNS)
    passes_df = pd.DataFrame(passes_rows, columns=DEFAULT_PASSES_COLUMNS)

    _append_csv(scans_path, scans_df, DEFAULT_SCANS_COLUMNS)
    if not passes_df.empty:
        _append_csv(passes_path, passes_df, DEFAULT_PASSES_COLUMNS)

    return scans_df


def _get_sold_comps(
    query: WatchlistQuery,
    cfg: RunConfig,
    api: EbayApiClient,
    cache: PriceCache,
) -> SoldCompStats:
    cache_key = f"sold_comp:{query.query_id or query.query_text}"
    cached = cache.get(cache_key)
    if cached and cached.ok and cached.value is not None:
        return _stats_from_cache(cached.value, cached.notes)

    comps = api.fetch_sold_comps(
        query_text=query.query_text,
        category_id=query.category_id,
        condition=query.condition,
        max_entries=60,
    )
    prices = _prices_to_gbp(
        ((c.price, c.currency) for c in comps if c.price is not None),
        cfg,
    )
    stats = _compute_comp_stats(prices, cfg.min_comp_samples)
    cache.set(
        cache_key,
        stats.median_gbp,
        currency="GBP",
        ttl_s=cfg.sold_comp_ttl_s,
        ok=stats.median_gbp is not None,
        notes=json.dumps({"sample": stats.sample_size, "variance": stats.variance_ratio}),
    )
    return stats


def _compute_comp_stats(prices: list[float], min_samples: int) -> SoldCompStats:
    if not prices:
        return SoldCompStats(median_gbp=None, sample_size=0, variance_ratio=0.0)

    prices_sorted = sorted(prices)
    filtered = _trim_outliers(prices_sorted)
    base = filtered if len(filtered) >= min_samples else prices_sorted
    median = statistics.median(base)
    variance_ratio = _variance_ratio(base)
    return SoldCompStats(median_gbp=median, sample_size=len(base), variance_ratio=variance_ratio)


def _trim_outliers(values: list[float]) -> list[float]:
    if len(values) < 4:
        return values
    q1 = statistics.quantiles(values, n=4)[0]
    q3 = statistics.quantiles(values, n=4)[2]
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    return [v for v in values if lower <= v <= upper]


def _variance_ratio(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean_val = statistics.mean(values)
    if mean_val <= 0:
        return 0.0
    stdev = statistics.pstdev(values)
    return stdev / mean_val


def _stats_from_cache(value: float, notes: str) -> SoldCompStats:
    try:
        payload = json.loads(notes or "{}")
    except json.JSONDecodeError:
        payload = {}
    return SoldCompStats(
        median_gbp=value,
        sample_size=int(payload.get("sample", 0)),
        variance_ratio=float(payload.get("variance", 0.0)),
    )


def _row_to_query(row: Dict[str, Any], cfg: RunConfig) -> WatchlistQuery:
    min_sold = _to_float_optional(row.get("min_sold_comp_gbp"))
    if min_sold is None:
        min_sold = cfg.min_sold_comp_gbp

    min_roi = _to_float_optional(row.get("min_roi"))
    if min_roi is None:
        min_roi = cfg.min_roi

    min_profit = _to_float_optional(row.get("min_profit_gbp"))
    if min_profit is None:
        min_profit = cfg.min_profit_gbp

    return WatchlistQuery(
        query_id=_to_str(row.get("query_id")),
        query_text=_to_str(row.get("query_text")),
        category_id=_to_optional(row.get("category_id")),
        condition=_to_optional(row.get("condition")),
        max_buy_gbp=_to_float_optional(row.get("max_buy_gbp")) or None,
        keywords_include=_split_keywords(row.get("keywords_include")),
        keywords_exclude=_split_keywords(row.get("keywords_exclude")),
        min_sold_comp_gbp=min_sold,
        min_roi=min_roi,
        min_profit_gbp=min_profit,
    )


def _matches_keywords(title: str, include: list[str], exclude: list[str]) -> bool:
    hay = (title or "").lower()
    if include:
        if not all(word in hay for word in include):
            return False
    if exclude:
        if any(word in hay for word in exclude):
            return False
    return True


def _normalize_prices(
    price: Optional[float],
    shipping: Optional[float],
    currency: Optional[str],
    cfg: RunConfig,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    if price is None:
        return None, None, None, currency
    shipping_cost = shipping or 0.0
    cur = (currency or "GBP").upper()
    if cur != "GBP":
        if not cfg.allow_non_gbp:
            return None, None, None, cur
        rate = cfg.rate_to_gbp(cur)
        if not rate:
            return None, None, None, cur
        price = price * rate
        shipping_cost = shipping_cost * rate
        cur = "GBP"
    total = price + shipping_cost
    return price, shipping_cost, total, cur


def _prices_to_gbp(items: Iterable[tuple[Optional[float], Optional[str]]], cfg: RunConfig) -> list[float]:
    prices: list[float] = []
    for value, currency in items:
        if value is None:
            continue
        cur = (currency or "GBP").upper()
        if cur != "GBP":
            if not cfg.allow_non_gbp:
                continue
            rate = cfg.rate_to_gbp(cur)
            if not rate:
                continue
            value = value * rate
        prices.append(float(value))
    return prices


def _compute_score(
    cfg: RunConfig,
    profit: float,
    roi: float,
    start_time: Optional[str],
    comp_stats: SoldCompStats,
) -> float:
    weights = cfg.score_weights
    freshness = _freshness_score(start_time, cfg.freshness_half_life_hours)
    score = (
        weights.profit_weight * profit
        + weights.roi_weight * roi
        + weights.freshness_weight * freshness
        - weights.comp_variance_weight * comp_stats.variance_ratio
    )
    return score


def _freshness_score(start_time: Optional[str], half_life_hours: float) -> float:
    dt = parse_iso8601(start_time)
    if not dt:
        return 0.5
    age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    return 0.5 ** (age_hours / half_life_hours)


def _scan_row(
    now_iso: str,
    query: WatchlistQuery,
    listing: Any,
    comp_stats: SoldCompStats,
    price_gbp: Optional[float],
    shipping_gbp: Optional[float],
    total_gbp: Optional[float],
    flags: Dict[str, Any],
    *,
    profit: Optional[float] = None,
    roi: Optional[float] = None,
    score: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "scanned_at_iso": now_iso,
        "query_id": query.query_id,
        "title": listing.title,
        "listing_url": listing.listing_url,
        "price_gbp": price_gbp,
        "shipping_gbp": shipping_gbp,
        "total_gbp": total_gbp,
        "condition": listing.condition,
        "end_time_iso": listing.end_time,
        "seller_feedback": listing.seller_feedback,
        "location": listing.location,
        "sold_comp_median_gbp": comp_stats.median_gbp,
        "est_profit_gbp": profit,
        "est_roi": roi,
        "score": score,
        "flags_json": json.dumps(flags),
    }


def _pass_row(
    now_iso: str,
    query: WatchlistQuery,
    listing: Any,
    *,
    reason: str,
    price_gbp: Optional[float] = None,
    shipping_gbp: Optional[float] = None,
    total_gbp: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "scanned_at_iso": now_iso,
        "query_id": query.query_id,
        "title": listing.title,
        "listing_url": listing.listing_url,
        "price_gbp": price_gbp,
        "shipping_gbp": shipping_gbp,
        "total_gbp": total_gbp,
        "reason": reason,
    }


def _listing_cache_key(listing: Any) -> str:
    return f"listing:{listing.listing_id}"


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def _to_optional(value: Any) -> Optional[str]:
    text = _to_str(value)
    return text or None


def _to_float_optional(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() == "nan":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _split_keywords(value: Any) -> list[str]:
    text = _to_str(value)
    if not text:
        return []
    return [part.strip().lower() for part in text.split(",") if part.strip()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_csv_safe(path: Path) -> pd.DataFrame:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return pd.DataFrame()
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _append_csv(path: Path, batch: pd.DataFrame, cols: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if batch.empty:
        return

    prev = _read_csv_safe(path)
    if prev.empty:
        batch.reindex(columns=cols).to_csv(path, index=False)
        return

    union_cols = list(dict.fromkeys(list(prev.columns) + cols))
    prev2 = prev.reindex(columns=union_cols)
    batch2 = batch.reindex(columns=union_cols)

    pd.concat([prev2, batch2], ignore_index=True).to_csv(path, index=False)
