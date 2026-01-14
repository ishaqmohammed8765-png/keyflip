from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# ============================================================
# Networking
# ============================================================

HTTP_CONNECT_TIMEOUT_S = 6
HTTP_READ_TIMEOUT_S = 20

# Some code expects a single timeout value
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
    """Returns (profit_gbp, roi)."""
    net_sell = sell_gbp * (1.0 - SELL_FEE_PCT)
    buffer = BUFFER_FIXED_GBP + (buy_gbp * BUFFER_PCT_OF_BUY)
    profit = net_sell - buy_gbp - buffer
    roi = profit / buy_gbp if buy_gbp > 0 else -1.0
    return profit, roi


# ============================================================
# Cache TTL
# ============================================================

PRICE_OK_TTL_S = 60 * 30    # 30 minutes
PRICE_FAIL_TTL_S = 60 * 20  # 20 minutes

# Back-compat aliases some files may import
PRICE_OK_TTL = PRICE_OK_TTL_S
PRICE_FAIL_TTL = PRICE_FAIL_TTL_S


# ============================================================
# Buy-side sources (trusted)
# ============================================================

@dataclass(frozen=True)
class BuySource:
    key: str
    url: str
    label: str
    trust_rating: str


TRUST_RATING_SCORES = {
    "A+": 5,
    "A": 4,
    "A-": 3,
    "B+": 2,
    "B": 1,
    "C": 0,
}


def trust_score(rating: str) -> int:
    return TRUST_RATING_SCORES.get((rating or "").upper(), 0)


TRUSTED_BUY_SOURCES: tuple[BuySource, ...] = (
    # Loaded.com (trusted retailer)
    BuySource("loaded_deals", "https://www.loaded.com/deals", "Loaded Deals", "A"),
    BuySource("loaded_deals_pc", "https://www.loaded.com/deals/pc", "Loaded Deals (PC)", "A"),
    BuySource("loaded_explore_action", "https://www.loaded.com/explore/action-games", "Loaded Action", "A"),
    BuySource("loaded_explore_adventure", "https://www.loaded.com/explore/adventure-games", "Loaded Adventure", "A"),
    BuySource("loaded_explore_arcade", "https://www.loaded.com/explore/arcade-games", "Loaded Arcade", "A"),
    BuySource("loaded_explore_open_world", "https://www.loaded.com/explore/open-world-games", "Loaded Open World", "A"),
    BuySource("loaded_explore_rpg", "https://www.loaded.com/explore/rpg-games", "Loaded RPG", "A"),
    BuySource("loaded_explore_strategy", "https://www.loaded.com/explore/strategy-games", "Loaded Strategy", "A"),
    BuySource("loaded_explore_simulation", "https://www.loaded.com/explore/simulation-games", "Loaded Simulation", "A"),
    BuySource("loaded_explore_sports", "https://www.loaded.com/explore/sports-games", "Loaded Sports", "A"),
    BuySource("loaded_explore_indie", "https://www.loaded.com/explore/indie-games", "Loaded Indie", "A"),
    BuySource("loaded_explore_horror", "https://www.loaded.com/explore/horror-games", "Loaded Horror", "A"),
    BuySource("loaded_explore_shooter", "https://www.loaded.com/explore/shooter-games", "Loaded Shooter", "A"),
    # CDKeys (trusted retailer)
    BuySource("cdkeys_deals", "https://www.cdkeys.com/deals", "CDKeys Deals", "A-"),
    BuySource("cdkeys_pc", "https://www.cdkeys.com/pc", "CDKeys PC", "A-"),
    BuySource("cdkeys_playstation", "https://www.cdkeys.com/playstation", "CDKeys PlayStation", "A-"),
    BuySource("cdkeys_xbox", "https://www.cdkeys.com/xbox", "CDKeys Xbox", "A-"),
    BuySource("cdkeys_nintendo", "https://www.cdkeys.com/nintendo", "CDKeys Nintendo", "A-"),
)


def trusted_buy_sources() -> tuple[BuySource, ...]:
    return TRUSTED_BUY_SOURCES


def buy_source_urls() -> Dict[str, str]:
    return {src.key: src.url for src in TRUSTED_BUY_SOURCES}


def buy_source_for_url(url: str) -> Optional[BuySource]:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return None
    if not host:
        return None
    host = host.split(":")[0].lstrip("www.")
    for src in TRUSTED_BUY_SOURCES:
        src_host = urlparse(src.url).netloc.lower().split(":")[0].lstrip("www.")
        if host == src_host:
            return src
    return None


