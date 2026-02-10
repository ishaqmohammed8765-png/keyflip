from __future__ import annotations

from typing import Any

from ebayflip.config import RunSettings
from ebayflip.costs import other_fees_gbp_for_resale


def max_total_buy_for_target_profit(
    *,
    resale_est_gbp: float,
    target_profit_gbp: float,
    settings: RunSettings,
) -> float:
    other_fees = other_fees_gbp_for_resale(resale_est_gbp, settings)
    numerator = (
        resale_est_gbp * (1 - settings.ebay_fee_pct)
        - other_fees
        - settings.shipping_out_gbp
        - settings.buffer_fixed_gbp
        - target_profit_gbp
    )
    denominator = 1 + settings.buffer_pct_of_buy
    if denominator <= 0:
        return 0.0
    return max(0.0, numerator / denominator)


def break_even_total_buy_gbp(*, resale_est_gbp: float, settings: RunSettings) -> float:
    return max_total_buy_for_target_profit(
        resale_est_gbp=resale_est_gbp,
        target_profit_gbp=0.0,
        settings=settings,
    )


def offer_price_from_max_buy(max_buy_gbp: float, *, negotiation_discount: float = 0.1) -> float:
    discount = min(max(negotiation_discount, 0.0), 0.5)
    return max(0.0, max_buy_gbp * (1 - discount))


def risk_band(*, confidence: float, roi: float) -> str:
    if confidence >= 0.7 and roi >= 0.25:
        return "low"
    if confidence >= 0.5 and roi >= 0.15:
        return "medium"
    return "high"


def flip_grade(*, score: float, confidence: float, roi: float) -> str:
    if score >= 60 and confidence >= 0.65 and roi >= 0.25:
        return "A"
    if score >= 40 and confidence >= 0.50 and roi >= 0.15:
        return "B"
    if score >= 20 and confidence >= 0.35 and roi >= 0.10:
        return "C"
    return "D"


def enrich_item(item: dict[str, Any], settings: RunSettings, *, target_profit_gbp: float) -> dict[str, Any]:
    row = dict(item)
    resale_est = float(row.get("resale_est_gbp") or 0.0)
    buy_total = float(row.get("total_buy_gbp") or 0.0)
    profit = float(row.get("expected_profit_gbp") or 0.0)
    roi = float(row.get("roi") or 0.0)
    confidence = float(row.get("confidence") or 0.0)
    score = float(row.get("deal_score") or 0.0)

    max_buy = max_total_buy_for_target_profit(
        resale_est_gbp=resale_est,
        target_profit_gbp=target_profit_gbp,
        settings=settings,
    )
    break_even = break_even_total_buy_gbp(resale_est_gbp=resale_est, settings=settings)
    offer = offer_price_from_max_buy(max_buy)
    edge = max_buy - buy_total

    row["max_total_buy_target_gbp"] = max_buy
    row["break_even_buy_gbp"] = break_even
    row["suggested_offer_gbp"] = offer
    row["buy_edge_gbp"] = edge
    row["risk_band"] = risk_band(confidence=confidence, roi=roi)
    row["flip_grade"] = flip_grade(score=score, confidence=confidence, roi=roi)
    row["is_actionable"] = edge > 0 and profit > 0 and confidence >= settings.min_confidence
    return row


def enrich_items(items: list[dict[str, Any]], settings: RunSettings, *, target_profit_gbp: float) -> list[dict[str, Any]]:
    return [enrich_item(item, settings, target_profit_gbp=target_profit_gbp) for item in items]


def _portfolio_priority(item: dict[str, Any]) -> float:
    buy = float(item.get("total_buy_gbp") or 0.0)
    profit = float(item.get("expected_profit_gbp") or 0.0)
    confidence = float(item.get("confidence") or 0.0)
    edge = float(item.get("buy_edge_gbp") or 0.0)
    capital_efficiency = profit / buy if buy > 0 else 0.0
    return (profit * confidence) + (capital_efficiency * 20) + (edge * 0.15)


def plan_portfolio(
    items: list[dict[str, Any]],
    *,
    budget_gbp: float,
    max_items: int = 5,
) -> list[dict[str, Any]]:
    if budget_gbp <= 0 or max_items <= 0:
        return []
    candidates = [
        item
        for item in items
        if (item.get("is_actionable") or False)
        and (item.get("expected_profit_gbp") or 0) > 0
        and (item.get("total_buy_gbp") or 0) > 0
    ]
    ranked = sorted(candidates, key=_portfolio_priority, reverse=True)
    selected: list[dict[str, Any]] = []
    remaining = float(budget_gbp)
    for item in ranked:
        buy = float(item.get("total_buy_gbp") or 0.0)
        if buy <= remaining:
            selected.append(item)
            remaining -= buy
        if len(selected) >= max_items:
            break
    return selected
