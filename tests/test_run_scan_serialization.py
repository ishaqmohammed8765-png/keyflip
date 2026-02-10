from __future__ import annotations

import json

from ebayflip.config import RunSettings
from scanner.run_scan import _serialize_items


def test_serialize_items_includes_source_and_marketplaces() -> None:
    rows = [
        {
            "listing_id": 1,
            "title": "Nintendo Switch OLED",
            "url": "https://example.test/item",
            "total_buy_gbp": 180.0,
            "resale_est_gbp": 230.0,
            "expected_profit_gbp": 24.0,
            "roi": 0.13,
            "confidence": 0.6,
            "deal_score": 55.0,
            "decision": "deal",
            "reasons_json": json.dumps(["Strong comps"]),
            "evaluated_at": "2026-01-01T00:00:00+00:00",
            "image_url": None,
            "location": "SF Bay Area",
            "listing_type": "fixed",
            "raw_json": json.dumps({"source": "craigslist_html"}),
        }
    ]
    settings = RunSettings(marketplace="ebay", sell_marketplace="ebay")
    items = _serialize_items(rows, settings=settings)

    assert len(items) == 1
    assert items[0]["source"] == "craigslist_html"
    assert items[0]["buy_marketplace"] == "ebay"
    assert items[0]["sell_marketplace"] == "ebay"
