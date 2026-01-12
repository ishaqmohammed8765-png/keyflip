from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

# ============================================================
# Pricing / profitability assumptions
# ============================================================

# Marketplace fee (fraction). Example: 0.12 = 12%
SELL_FEE_PCT = 0.12

# Profit safety buffers:
# - fixed covers FX spreads / payment fees / rounding
# - pct_of_buy covers price drift risk that grows with buy price
BUFFER_FIXED_GBP = 0.30
BUFFER_PCT_OF_BUY = 0.05

# Minimums to consider a pass
MIN_PROFIT_GBP = 0.50
MIN_ROI = 0.20


@dataclass(frozen=True)
class ProfitConfig:
    """
    Central profit assumptions. Keep these conservative.
    """
    sell_fee_pct: float = SELL_FEE_PCT
    buffer_fixed_gbp: float = BUFFER_FIXED_GBP
    buffer_pct_of_buy: float = BUFFER_PCT_OF_BUY
    min_profit_gbp: float = MIN_PROFIT_GBP
    min_roi: float = MIN_ROI


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except Exception:
        return lo
    return max(lo, min(hi, v))


def _safe_cfg(cfg: ProfitConfig | None) -> ProfitConfig:
    """
    Normalize config and clamp unsafe values to sane ranges.
    """
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
) -> tuple[float, float]:
    """
    Compute profitability.

    Returns:
      (profit_gbp, roi)

    Notes:
      - Invalid inputs return (-1.0, -1.0) to keep downstream logic simple.
      - Config values are clamped to sane ranges to avoid accidental "negative buffers"
        or impossible fee values.
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
    Returns True if profit >= min_profit_gbp and roi >= min_roi.
    """
    c = _safe_cfg(cfg)
    profit, roi = compute_profit(buy_gbp, sell_gbp, cfg=c)
    return profit >= c.min_profit_gbp and roi >= c.min_roi


# ============================================================
# Networking
# ============================================================

# Requests timeout handling
HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20

# Preferred for requests.get(..., timeout=REQUESTS_TIMEOUT)
REQUESTS_TIMEOUT: Tuple[int, int] = (HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S)

# Legacy single-timeout constant
HTTP_TIMEOUT_S = HTTP_READ_TIMEOUT_S

# Unified User-Agent (shared by requests + Playwright)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": UA,
    "Accept-Language": "en-GB,en;q=0.9",
}


# ============================================================
# Cache defaults
# ============================================================

# Successful prices cached longer
PRICE_OK_TTL_S = 60 * 30  # 30 minutes

# Failed price lookups cached shorter (temporary issues)
PRICE_FAIL_TTL_S = 60 * 20  # 20 minutes


# ============================================================
# Fanatical sources (Playwright-friendly)
# ============================================================

# These are JS-rendered listing pages that Playwright can scrape reliably.
# Only /en-*/game/... URLs will be kept by the harvester.
FANATICAL_SOURCES: Dict[str, str] = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "new": "https://www.fanatical.com/en/new",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",
    # Optional filters for more low-price inventory
    "under5": "https://www.fanatical.com/en/search?price_to=5",
    "under10": "https://www.fanatical.com/en/search?price_to=10",
}
