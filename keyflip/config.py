from __future__ import annotations

# --- Pricing / profitability assumptions (tune these) ---
SELL_FEE_PCT = 0.12
BUFFER_FIXED_GBP = 0.30
BUFFER_PCT_OF_BUY = 0.05

MIN_PROFIT_GBP = 0.50
MIN_ROI = 0.20

def compute_profit(buy_gbp: float, sell_gbp: float) -> tuple[float, float]:
    """Returns (profit_gbp, roi)."""
    net_sell = sell_gbp * (1.0 - SELL_FEE_PCT)
    buffer = BUFFER_FIXED_GBP + (buy_gbp * BUFFER_PCT_OF_BUY)
    profit = net_sell - buy_gbp - buffer
    roi = profit / buy_gbp if buy_gbp > 0 else -1.0
    return profit, roi

# --- Networking ---
HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# --- Cache defaults ---
PRICE_OK_TTL_S = 60 * 30       # 30 min
FAIL_TTL_SOFT_S = 60 * 3       # timeouts/403s etc
FAIL_TTL_HARD_S = 60 * 30      # 404/no-price etc

# --- Fanatical sources ---
FANATICAL_SOURCES = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "latest": "https://www.fanatical.com/en/latest-deals",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",
}
