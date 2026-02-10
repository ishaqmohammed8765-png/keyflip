from __future__ import annotations

from ebayflip.ebay_client import _looks_like_listing_link, _looks_like_listing_title


def test_poshmark_listing_link_filter() -> None:
    assert _looks_like_listing_link("poshmark", "https://poshmark.com/listing/Sony-Camera-123") is True
    assert _looks_like_listing_link("poshmark", "https://poshmark.com/category/Electronics?size=OS") is False
    assert _looks_like_listing_link("poshmark", "https://poshmark.com/closet/seller123") is False


def test_mercari_listing_link_filter() -> None:
    assert _looks_like_listing_link("mercari", "https://www.mercari.com/us/item/m123456/") is True
    assert _looks_like_listing_link("mercari", "https://www.mercari.com/search/?keyword=switch") is False


def test_listing_title_filter_removes_noise() -> None:
    assert _looks_like_listing_title("Sony WH-1000XM5 Headphones") is True
    assert _looks_like_listing_title("Size: OS") is False
    assert _looks_like_listing_title("Just Shared") is False
    assert _looks_like_listing_title("7") is False

