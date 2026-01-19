import os

import pytest

from ebayflip.config import RunSettings
from ebayflip.ebay_client import EbayClient, build_url, fetch_with_playwright, parse_html
from ebayflip.models import Target


@pytest.mark.integration
def test_playwright_search_returns_results() -> None:
    if os.getenv("EBAY_RUN_PLAYWRIGHT_TESTS") != "1":
        pytest.skip("Set EBAY_RUN_PLAYWRIGHT_TESTS=1 to run live Playwright test.")
    settings = RunSettings()
    client = EbayClient(settings)
    url = build_url("iphone 14")
    html = fetch_with_playwright(url, client.session.headers)
    assert html, "Expected Playwright HTML response"
    target = Target(id=0, name="iphone 14", query="iphone 14")
    listings, _ = parse_html(html, target, client)
    assert listings, "Expected at least one listing"
    assert any(listing.price_gbp > 0 and listing.url for listing in listings)
