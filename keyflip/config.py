from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# ============================================================
# Networking
# ============================================================

HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20
HTTP_TIMEOUT_S = HTTP_READ_TIMEOUT_S

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ScoringWeights:
    profit_weight: float = 1.0
    roi_weight: float = 50.0
    freshness_weight: float = 5.0
    comp_variance_weight: float = 10.0


@dataclass
class RunConfig:
    """
    Canonical config for the eBay mispricing radar.
    """

    root: Optional[Path] = None

    # eBay API
    ebay_app_id: Optional[str] = None
    ebay_global_id: str = "EBAY-GB"
    ebay_site_id: int = 3

    # Rate limiting + retries
    rate_limit_per_min: int = 60
    max_retries: int = 3
    backoff_base_s: float = 0.6
    backoff_max_s: float = 6.0
    request_timeout_s: float = 20.0

    # Currency
    allow_non_gbp: bool = False
    currency_rates_to_gbp: Dict[str, float] = field(
        default_factory=lambda: {"GBP": 1.0, "EUR": 0.86, "USD": 0.79}
    )

    # Profit assumptions
    fee_pct: float = 0.12
    buffer_fixed_gbp: float = 0.30
    buffer_pct_of_buy: float = 0.05

    # Thresholds
    min_profit_gbp: float = 0.50
    min_roi: float = 0.20
    min_sold_comp_gbp: float = 0.0

    # Sold comps
    min_comp_samples: int = 10
    sold_comp_ttl_s: int = 60 * 60 * 6

    # Listing preferences
    prefer_buy_it_now: bool = True
    prefer_newly_listed: bool = True
    freshness_half_life_hours: float = 12.0

    # Scoring
    score_weights: ScoringWeights = field(default_factory=ScoringWeights)

    # Idempotency
    alert_cooldown_days: int = 2

    # Scan
    scan_limit: int = 50
    scan_sleep_s: float = 0.0

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> "RunConfig":
        if "root" in kwargs and kwargs["root"] is not None and not isinstance(kwargs["root"], Path):
            kwargs["root"] = Path(str(kwargs["root"]))

        allowed = set(cls.__dataclass_fields__.keys())
        unknown = [k for k in kwargs.keys() if k not in allowed]
        if unknown:
            raise TypeError(f"Unexpected RunConfig argument(s): {', '.join(unknown)}")

        cfg = cls(**kwargs)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        if self.rate_limit_per_min < 0:
            raise ValueError("rate_limit_per_min must be >= 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.backoff_base_s < 0:
            raise ValueError("backoff_base_s must be >= 0")
        if self.backoff_max_s < 0:
            raise ValueError("backoff_max_s must be >= 0")
        if self.request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be > 0")

        if self.fee_pct < 0 or self.fee_pct >= 1:
            raise ValueError("fee_pct must be between 0 and 1")
        if self.buffer_fixed_gbp < 0:
            raise ValueError("buffer_fixed_gbp must be >= 0")
        if self.buffer_pct_of_buy < 0:
            raise ValueError("buffer_pct_of_buy must be >= 0")

        if self.min_profit_gbp < 0:
            raise ValueError("min_profit_gbp must be >= 0")
        if self.min_roi < 0:
            raise ValueError("min_roi must be >= 0")
        if self.min_sold_comp_gbp < 0:
            raise ValueError("min_sold_comp_gbp must be >= 0")

        if self.min_comp_samples < 0:
            raise ValueError("min_comp_samples must be >= 0")
        if self.sold_comp_ttl_s <= 0:
            raise ValueError("sold_comp_ttl_s must be > 0")

        if self.freshness_half_life_hours <= 0:
            raise ValueError("freshness_half_life_hours must be > 0")
        if self.alert_cooldown_days < 0:
            raise ValueError("alert_cooldown_days must be >= 0")
        if self.scan_limit < 0:
            raise ValueError("scan_limit must be >= 0")
        if self.scan_sleep_s < 0:
            raise ValueError("scan_sleep_s must be >= 0")

    def resolved_app_id(self) -> Optional[str]:
        return self.ebay_app_id or os.getenv("EBAY_APP_ID")

    def rate_to_gbp(self, currency: str) -> Optional[float]:
        if not currency:
            return None
        return self.currency_rates_to_gbp.get(currency.upper())


def compute_profit(
    *,
    total_buy_gbp: float,
    sold_comp_median_gbp: float,
    fee_pct: float,
    buffer_fixed_gbp: float,
    buffer_pct_of_buy: float,
) -> tuple[float, float]:
    net_sell = sold_comp_median_gbp * (1.0 - fee_pct)
    buffer = buffer_fixed_gbp + (total_buy_gbp * buffer_pct_of_buy)
    profit = net_sell - total_buy_gbp - buffer
    roi = profit / total_buy_gbp if total_buy_gbp > 0 else -1.0
    return profit, roi


DEFAULT_WATCHLIST_COLUMNS = [
    "query_id",
    "query_text",
    "category_id",
    "condition",
    "max_buy_gbp",
    "keywords_include",
    "keywords_exclude",
    "min_sold_comp_gbp",
    "min_roi",
    "min_profit_gbp",
]

DEFAULT_SCANS_COLUMNS = [
    "scanned_at_iso",
    "query_id",
    "title",
    "listing_url",
    "price_gbp",
    "shipping_gbp",
    "total_gbp",
    "condition",
    "end_time_iso",
    "seller_feedback",
    "location",
    "sold_comp_median_gbp",
    "est_profit_gbp",
    "est_roi",
    "score",
    "flags_json",
]

DEFAULT_PASSES_COLUMNS = [
    "scanned_at_iso",
    "query_id",
    "title",
    "listing_url",
    "price_gbp",
    "shipping_gbp",
    "total_gbp",
    "reason",
]

# Back-compat defaults
MIN_PROFIT_GBP = RunConfig().min_profit_gbp
MIN_ROI = RunConfig().min_roi
