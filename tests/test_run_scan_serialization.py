from __future__ import annotations

import json
from datetime import datetime, timezone

from ebayflip.config import RunSettings
from scanner.run_scan import _filter_rows_since, _serialize_items


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


def test_filter_rows_since_keeps_current_cycle_rows() -> None:
    start = datetime(2026, 2, 10, 18, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"evaluated_at": "2026-02-10T17:59:59+00:00", "listing_id": 1},
        {"evaluated_at": "2026-02-10T18:00:00+00:00", "listing_id": 2},
        {"evaluated_at": "2026-02-10T18:00:05+00:00", "listing_id": 3},
    ]
    filtered = _filter_rows_since(rows, since=start)
    assert [row["listing_id"] for row in filtered] == [2, 3]


def test_serialize_items_deduplicates_to_latest_evaluation() -> None:
    rows = [
        {
            "listing_id": 27,
            "title": "Phone",
            "url": "https://example.test/item/27",
            "deal_score": 20.0,
            "decision": "maybe",
            "evaluated_at": "2026-02-10T18:00:00+00:00",
            "location": "SF",
            "listing_type": "fixed",
            "raw_json": "{}",
        },
        {
            "listing_id": 27,
            "title": "Phone",
            "url": "https://example.test/item/27",
            "deal_score": 35.0,
            "decision": "deal",
            "evaluated_at": "2026-02-10T18:00:03+00:00",
            "location": "SF",
            "listing_type": "fixed",
            "raw_json": "{}",
        },
    ]
    settings = RunSettings(marketplace="ebay", sell_marketplace="ebay")

    items = _serialize_items(rows, settings=settings)

    assert len(items) == 1
    assert items[0]["decision"] == "deal"
    assert items[0]["deal_score"] == 35.0
    assert items[0]["evaluated_at"] == "2026-02-10T18:00:03+00:00"
