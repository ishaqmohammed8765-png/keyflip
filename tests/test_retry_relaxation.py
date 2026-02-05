from __future__ import annotations

from unittest.mock import patch

from ebayflip.config import RunSettings
from ebayflip.ebay_client import EbayClient, SearchCriteria, SearchResult, _build_retry_steps
from ebayflip.models import Target


def test_build_retry_steps_includes_relaxation_stages() -> None:
    criteria = SearchCriteria(
        query='"iphone 14" 128gb black',
        category_id="9355",
        condition="3000",
        max_buy_gbp=250.0,
        shipping_max_gbp=10.0,
        listing_type="auction",
    )
    labels = [label for label, _ in _build_retry_steps(criteria)]
    assert labels[0] == "initial"
    assert "removed category filter" in labels
    assert "removed condition filter" in labels
    assert "removed listing type filter" in labels
    assert "removed price filters" in labels
    assert any(label.startswith("broadened query") for label in labels)


def test_search_active_retries_until_relaxed_stage_returns_results() -> None:
    client = EbayClient(RunSettings(request_cap=50), app_id=None)
    target = Target(
        id=1,
        name="iPhone",
        query='"iphone 14" 128gb black',
        category_id="9355",
        condition="3000",
        max_buy_gbp=250.0,
        shipping_max_gbp=10.0,
        listing_type="auction",
    )

    calls: list[SearchCriteria] = []

    def fake_search(criteria: SearchCriteria, _target: Target, diagnostics: list) -> SearchResult:
        calls.append(criteria)
        # return listings after several retry stages have been attempted
        if len(calls) >= 6:
            from ebayflip.models import Listing

            listing = Listing(
                ebay_item_id="1",
                target_id=1,
                title="Item",
                url="https://example.test/item",
                price_gbp=100.0,
                shipping_gbp=0.0,
                total_buy_gbp=100.0,
            )
            return SearchResult(
                listings=[listing],
                retry_report=[],
                diagnostics=diagnostics,
                rejection_counts={},
                raw_count=1,
                filtered_count=1,
                last_request_url="https://example.test",
                status="ok",
                blocked=None,
            )
        return SearchResult(
            listings=[],
            retry_report=[],
            diagnostics=diagnostics,
            rejection_counts={},
            raw_count=0,
            filtered_count=0,
            last_request_url="https://example.test",
            status="ok",
            blocked=None,
        )

    with patch.object(EbayClient, "_search_active_html", side_effect=fake_search):
        result = client._search_active_with_retry(target, mode="html")

    assert result.raw_count == 1
    assert len(calls) >= 6
    assert any(c.category_id is None for c in calls)
    assert any(c.condition is None for c in calls)
    assert any(c.listing_type == "any" for c in calls)
    assert any(c.max_buy_gbp is None and c.shipping_max_gbp is None for c in calls)
