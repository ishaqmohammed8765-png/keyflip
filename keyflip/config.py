# keyflip/config.py
from __future__ import annotations

from dataclasses import dataclass

# ============================================================
# Networking
# ============================================================

HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20

# Some modules expect a single timeout variable:
HTTP_TIMEOUT_S = HTTP_READ_TIMEOUT_S

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ============================================================
# Pricing / profitability assumptions (tune these)
# ============================================================

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


# ============================================================
# Cache TTL defaults
# ============================================================

PRICE_OK_TTL_S = 60 * 30        # 30 minutes
PRICE_FAIL_TTL_S = 60 * 20      # 20 minutes

# Some code names this slightly differently; define both to be safe.
PRICE_OK_TTL = PRICE_OK_TTL_S
PRICE_FAIL_TTL = PRICE_FAIL_TTL_S

# ============================================================
# Fanatical sources (used by builder)
# ============================================================

FANATICAL_SOURCES = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "latest": "https://www.fanatical.com/en/new-releases",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",
}

# ============================================================
# Run configuration (your existing config object)
# ============================================================


@dataclass
class RunConfig:
    max_buy: float
    target: int = 15
    verify_candidates: int = 200
    pages_per_source: int = 5
    verify_limit: int = 0          # 0 = unlimited (but safety_cap still applies)
    safety_cap: int = 20
    avoid_recent_days: int = 0
    allow_eur: bool = False
    eur_to_gbp: float = 0.86
    scan_limit: int = 0            # 0 = unlimited
    item_budget: float = 45.0      # seconds
    run_budget: float = 0.0        # 0 = unlimited

    def __post_init__(self) -> None:
        # Fail-fast validation (prevents silent weird behaviour later)
        if self.max_buy <= 0:
            raise ValueError("max_buy must be > 0")

        if self.target <= 0:
            raise ValueError("target must be > 0")

        if self.verify_candidates <= 0:
            raise ValueError("verify_candidates must be > 0")

        if self.pages_per_source <= 0:
            raise ValueError("pages_per_source must be > 0")

        if self.verify_limit < 0:
            raise ValueError("verify_limit must be >= 0 (0 means unlimited)")

        if self.safety_cap <= 0:
            raise ValueError("safety_cap must be > 0")

        if self.avoid_recent_days < 0:
            raise ValueError("avoid_recent_days must be >= 0")

        if self.scan_limit < 0:
            raise ValueError("scan_limit must be >= 0 (0 means unlimited)")

        if self.item_budget <= 0:
            raise ValueError("item_budget must be > 0 seconds")

        if self.run_budget < 0:
            raise ValueError("run_budget must be >= 0 seconds (0 means unlimited)")

        if self.allow_eur and self.eur_to_gbp <= 0:
            raise ValueError("eur_to_gbp must be > 0 when allow_eur=True")

    # Helpers: safe to add; other files don't need to use them
    def effective_verify_limit(self) -> int:
        """Apply safety cap even if verify_limit is unlimited (0)."""
        if self.verify_limit == 0:
            return self.safety_cap
        return min(self.verify_limit, self.safety_cap)

    def is_unlimited_scan(self) -> bool:
        return self.scan_limit == 0


# ============================================================
# Placeholders (kept so imports don't break)
# ============================================================

def build_watchlist(config: RunConfig, output_path) -> int:
    """
    Build a watchlist using Fanatical scraping logic (placeholder).
    Return: number of items written.
    """
    print("Building watchlist with:", config)
    return 0


def scan_watchlist(config: RunConfig, watchlist_path, scans_path, passes_path, db_path, fail_ttl):
    """
    Scan watchlist using Eneba logic (placeholder).
    """
    print("Scanning watchlist using:", config)
    return
