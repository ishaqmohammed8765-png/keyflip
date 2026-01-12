from __future__ import annotations

from dataclasses import dataclass

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
    sell_fee_pct: float = SELL_FEE_PCT
    buffer_fixed_gbp: float = BUFFER_FIXED_GBP
    buffer_pct_of_buy: float = BUFFER_PCT_OF_BUY
    min_profit_gbp: float = MIN_PROFIT_GBP
    min_roi: float = MIN_ROI


def compute_profit(
    buy_gbp: float,
    sell_gbp: float,
    *,
    cfg: ProfitConfig = ProfitConfig(),
) -> tuple[float, float]:
    """
    Compute profitability.

    Returns:
      (profit_gbp, roi)
    """
    if buy_gbp <= 0 or sell_gbp <= 0:
        return -1.0, -1.0

    fee = min(max(cfg.sell_fee_pct, 0.0), 0.95)
    net_sell = sell_gbp * (1.0 - fee)
    buffer = cfg.buffer_fixed_gbp + (buy_gbp * cfg.buffer_pct_of_buy)

    profit = net_sell - buy_gbp - buffer
    roi = profit / buy_gbp
    return profit, roi


def is_pass(
    buy_gbp: float,
    sell_gbp: float,
    *,
    cfg: ProfitConfig = ProfitConfig(),
) -> bool:
    """
    Returns True if profit >= MIN_PROFIT_GBP and roi >= MIN_ROI
    """
    profit, roi = compute_profit(buy_gbp, sell_gbp, cfg=cfg)
    return profit >= cfg.min_profit_gbp and roi >= cfg.min_roi


# ============================================================
# Networking
# ============================================================

# Requests timeout handling
HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20

# Preferred for requests.get(..., timeout=REQUESTS_TIMEOUT)
REQUESTS_TIMEOUT = (HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S)

# Legacy single-timeout constant
HTTP_TIMEOUT_S = HTTP_READ_TIMEOUT_S

# Unified User-Agent (shared by requests + Playwright)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-GB,en;q=0.9",
}


# ============================================================
# Cache defaults
# ============================================================

# Successful prices cached longer
PRICE_OK_TTL_S = 60 * 30          # 30 minutes

# Failed price lookups cached shorter (temporary issues)
PRICE_FAIL_TTL_S = 60 * 20        # 20 minutes


# ============================================================
# Fanatical sources (Playwright-friendly)
# ============================================================

# These are JS-rendered listing pages that Playwright can scrape reliably.
# Only /en-*/game/... URLs will be kept by the harvester.
FANATICAL_SOURCES: dict[str, str] = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "new": "https://www.fanatical.com/en/new",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",

    # Optional filters for more low-price inventory
    "under5": "https://www.fanatical.com/en/search?price_to=5",
    "under10": "https://www.fanatical.com/en/search?price_to=10",
}
