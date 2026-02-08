from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(slots=True)
class RunSettings:
    marketplace: str = "craigslist"
    craigslist_site: str = "london"
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
    currency_whitelist: tuple[str, ...] = ("GBP", "USD")
    blocked_keywords: tuple[str, ...] = ()
    min_seller_feedback_pct: Optional[float] = None
    min_seller_feedback_score: Optional[int] = None
    allow_missing_shipping_price: bool = True
    assumed_inbound_shipping_gbp: float = 3.50
    use_playwright_fallback: bool = True
    delivery_only: bool = False
    include_ebay_buy_now: bool = True


@dataclass(slots=True)
class AlertSettings:
    discord_webhook_url: Optional[str] = None


@dataclass(slots=True)
class AppConfig:
    db_path: str
    run: RunSettings
    alerts: AlertSettings
