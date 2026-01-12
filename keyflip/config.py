from __future__ import annotations

"""
Global configuration constants and default parameters for Keyflip, including
profitability thresholds, network timeouts, and user agent settings.
"""

from dataclasses import dataclass
from typing import Dict, Tuple

# ============================================================
# Pricing / profitability assumptions
# ============================================================

# Marketplace selling fee (fraction of price taken as fee). e.g., 0.12 = 12%
SELL_FEE_PCT = 0.12

# Profit safety buffers:
# - BUFFER_FIXED_GBP covers fixed costs (e.g., FX spread, payment fees, rounding)
# - BUFFER_PCT_OF_BUY covers price drift risk proportional to the buy price
BUFFER_FIXED_GBP = 0.30
BUFFER_PCT_OF_BUY = 0.05

# Minimum profit and ROI required for a deal to be considered "passing"
MIN_PROFIT_GBP = 0.50
MIN_ROI = 0.20

@dataclass(frozen=True)
class ProfitConfig:
    """
    Profit threshold configuration.
    All values default to conservative assumptions defined above.
    """
    sell_fee_pct: float = SELL_FEE_PCT
    buffer_fixed_gbp: float = BUFFER_FIXED_GBP
    buffer_pct_of_buy: float = BUFFER_PCT_OF_BUY
    min_profit_gbp: float = MIN_PROFIT_GBP
    min_roi: float = MIN_ROI

def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp numeric value x to the [lo, hi] range (inclusive)."""
    try:
        v = float(x)
    except Exception:
        return lo
    return max(lo, min(hi, v))

def _safe_cfg(cfg: ProfitConfig | None) -> ProfitConfig:
    """Return a ProfitConfig instance with values clamped to safe ranges."""
    c = cfg or ProfitConfig()
    return ProfitConfig(
        sell_fee_pct=_clamp(c.sell_fee_pct, 0.0, 0.95),
        buffer_fixed_gbp=_clamp(c.buffer_fixed_gbp, 0.0, 50.0),
        buffer_pct_of_buy=_clamp(c.buffer_pct_of_buy, 0.0, 0.50),
        min_profit_gbp=float(c.min_profit_gbp),
        min_roi=float(c.min_roi),
    )

def compute_profit(
    buy_gbp: float,
    sell_gbp: float,
    *,
    cfg: ProfitConfig | None = None,
) -> Tuple[float, float]:
    """
    Compute the profit (GBP) and return-on-investment (ROI) from a given buy price and sell price.
    
    Returns:
        (profit_gbp, roi) as a tuple of floats.
    
    Notes:
        - If inputs are invalid or non-positive, returns (-1.0, -1.0).
        - The ProfitConfig (cfg) values are clamped to safe ranges to avoid negative buffers or extreme fees.
    """
    try:
        buy = float(buy_gbp)
        sell = float(sell_gbp)
    except Exception:
        return -1.0, -1.0

    if buy <= 0 or sell <= 0:
        return -1.0, -1.0

    c = _safe_cfg(cfg)
    net_sell = sell * (1.0 - c.sell_fee_pct)
    buffer = c.buffer_fixed_gbp + (buy * c.buffer_pct_of_buy)
    profit = net_sell - buy - buffer
    roi = profit / buy if buy > 0 else -1.0
    return profit, roi

def is_pass(
    buy_gbp: float,
    sell_gbp: float,
    *,
    cfg: ProfitConfig | None = None,
) -> bool:
    """
    Determine if a flip meets the minimum profitability criteria.
    
    Returns True if and only if profit >= min_profit_gbp and ROI >= min_roi for the given (or default) ProfitConfig.
    """
    c = _safe_cfg(cfg)
    profit, roi = compute_profit(buy_gbp, sell_gbp, cfg=c)
    return profit >= c.min_profit_gbp and roi >= c.min_roi

# ============================================================
# Networking defaults
# ============================================================

# Timeout for HTTP requests: (connect_timeout, read_timeout) in seconds
HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20
REQUESTS_TIMEOUT: Tuple[int, int] = (HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S)
# Single-value timeout (legacy usage, equal to read timeout)
HTTP_TIMEOUT_S = HTTP_READ_TIMEOUT_S

# Default User-Agent string for web requests and Playwright
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
# Common headers for HTTP requests
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": UA,
    "Accept-Language": "en-GB,en;q=0.9",
}

# ============================================================
# Cache TTLs (seconds)
# ============================================================

# Cache duration for successful price lookups (longer, since the price is known)
PRICE_OK_TTL_S = 60 * 30   # 30 minutes
# Cache duration for failed price lookups (shorter, to retry sooner in case of transient issues)
PRICE_FAIL_TTL_S = 60 * 20  # 20 minutes

# ============================================================
# Fanatical sources (URLs for Playwright scraping)
# ============================================================

# Key pages on Fanatical to scrape for game deals. Only game pages under /en-*/game/... are considered.
FANATICAL_SOURCES: Dict[str, str] = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "new": "https://www.fanatical.com/en/new",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",
    # Additional search filters for low-price items
    "under5": "https://www.fanatical.com/en/search?price_to=5",
    "under10": "https://www.fanatical.com/en/search?price_to=10",
}
