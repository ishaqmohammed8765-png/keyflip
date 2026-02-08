from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Target:
    id: Optional[int]
    name: str
    query: str
    category_id: Optional[str] = None
    condition: Optional[str] = None
    max_buy_gbp: Optional[float] = None
    shipping_max_gbp: Optional[float] = None
    listing_type: str = "any"
    country: str = "UK"
    enabled: bool = True
    created_at: str = field(default_factory=_iso_now)

    @classmethod
    def from_row(cls, row: Any) -> "Target":
        return cls(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            category_id=row["category_id"],
            condition=row["condition"],
            max_buy_gbp=row["max_buy_gbp"],
            shipping_max_gbp=row["shipping_max_gbp"],
            listing_type=row["listing_type"],
            country=row["country"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class Listing:
    ebay_item_id: str
    target_id: int
    title: str
    url: str
    price_gbp: float
    shipping_gbp: float
    total_buy_gbp: float
    condition: Optional[str] = None
    seller_feedback_pct: Optional[float] = None
    seller_feedback_score: Optional[int] = None
    returns_accepted: Optional[bool] = None
    listing_type: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    image_url: Optional[str] = None
    raw_json: dict[str, Any] | None = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Any) -> "Listing":
        raw_json = row["raw_json"]
        if raw_json:
            try:
                raw_json = json.loads(raw_json)
            except json.JSONDecodeError:
                raw_json = {"raw": raw_json}
        return cls(
            ebay_item_id=row["ebay_item_id"],
            target_id=row["target_id"],
            title=row["title"],
            url=row["url"],
            price_gbp=row["price_gbp"],
            shipping_gbp=row["shipping_gbp"],
            total_buy_gbp=row["total_buy_gbp"],
            condition=row["condition"],
            seller_feedback_pct=row["seller_feedback_pct"],
            seller_feedback_score=row["seller_feedback_score"],
            returns_accepted=bool(row["returns_accepted"]) if row["returns_accepted"] is not None else None,
            listing_type=row["listing_type"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            location=row["location"],
            image_url=row["image_url"],
            raw_json=raw_json,
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )


@dataclass(slots=True)
class CompStats:
    comp_query: str
    sold_count: int
    median_sold_gbp: Optional[float]
    p25_sold_gbp: Optional[float]
    p75_sold_gbp: Optional[float]
    spread_gbp: Optional[float]
    computed_at: str

    @classmethod
    def from_row(cls, row: Any) -> "CompStats":
        return cls(
            comp_query=row["comp_query"],
            sold_count=row["sold_count"],
            median_sold_gbp=row["median_sold_gbp"],
            p25_sold_gbp=row["p25_sold_gbp"],
            p75_sold_gbp=row["p75_sold_gbp"],
            spread_gbp=row["spread_gbp"],
            computed_at=row["computed_at"],
        )


@dataclass(slots=True)
class Evaluation:
    resale_est_gbp: float
    ebay_fee_pct: float
    other_fees_gbp: float
    shipping_out_gbp: float
    buffer_gbp: float
    expected_profit_gbp: float
    roi: float
    confidence: float
    deal_score: float
    decision: str
    reasons: list[str]
    evaluated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Evaluation":
        reasons = []
        if row["reasons_json"]:
            try:
                reasons = json.loads(row["reasons_json"])
            except json.JSONDecodeError:
                reasons = [row["reasons_json"]]
        return cls(
            resale_est_gbp=row["resale_est_gbp"],
            ebay_fee_pct=row["ebay_fee_pct"],
            other_fees_gbp=row["other_fees_gbp"],
            shipping_out_gbp=row["shipping_out_gbp"],
            buffer_gbp=row["buffer_gbp"],
            expected_profit_gbp=row["expected_profit_gbp"],
            roi=row["roi"],
            confidence=row["confidence"],
            deal_score=row["deal_score"],
            decision=row["decision"],
            reasons=reasons,
            evaluated_at=row["evaluated_at"],
        )


@dataclass(slots=True)
class SoldComp:
    price_gbp: float
    title: str
    url: Optional[str] = None
