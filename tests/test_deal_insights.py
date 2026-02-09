from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.deal_insights import (
    break_even_total_buy_gbp,
    enrich_item,
    max_total_buy_for_target_profit,
    offer_price_from_max_buy,
    plan_portfolio,
)


def test_max_total_buy_for_target_profit_decreases_as_target_profit_increases() -> None:
    settings = RunSettings()
    low_target = max_total_buy_for_target_profit(
        resale_est_gbp=200.0,
        target_profit_gbp=10.0,
        settings=settings,
    )
    high_target = max_total_buy_for_target_profit(
        resale_est_gbp=200.0,
        target_profit_gbp=30.0,
        settings=settings,
    )
    assert low_target > high_target


def test_break_even_higher_than_target_profit_buy() -> None:
    settings = RunSettings()
    break_even = break_even_total_buy_gbp(resale_est_gbp=250.0, settings=settings)
    target = max_total_buy_for_target_profit(
        resale_est_gbp=250.0,
        target_profit_gbp=25.0,
        settings=settings,
    )
    assert break_even > target


def test_offer_price_applies_discount() -> None:
    assert offer_price_from_max_buy(100.0, negotiation_discount=0.1) == 90.0


def test_enrich_item_adds_actionable_fields() -> None:
    settings = RunSettings(min_confidence=0.4)
    item = {
        "resale_est_gbp": 250.0,
        "total_buy_gbp": 120.0,
        "expected_profit_gbp": 40.0,
        "roi": 0.33,
        "confidence": 0.7,
        "deal_score": 60.0,
    }
    enriched = enrich_item(item, settings, target_profit_gbp=20.0)
    assert "max_total_buy_target_gbp" in enriched
    assert "buy_edge_gbp" in enriched
    assert enriched["flip_grade"] in {"A", "B", "C", "D"}
    assert enriched["risk_band"] in {"low", "medium", "high"}


def test_plan_portfolio_respects_budget_and_count() -> None:
    items = [
        {"title": "A", "is_actionable": True, "expected_profit_gbp": 30, "confidence": 0.7, "total_buy_gbp": 100, "buy_edge_gbp": 20},
        {"title": "B", "is_actionable": True, "expected_profit_gbp": 25, "confidence": 0.8, "total_buy_gbp": 90, "buy_edge_gbp": 15},
        {"title": "C", "is_actionable": True, "expected_profit_gbp": 8, "confidence": 0.6, "total_buy_gbp": 40, "buy_edge_gbp": 5},
    ]
    picks = plan_portfolio(items, budget_gbp=150, max_items=2)
    assert len(picks) <= 2
    assert sum(p["total_buy_gbp"] for p in picks) <= 150
