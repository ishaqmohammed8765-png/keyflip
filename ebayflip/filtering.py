from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from ebayflip.config import RunSettings
from ebayflip.models import Listing, Target

CONDITION_CODE_MAP = {
    "1000": "new",
    "1500": "open box",
    "2000": "manufacturer refurbished",
    "2500": "seller refurbished",
    "3000": "used",
    "7000": "for parts or not working",
}

REJECTION_REASONS = (
    "over max_buy",
    "missing/invalid price",
    "wrong condition",
    "blocked keywords",
    "seller risk thresholds",
    "missing shipping price",
)


@dataclass(slots=True)
class FilterOutcome:
    listings: list[Listing]
    rejection_counts: dict[str, int]


def filter_listings(
    listings: Iterable[Listing],
    target: Target,
    settings: RunSettings,
) -> FilterOutcome:
    kept: list[Listing] = []
    counts: Counter[str] = Counter()
    blocked = [keyword.lower() for keyword in settings.blocked_keywords if keyword.strip()]

    for listing in listings:
        reasons: list[str] = []
        if listing.price_gbp <= 0 or listing.total_buy_gbp <= 0:
            reasons.append("missing/invalid price")
        if target.max_buy_gbp is not None and listing.total_buy_gbp > target.max_buy_gbp:
            reasons.append("over max_buy")
        if target.shipping_max_gbp is not None and listing.shipping_gbp > target.shipping_max_gbp:
            reasons.append("over max_buy")
        if target.condition:
            if listing.condition:
                if not _condition_matches(listing.condition, target.condition):
                    reasons.append("wrong condition")
            else:
                reasons.append("wrong condition")
        if blocked and listing.title:
            title = listing.title.lower()
            if any(keyword in title for keyword in blocked):
                reasons.append("blocked keywords")
        if _seller_fails_thresholds(listing, settings):
            reasons.append("seller risk thresholds")
        if listing.raw_json and listing.raw_json.get("shipping_missing") and not settings.allow_missing_shipping_price:
            reasons.append("missing shipping price")

        if reasons:
            for reason in reasons:
                counts[reason] += 1
            continue
        kept.append(listing)

    rejection_counts = {reason: counts.get(reason, 0) for reason in REJECTION_REASONS}
    return FilterOutcome(listings=kept, rejection_counts=rejection_counts)


def _condition_matches(listing_condition: str, target_condition: str) -> bool:
    expected = CONDITION_CODE_MAP.get(target_condition, target_condition).lower()
    listing_value = listing_condition.lower()
    return expected in listing_value


def _seller_fails_thresholds(listing: Listing, settings: RunSettings) -> bool:
    if settings.min_seller_feedback_pct is not None and listing.seller_feedback_pct is not None:
        if listing.seller_feedback_pct < settings.min_seller_feedback_pct:
            return True
    if settings.min_seller_feedback_score is not None and listing.seller_feedback_score is not None:
        if listing.seller_feedback_score < settings.min_seller_feedback_score:
            return True
    return False
