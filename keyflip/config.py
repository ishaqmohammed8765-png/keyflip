from __future__ import annotations

from dataclasses import dataclass

# ============================================================
# Pricing / profitability assumptions
# ============================================================

# Marketplace fee (fraction). Example: 0.12 = 12%
SELL_FEE_PCT = 0.12

# Profit safety buffers:
# - fixed covers FX spreads / payment fees / rounding
# - pct_of_buy covers "price drift" risk that grows with buy price
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
        profit_gbp: net profit after fees + buffers
        roi: profit / buy (or -1.0 if buy <= 0)
    """
    if buy_gbp <= 0:
        return -1.0, -1.0
    if sell_gbp <= 0:
        return -buy_gbp, -1.0

    # Clamp fee into a sane range to avoid nonsense if misconfigured.
    fee = min(max(cfg.sell_fee_pct, 0.0), 0.95)

    net_sell = sell_gbp * (1.0 - fee)
    buffer = cfg.buffer_fixed_gbp + (buy_gbp * cfg.buffer_pct_of_buy)

    profit = net_sell - buy_gbp - buffer
    roi = profit / buy_gbp
    return profit, roi


def is_pass(buy_gbp: float, sell_gbp: float, *, cfg: ProfitConfig = ProfitConfig()) -> bool:
    """
    Convenience helper: returns True if (profit >= MIN_PROFIT) AND (roi >= MIN_ROI).
    """
    profit, roi = compute_profit(buy_gbp, sell_gbp, cfg=cfg)
    return (profit >= cfg.min_profit_gbp) and (roi >= cfg.min_roi)


# ============================================================
# Networking
# ============================================================

# Requests supports timeout as either:
# - float (total-ish) OR
# - tuple(connect_timeout, read_timeout)
HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20

# Preferred for requests.get(..., timeout=REQUESTS_TIMEOUT)
REQUESTS_TIMEOUT = (HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S)

# Legacy single-timeout constant (some modules expect this name)
HTTP_TIMEOUT_S = HTTP_READ_TIMEOUT_S

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Optional headers you can reuse across modules
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-GB,en;q=0.9",
}


# ============================================================
# Cache defaults (names must match core.py if imported there)
# ============================================================

# "OK" prices can be cached longer; failures should be shorter (site hiccups happen).
PRICE_OK_TTL_S = 60 * 30          # 30 minutes
PRICE_FAIL_TTL_S = 60 * 20        # 20 minutes (slightly shorter than OK)

# Optional granular TTLs (only use if your cache layer supports them)
FAIL_TTL_SOFT_S = 60 * 3
FAIL_TTL_HARD_S = PRICE_FAIL_TTL_S


# ============================================================
# Fanatical sources
# ============================================================

FANATICAL_SOURCES: dict[str, str] = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "latest": "https://www.fanatical.com/en/latest-deals",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",
}
