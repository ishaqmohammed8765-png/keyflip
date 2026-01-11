from __future__ import annotations

# --- Pricing / profitability assumptions (tune these) ---
SELL_FEE_PCT = 0.12          # marketplace fee (approx)
BUFFER_FIXED_GBP = 0.30      # fixed buffer for spreads/fees
BUFFER_PCT_OF_BUY = 0.05     # extra buffer as % of buy

MIN_PROFIT_GBP = 0.50        # minimum absolute profit
MIN_ROI = 0.20               # minimum ROI (profit / buy)

# --- Networking ---
HTTP_TIMEOUT_S = 20
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# --- Cache defaults ---
PRICE_OK_TTL_S = 60 * 30        # 30 minutes
PRICE_FAIL_TTL_S = 60 * 20      # 20 minutes

# --- Fanatical sources ---
FANATICAL_SOURCES = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "latest": "https://www.fanatical.com/en/latest-deals",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",
}

