from __future__ import annotations

from dataclasses import dataclass


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


def build_watchlist(config: RunConfig, output_path) -> int:
    """
    Build a watchlist using Fanatical scraping logic (placeholder).
    Return: number of items written.
    """
    # Keep prints for now (since you said don't change other files).
    # In your real implementation, you'd use:
    # - config.max_buy
    # - config.pages_per_source
    # - config.verify_candidates
    # - config.effective_verify_limit()
    # - config.target
    print("Building watchlist with:", config)
    return 0


def scan_watchlist(config: RunConfig, watchlist_path, scans_path, passes_path, db_path, fail_ttl):
    """
    Scan watchlist using Eneba logic (placeholder).
    """
    # In your real implementation, you'd use:
    # - config.scan_limit (0 = unlimited)
    # - config.item_budget / config.run_budget for time limits
    # - config.allow_eur / eur_to_gbp for currency handling
    print("Scanning watchlist using:", config)
    return
