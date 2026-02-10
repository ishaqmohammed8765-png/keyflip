from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.filtering import filter_listings
from ebayflip.models import Listing, Target


def _target() -> Target:
    return Target(id=1, name="switch", query="switch")


def test_delivery_only_blocks_craigslist_without_delivery_hint() -> None:
    listing = Listing(
        ebay_item_id="cl-1001",
        target_id=1,
        title="Nintendo Switch OLED",
        url="https://sfbay.craigslist.org/abc/1001.html",
        price_gbp=180.0,
        shipping_gbp=0.0,
        total_buy_gbp=180.0,
        raw_json={"source": "craigslist_html", "delivery_hint": False},
    )
    outcome = filter_listings([listing], _target(), RunSettings(marketplace="craigslist", delivery_only=True))
    assert len(outcome.listings) == 0
    assert outcome.rejection_counts["no delivery available"] == 1


def test_delivery_only_allows_craigslist_with_delivery_hint() -> None:
    listing = Listing(
        ebay_item_id="cl-1002",
        target_id=1,
        title="Nintendo Switch OLED with delivery",
        url="https://sfbay.craigslist.org/abc/1002.html",
        price_gbp=180.0,
        shipping_gbp=0.0,
        total_buy_gbp=180.0,
        raw_json={"source": "craigslist_html", "delivery_hint": True},
    )
    outcome = filter_listings([listing], _target(), RunSettings(marketplace="craigslist", delivery_only=True))
    assert len(outcome.listings) == 1
    assert outcome.rejection_counts["no delivery available"] == 0


def test_delivery_only_allows_mercari_with_delivery_shipping_type() -> None:
    listing = Listing(
        ebay_item_id="merc-1001",
        target_id=1,
        title="Nintendo Switch OLED",
        url="https://www.mercari.com/us/item/m123/",
        price_gbp=180.0,
        shipping_gbp=0.0,
        total_buy_gbp=180.0,
        raw_json={"source": "mercari_html", "shipping_type": "delivery"},
    )
    outcome = filter_listings([listing], _target(), RunSettings(marketplace="mercari", delivery_only=True))
    assert len(outcome.listings) == 1
    assert outcome.rejection_counts["no delivery available"] == 0
