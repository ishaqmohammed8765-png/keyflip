from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.ebay_client import BlockedInfo, EbayClient, SearchResult
from ebayflip.models import Listing, Target


def test_search_active_listings_falls_back_to_mercari_when_ebay_blocked(monkeypatch) -> None:
    client = EbayClient(RunSettings(marketplace="ebay"))
    target = Target(id=1, name="Nintendo Switch", query="Nintendo Switch OLED")

    blocked = SearchResult(
        listings=[],
        retry_report=["retry: broaden keywords"],
        diagnostics=[],
        rejection_counts={},
        raw_count=0,
        filtered_count=0,
        last_request_url="https://www.ebay.co.uk/sch/i.html",
        status="blocked",
        blocked=BlockedInfo(
            status="blocked",
            reason="splashui_challenge",
            url="https://www.ebay.co.uk/splashui/challenge",
            debug_artifacts=[],
            message="blocked",
            detail="challenge",
        ),
    )
    fallback_listing = Listing(
        ebay_item_id="mercari-1",
        target_id=1,
        title="Nintendo Switch OLED",
        url="https://www.mercari.com/us/item/m123456789/",
        price_gbp=150.0,
        shipping_gbp=0.0,
        total_buy_gbp=150.0,
        listing_type="fixed",
        raw_json={"source": "mercari_html"},
    )
    fallback_ok = SearchResult(
        listings=[fallback_listing],
        retry_report=[],
        diagnostics=[],
        rejection_counts={},
        raw_count=1,
        filtered_count=1,
        last_request_url=fallback_listing.url,
        status="ok",
        blocked=None,
    )

    def fake_search_with_retry(self, _target, mode):  # noqa: ANN001
        if self.settings.marketplace == "ebay":
            return blocked
        return fallback_ok

    monkeypatch.setattr(EbayClient, "_search_active_with_retry", fake_search_with_retry)
    monkeypatch.setenv("BUY_BLOCKED_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("BUY_BLOCKED_FALLBACK_MARKETPLACE", "mercari")

    result = client.search_active_listings(target)

    assert result.status == "ok"
    assert len(result.listings) == 1
    assert "fallback: buy marketplace switched from ebay to mercari due to anti-bot challenge" in result.retry_report
    assert result.listings[0].raw_json.get("source") == "mercari_html"


def test_blocked_buy_fallback_defaults_to_craigslist_for_uk_locale(monkeypatch) -> None:
    monkeypatch.delenv("BUY_BLOCKED_FALLBACK_MARKETPLACE", raising=False)
    monkeypatch.setenv("LOCALE", "en_GB")
    client = EbayClient(RunSettings(marketplace="ebay"))
    assert client._blocked_buy_fallback_marketplaces() == ["craigslist"]
