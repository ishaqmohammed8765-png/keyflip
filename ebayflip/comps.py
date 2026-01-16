from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Iterable

from ebayflip.models import CompStats, SoldComp


def compute_comp_stats(comp_query: str, comps: Iterable[SoldComp]) -> CompStats:
    prices = sorted([comp.price_gbp for comp in comps if comp.price_gbp > 0])
    sold_count = len(prices)
    if sold_count == 0:
        return CompStats(
            comp_query=comp_query,
            sold_count=0,
            median_sold_gbp=None,
            p25_sold_gbp=None,
            p75_sold_gbp=None,
            spread_gbp=None,
            computed_at=datetime.utcnow().isoformat(),
        )
    median_val = float(median(prices))
    p25 = prices[int(0.25 * (sold_count - 1))]
    p75 = prices[int(0.75 * (sold_count - 1))]
    spread = p75 - p25
    return CompStats(
        comp_query=comp_query,
        sold_count=sold_count,
        median_sold_gbp=median_val,
        p25_sold_gbp=p25,
        p75_sold_gbp=p75,
        spread_gbp=spread,
        computed_at=datetime.utcnow().isoformat(),
    )