# Compatibility aliases (older modules may still import these)
LOADED_SOURCES = buy_source_urls()
CDKEYS_SOURCES = LOADED_SOURCES
FANATICAL_SOURCES = LOADED_SOURCES


# ============================================================
# RunConfig (backwards + forwards compatible)
# ============================================================

def _parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


@dataclass
class RunConfig:
    """
    Canonical config fields used by the Streamlit app.
    This dataclass is deliberately flexible: it accepts older naming via from_kwargs().
    """

    # optional project root (not required by app)
    root: Optional[Path] = None

    # core controls
    max_buy: float = 10.0
    target: int = 15

    # build controls
    verify_candidates: int = 200
    pages_per_source: int = 5
    verify_limit: int = 0  # 0 => use safety cap
    safety_cap: int = 20

    # scan controls
    scan_limit: int = 0  # 0 => scan all
    avoid_recent_days: int = 0

    # currency handling
    allow_eur: bool = False
    eur_to_gbp: float = 0.86

    # budgets (seconds)
    item_budget: float = 45.0
    run_budget: float = 0.0

    # optional cache ttl override
    cache_fail_ttl: Optional[int] = None

    # Optional knobs (safe to ignore if unused elsewhere)
    refresh_buy_price: bool = False
    scan_sleep_s: float = 0.0

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> "RunConfig":
        """
        Accept known historical argument styles and normalize to this dataclass.
        Unknown keys are rejected (to catch typos).
        """
        # --- normalize aliases ---
        if "max_buy_gbp" in kwargs and "max_buy" not in kwargs:
            kwargs["max_buy"] = kwargs.pop("max_buy_gbp")

        if "watchlist_target" in kwargs and "target" not in kwargs:
            kwargs["target"] = kwargs.pop("watchlist_target")

        if "verify_safety_cap" in kwargs and "safety_cap" not in kwargs:
            kwargs["safety_cap"] = kwargs.pop("verify_safety_cap")

        if "item_budget_s" in kwargs and "item_budget" not in kwargs:
            kwargs["item_budget"] = kwargs.pop("item_budget_s")

        if "run_budget_s" in kwargs and "run_budget" not in kwargs:
            kwargs["run_budget"] = kwargs.pop("run_budget_s")

        if "scan_limit_s" in kwargs and "scan_limit" not in kwargs:
            kwargs["scan_limit"] = kwargs.pop("scan_limit_s")

        if "fail_ttl" in kwargs and "cache_fail_ttl" not in kwargs:
            kwargs["cache_fail_ttl"] = kwargs.pop("fail_ttl")

        # root can be str or Path
        if "root" in kwargs and kwargs["root"] is not None and not isinstance(kwargs["root"], Path):
            kwargs["root"] = Path(str(kwargs["root"]))

        # bool parsing
        if "allow_eur" in kwargs:
            kwargs["allow_eur"] = _parse_bool(kwargs["allow_eur"], default=False)
        if "refresh_buy_price" in kwargs:
            kwargs["refresh_buy_price"] = _parse_bool(kwargs["refresh_buy_price"], default=False)

        # --- reject unknown keys ---
        allowed = set(cls.__dataclass_fields__.keys())
        unknown = [k for k in kwargs.keys() if k not in allowed]
        if unknown:
            raise TypeError(f"Unexpected RunConfig argument(s): {', '.join(unknown)}")

        cfg = cls(**kwargs)
        cfg._validate()
        return cfg

    # ---- compatibility properties (older code may read these) ----
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

        if self.verify_candidates < 0:
            raise ValueError("verify_candidates must be >= 0")
        if self.pages_per_source <= 0:
            raise ValueError("pages_per_source must be > 0")

        if self.verify_limit < 0:
            raise ValueError("verify_limit must be >= 0")
        if self.safety_cap <= 0:
            raise ValueError("safety_cap must be > 0")

        if self.scan_limit < 0:
            raise ValueError("scan_limit must be >= 0")
        if self.avoid_recent_days < 0:
            raise ValueError("avoid_recent_days must be >= 0")

        if self.item_budget <= 0:
            raise ValueError("item_budget must be > 0")
        if self.run_budget < 0:
            raise ValueError("run_budget must be >= 0")

        if self.allow_eur and self.eur_to_gbp <= 0:
            raise ValueError("eur_to_gbp must be > 0 when allow_eur=True")
        if self.scan_sleep_s < 0:
            raise ValueError("scan_sleep_s must be >= 0")
