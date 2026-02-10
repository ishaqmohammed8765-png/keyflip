from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.models import CompStats, Listing
from ebayflip.scoring import evaluate_listing


def test_missing_shipping_adds_risk_buffer_and_reduces_confidence() -> None:
    settings = RunSettings(
        missing_shipping_penalty_gbp=5.0,
        missing_shipping_confidence_penalty=0.10,
        min_profit_gbp=0.0,
        min_roi=0.0,
        min_confidence=0.0,
    )
    listing = Listing(
        ebay_item_id="1",
        target_id=1,
        title="Test Item",
        url="https://example.test/1",
        price_gbp=100.0,
        shipping_gbp=0.0,
        total_buy_gbp=100.0,
        raw_json={"shipping_missing": True},
    )
    comps = CompStats(
        comp_query="test item",
        sold_count=10,
        median_sold_gbp=180.0,
        p25_sold_gbp=170.0,
        p75_sold_gbp=190.0,
        spread_gbp=20.0,
        computed_at="2026-01-01T00:00:00+00:00",
    )
    evaluation = evaluate_listing(listing, comps, settings)

    assert evaluation.buffer_gbp >= settings.buffer_fixed_gbp + settings.missing_shipping_penalty_gbp
    assert evaluation.confidence < 1.0
    assert any("Missing inbound shipping" in reason for reason in evaluation.reasons)

