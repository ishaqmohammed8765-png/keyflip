from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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

# ============================================================
# Pricing / profitability assumptions
# ============================================================

SELL_FEE_PCT = 0.12
BUFFER_FIXED_GBP = 0.30
BUFFER_PCT_OF_BUY = 0.05

MIN_PROFIT_GBP = 0.50
MIN_ROI = 0.20


def compute_profit(buy_gbp: float, sell_gbp: float) -> tuple[float, float]:
    net_sell = sell_gbp * (1.0 - SELL_FEE_PCT)
    buffer = BUFFER_FIXED_GBP + (buy_gbp * BUFFER_PCT_OF_BUY)
    profit = net_sell - buy_gbp - buffer
    roi = profit / buy_gbp if buy_gbp > 0 else -1.0
    return profit, roi


# ============================================================
# Cache TTL
# ============================================================

PRICE_OK_TTL_S = 60 * 30
PRICE_FAIL_TTL_S = 60 * 20

PRICE_OK_TTL = PRICE_OK_TTL_S
PRICE_FAIL_TTL = PRICE_FAIL_TTL_S

# ============================================================
# Fanatical sources
# ============================================================

FANATICAL_SOURCES = {
    "sale": "https://www.fanatical.com/en/on-sale",
    "latest": "https://www.fanatical.com/en/new-releases",
    "top": "https://www.fanatical.com/en/top-sellers",
    "trending": "https://www.fanatical.com/en/trending",
}

# ============================================================
# RunConfig (backwards + forwards compatible)
# ============================================================

class RunConfig:
    """
    Accepts ALL known argument styles used across your project.
    This prevents TypeError crashes without touching other files.
    """

    def __init__(self, **kwargs: Any) -> None:
        # ---- aliases from different versions ----
        if "max_buy_gbp" in kwargs:
            kwargs["max_buy"] = kwargs.pop("max_buy_gbp")

        if "watchlist_target" in kwargs:
            kwargs["target"] = kwargs.pop("watchlist_target")

        if "verify_safety_cap" in kwargs:
            kwargs["safety_cap"] = kwargs.pop("verify_safety_cap")

        if "item_budget_s" in kwargs:
            kwargs["item_budget"] = kwargs.pop("item_budget_s")

        if "run_budget_s" in kwargs:
            kwargs["run_budget"] = kwargs.pop("run_budget_s")

        if "scan_limit_s" in kwargs:
            kwargs["scan_limit"] = kwargs.pop("scan_limit_s")

        # optional extras some files pass
        root = kwargs.pop("root", None)
        self.root: Optional[Path] = Path(root) if root else None
        self.cache_fail_ttl = kwargs.pop(
            "cache_fail_ttl",
            kwargs.pop("fail_ttl", None)
        )

        # ---- canonical fields ----
        self.max_buy: float = float(kwargs.pop("max_buy"))
        self.target: int = int(kwargs.pop("target", 15))
        self.verify_candidates: int = int(kwargs.pop("verify_candidates", 200))
        self.pages_per_source: int = int(kwargs.pop("pages_per_source", 5))
        self.verify_limit: int = int(kwargs.pop("verify_limit", 0))
        self.safety_cap: int = int(kwargs.pop("safety_cap", 20))
        self.avoid_recent_days: int = int(kwargs.pop("avoid_recent_days", 0))
        self.allow_eur: bool = bool(kwargs.pop("allow_eur", False))
        self.eur_to_gbp: float = float(kwargs.pop("eur_to_gbp", 0.86))
        self.scan_limit: int = int(kwargs.pop("scan_limit", 0))
        self.item_budget: float = float(kwargs.pop("item_budget", 45.0))
        self.run_budget: float = float(kwargs.pop("run_budget", 0.0))

        if kwargs:
            raise TypeError(f"Unexpected RunConfig argument(s): {', '.join(kwargs)}")

        self._validate()

    # ---- compatibility properties ----
    @property
    def max_buy_gbp(self) -> float:
        return self.max_buy

    @property
    def watchlist_target(self) -> int:
        return self.target

    @property
    def verify_safety_cap(self) -> int:
        return self.safety_cap

    @property
    def item_budget_s(self) -> float:
        return self.item_budget

    @property
    def run_budget_s(self) -> float:
        return self.run_budget

    # ---- helpers ----
    def effective_verify_limit(self) -> int:
        if self.verify_limit == 0:
            return self.safety_cap
        return min(self.verify_limit, self.safety_cap)

    def is_unlimited_scan(self) -> bool:
        return self.scan_limit == 0

    # ---- validation ----
    def _validate(self) -> None:
        if self.max_buy <= 0:
            raise ValueError("max_buy must be > 0")
        if self.target <= 0:
            raise ValueError("target must be > 0")
        if self.verify_candidates <= 0:
            raise ValueError("verify_candidates must be > 0")
        if self.pages_per_source <= 0:
            raise ValueError("pages_per_source must be > 0")
        if self.verify_limit < 0:
            raise ValueError("verify_limit must be >= 0")
        if self.safety_cap <= 0:
            raise ValueError("safety_cap must be > 0")
        if self.scan_limit < 0:
            raise ValueError("scan_limit must be >= 0")
        if self.item_budget <= 0:
            raise ValueError("item_budget must be > 0")
        if self.run_budget < 0:
            raise ValueError("run_budget must be >= 0")
        if self.allow_eur and self.eur_to_gbp <= 0:
            raise ValueError("eur_to_gbp must be > 0 when allow_eur=True")


# ============================================================
# Placeholders (do not remove – imports rely on them)
# ============================================================

def build_watchlist(config: RunConfig, output_path) -> int:
    print("Building watchlist with:", config.__dict__)
    return 0


def scan_watchlist(
    config: RunConfig,
    watchlist_path,
    scans_path,
    passes_path,
    db_path,
    fail_ttl,
):
    print("Scanning watchlist with:", config.__dict__)
