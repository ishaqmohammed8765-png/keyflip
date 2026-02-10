from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.filtering import filter_listings
from ebayflip.models import Listing, Target


def test_filter_rejects_unrelated_listing_title() -> None:
    target = Target(id=1, name="iPad Pro 11", query="ipad pro 11")
    listing = Listing(
        ebay_item_id="cl-2001",
        target_id=1,
        title="Coolbox Entertainment Coolers, FREE Shipping",
        url="https://sfbay.craigslist.org/sby/grq/d/brighton-coolbox-entertainment-coolers/7910382596.html",
        price_gbp=36.0,
        shipping_gbp=0.0,
        total_buy_gbp=36.0,
        raw_json={"source": "craigslist_html", "delivery_hint": True},
    )
    outcome = filter_listings([listing], target, RunSettings(marketplace="craigslist", delivery_only=True))
    assert len(outcome.listings) == 0
    assert outcome.rejection_counts["weak target match"] == 1


def test_filter_keeps_related_listing_title() -> None:
    target = Target(id=1, name="Apple iPhone 15 Pro", query="apple iphone 15 pro")
    listing = Listing(
        ebay_item_id="cl-2002",
        target_id=1,
        title="iPhone 15 Pro unlocked with delivery",
        url="https://sfbay.craigslist.org/sfc/mob/d/san-francisco-iphone-15-pro/7910999999.html",
        price_gbp=300.0,
        shipping_gbp=0.0,
        total_buy_gbp=300.0,
        raw_json={"source": "craigslist_html", "delivery_hint": True},
    )
    outcome = filter_listings([listing], target, RunSettings(marketplace="craigslist", delivery_only=True))
    assert len(outcome.listings) == 1
    assert outcome.rejection_counts["weak target match"] == 0
