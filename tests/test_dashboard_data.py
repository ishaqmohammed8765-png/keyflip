from __future__ import annotations

from datetime import datetime, timezone

from ebayflip.dashboard_data import (
    filter_items,
    items_to_csv_bytes,
    scan_age_seconds,
    sort_items,
    summarize_items,
)


def _items() -> list[dict]:
    return [
        {"decision": "ignore", "title": "A", "deal_score": 5, "expected_profit_gbp": -1, "reasons": []},
        {"decision": "maybe", "title": "B Phone", "deal_score": 20, "expected_profit_gbp": 10, "reasons": ["x"]},
        {"decision": "deal", "title": "C Phone", "deal_score": 50, "expected_profit_gbp": 25, "reasons": ["y"]},
    ]


def test_sort_items_prioritizes_deals_and_score() -> None:
    sorted_items = sort_items(_items())
    assert sorted_items[0]["decision"] == "deal"
    assert sorted_items[1]["decision"] == "maybe"


def test_filter_items_applies_all_filters() -> None:
    filtered = filter_items(_items(), decision="deal", search_term="phone", min_score=40, min_profit=20)
    assert len(filtered) == 1
    assert filtered[0]["title"] == "C Phone"


def test_summarize_items_returns_counts_and_profit() -> None:
    summary = summarize_items(_items())
    assert summary["deal_count"] == 1
    assert summary["maybe_count"] == 1
    assert summary["total_profit"] == 35


def test_items_to_csv_contains_rows() -> None:
    text = items_to_csv_bytes(_items()).decode("utf-8")
    assert "decision,title,url" in text
    assert "deal,C Phone" in text


def test_scan_age_seconds_handles_iso() -> None:
    payload = {"generated_at": datetime.now(timezone.utc).isoformat()}
    age = scan_age_seconds(payload)
    assert age is not None
    assert age >= 0
