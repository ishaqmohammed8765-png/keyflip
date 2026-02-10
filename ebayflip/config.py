from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

MIN_PROFIT_GBP = 5.0
MIN_ROI = 0.15
MIN_CONFIDENCE = 0.40

EBAY_FEE_PCT_DEFAULT = 0.128
SHIPPING_OUT_GBP_DEFAULT = 4.0
BUFFER_FIXED_GBP = 2.0
BUFFER_PCT_OF_BUY = 0.05

DEFAULT_REQUEST_CAP = 60
DEFAULT_COMPS_LIMIT = 30
DEFAULT_SCAN_LIMIT_PER_TARGET = 25
DEFAULT_SCAN_INTERVAL_MIN = 15
DEFAULT_COMPS_TTL_HOURS = 12

DEFAULT_GBP_EXCHANGE_RATE = 0.78


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _default_craigslist_site() -> str:
    override = os.getenv("CRAIGSLIST_SITE")
    if override and override.strip():
        return override.strip().lower()
    locale = os.getenv("LOCALE", "").lower()
    country = os.getenv("COUNTRY", "").lower()
    if locale.startswith("en_us") or country in {"us", "usa", "united states"}:
        return "sfbay"
    return "sfbay"


def _default_currency_whitelist() -> tuple[str, ...]:
    configured = os.getenv("CURRENCY_WHITELIST")
    if not configured:
        return ("GBP", "USD")
    values = tuple(part.strip().upper() for part in configured.split(",") if part.strip())
    return values or ("GBP", "USD")


def _sanitize_sell_marketplace(raw: str) -> str:
    allowed = {"ebay", "mercari", "poshmark"}
    values: list[str] = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part or part not in allowed:
            continue
        if part in values:
            continue
        values.append(part)
    return ",".join(values) if values else "ebay"


@dataclass(slots=True)
class RunSettings:
    marketplace: str = "ebay"
    sell_marketplace: str = "ebay,mercari,poshmark"
    craigslist_site: str = field(default_factory=_default_craigslist_site)
    min_profit_gbp: float = MIN_PROFIT_GBP
    min_roi: float = MIN_ROI
    min_confidence: float = MIN_CONFIDENCE
    ebay_fee_pct: float = EBAY_FEE_PCT_DEFAULT
    shipping_out_gbp: float = SHIPPING_OUT_GBP_DEFAULT
    buffer_fixed_gbp: float = BUFFER_FIXED_GBP
    buffer_pct_of_buy: float = BUFFER_PCT_OF_BUY
    request_cap: int = DEFAULT_REQUEST_CAP
    comps_limit: int = DEFAULT_COMPS_LIMIT
    scan_limit_per_target: int = DEFAULT_SCAN_LIMIT_PER_TARGET
    comps_ttl_hours: int = DEFAULT_COMPS_TTL_HOURS
    allow_non_gbp: bool = True
    gbp_exchange_rate: float = DEFAULT_GBP_EXCHANGE_RATE
    currency_whitelist: tuple[str, ...] = field(default_factory=_default_currency_whitelist)
    blocked_keywords: tuple[str, ...] = ()
    min_seller_feedback_pct: Optional[float] = None
    min_seller_feedback_score: Optional[int] = None
    allow_missing_shipping_price: bool = True
    assumed_inbound_shipping_gbp: float = 3.50
    use_playwright_fallback: bool = True
    delivery_only: bool = True
    include_ebay_buy_now: bool = True
    listing_max_age_hours: int = 72
    scan_workers: int = 4
    live_fx_enabled: bool = True
    fx_cache_minutes: int = 360
    missing_shipping_penalty_gbp: float = 4.0
    missing_shipping_confidence_penalty: float = 0.08
    auto_popular_targets: bool = True
    popular_targets_per_category: int = 3
    auto_smart_targets: bool = True
    auto_smart_target_limit: int = 3
    min_smart_target_confidence: float = 0.60
    min_smart_target_profit_gbp: float = 10.0

    @classmethod
    def from_env(cls, **overrides: object) -> "RunSettings":
        marketplace = os.getenv("MARKETPLACE", "ebay").strip().lower() or "ebay"
        sell_marketplace_raw = (
            os.getenv("SELL_MARKETPLACE", "ebay,mercari,poshmark").strip().lower()
            or "ebay,mercari,poshmark"
        )
        sell_marketplace = _sanitize_sell_marketplace(sell_marketplace_raw)
        include_buy_now_raw = os.getenv("INCLUDE_EBAY_BUY_NOW")
        if include_buy_now_raw is None:
            include_buy_now = marketplace == "ebay"
        else:
            include_buy_now = include_buy_now_raw.strip().lower() in {"1", "true", "yes", "y", "on"}
        kwargs: dict[str, object] = {
            "marketplace": marketplace,
            "sell_marketplace": sell_marketplace,
            "craigslist_site": _default_craigslist_site(),
            "request_cap": int(os.getenv("REQUEST_CAP", str(DEFAULT_REQUEST_CAP))),
            "scan_limit_per_target": int(
                os.getenv("SCAN_LIMIT_PER_TARGET", str(DEFAULT_SCAN_LIMIT_PER_TARGET))
            ),
            "comps_limit": int(os.getenv("COMPS_LIMIT", str(DEFAULT_COMPS_LIMIT))),
            "delivery_only": _env_bool("DELIVERY_ONLY", True),
            "include_ebay_buy_now": include_buy_now,
            "allow_non_gbp": _env_bool("ALLOW_NON_GBP", True),
            "gbp_exchange_rate": float(os.getenv("GBP_EXCHANGE_RATE", str(DEFAULT_GBP_EXCHANGE_RATE))),
            "currency_whitelist": _default_currency_whitelist(),
            "use_playwright_fallback": _env_bool("EBAY_USE_PLAYWRIGHT", True),
            "listing_max_age_hours": int(os.getenv("LISTING_MAX_AGE_HOURS", "72")),
            "scan_workers": max(1, int(os.getenv("SCAN_WORKERS", "4"))),
            "live_fx_enabled": _env_bool("LIVE_FX_ENABLED", True),
            "fx_cache_minutes": max(10, int(os.getenv("FX_CACHE_MINUTES", "360"))),
            "missing_shipping_penalty_gbp": float(os.getenv("MISSING_SHIPPING_PENALTY_GBP", "4.0")),
            "missing_shipping_confidence_penalty": float(
                os.getenv("MISSING_SHIPPING_CONFIDENCE_PENALTY", "0.08")
            ),
            "auto_popular_targets": _env_bool("AUTO_POPULAR_TARGETS", True),
            "popular_targets_per_category": max(
                1, int(os.getenv("POPULAR_TARGETS_PER_CATEGORY", "3"))
            ),
            "auto_smart_targets": _env_bool("AUTO_SMART_TARGETS", True),
            "auto_smart_target_limit": max(1, int(os.getenv("AUTO_SMART_TARGET_LIMIT", "3"))),
            "min_smart_target_confidence": float(os.getenv("MIN_SMART_TARGET_CONFIDENCE", "0.60")),
            "min_smart_target_profit_gbp": float(os.getenv("MIN_SMART_TARGET_PROFIT_GBP", "10.0")),
        }
        kwargs.update(overrides)
        return cls(**kwargs)


@dataclass(slots=True)
class AlertSettings:
    discord_webhook_url: Optional[str] = None


@dataclass(slots=True)
class AppConfig:
    db_path: str
    run: RunSettings
    alerts: AlertSettings
