from __future__ import annotations

from typing import Optional

from ebayflip.config import RunSettings
from ebayflip.ebay_client import EbayClient, SearchResult, fetch_ebay_search
from ebayflip.models import Target


def _empty_result() -> SearchResult:
    return SearchResult(
        listings=[],
        retry_report=[],
        diagnostics=[],
        rejection_counts={},
        raw_count=0,
        filtered_count=0,
        last_request_url=None,
        status="ok",
        blocked=None,
    )


def test_search_active_with_retry_uses_all_retry_steps(monkeypatch) -> None:
    client = EbayClient(RunSettings())
    target = Target(
        id=1,
        name="Nintendo Switch",
        query="Nintendo Switch OLED 64GB White",
        category_id="123",
        condition="3000",
        max_buy_gbp=250.0,
        shipping_max_gbp=10.0,
        listing_type="owner",
    )

    attempts: list[tuple[str, Optional[str], Optional[str], Optional[float], Optional[float]]] = []

    def fake_search(criteria, _target, _diagnostics):
        attempts.append(
            (
                criteria.query,
                criteria.category_id,
                criteria.condition,
                criteria.max_buy_gbp,
                criteria.shipping_max_gbp,
            )
        )
        return _empty_result()

    monkeypatch.setattr(client, "_search_active_html", fake_search)

    result = client._search_active_with_retry(target, mode="html")

    assert result.listings == []
    assert len(attempts) > 1


def test_fetch_ebay_search_uses_client_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_html_params(criteria, page, settings=None):
        captured["settings"] = settings
        return {"query": criteria.query}

    class DummyResponse:
        text = "<html></html>"
        url = "https://example.test/search"

    def fake_fetch_html(_client, _url, **_kwargs):
        return DummyResponse(), False

    monkeypatch.setattr("ebayflip.ebay_client._build_html_params", fake_build_html_params)
    monkeypatch.setattr("ebayflip.ebay_client.fetch_html", fake_fetch_html)
    monkeypatch.setattr("ebayflip.ebay_client.parse_html", lambda *_args, **_kwargs: ([], {"card_count": 0, "title_count": 0, "link_count": 0, "price_count": 0}))
    monkeypatch.setattr("ebayflip.ebay_client._parse_json_ld_listings", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("ebayflip.ebay_client._parse_initial_state_listings", lambda *_args, **_kwargs: [])

    items = fetch_ebay_search("iphone 14")

    assert items == []
    assert captured["settings"] is not None
