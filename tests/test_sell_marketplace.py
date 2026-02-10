from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.ebay_client import EbayClient, _parse_sell_marketplaces
from ebayflip.models import SoldComp


def test_search_sold_comps_uses_ebay_fallback_by_default(monkeypatch) -> None:
    client = EbayClient(RunSettings(marketplace="ebay", sell_marketplace="ebay"))
    expected = [SoldComp(price_gbp=120.0, title="AirPods Pro 2")]

    monkeypatch.setattr(client, "_search_sold_html", lambda _query: expected)

    comps = client.search_sold_comps("airpods pro 2")
    assert comps == expected


def test_parse_sell_marketplaces_supports_multi_value() -> None:
    values = _parse_sell_marketplaces("ebay, mercari, ebay, poshmark")
    assert values == ["ebay", "mercari", "poshmark"]


def test_parse_sell_marketplaces_drops_unsupported_sources() -> None:
    values = _parse_sell_marketplaces("craigslist,ebay,foo")
    assert values == ["ebay"]


def test_search_sold_comps_supports_multiple_sources(monkeypatch) -> None:
    client = EbayClient(RunSettings(marketplace="ebay", sell_marketplace="ebay,mercari"))
    monkeypatch.setattr(client, "_search_sold_html", lambda _query: [SoldComp(price_gbp=100.0, title="A")])
    monkeypatch.setattr(client, "_search_sold_mercari", lambda _query: [SoldComp(price_gbp=110.0, title="B")])

    comps = client.search_sold_comps("item")
    assert len(comps) == 2


def test_search_sold_comps_keeps_partial_results_when_one_source_fails(monkeypatch) -> None:
    client = EbayClient(RunSettings(marketplace="ebay", sell_marketplace="ebay,mercari"))
    monkeypatch.setattr(client, "_search_sold_html", lambda _query: [SoldComp(price_gbp=100.0, title="A")])

    def fail(_query: str):
        raise RuntimeError("temporary fail")

    monkeypatch.setattr(client, "_search_sold_mercari", fail)
    comps = client.search_sold_comps("item")
    assert len(comps) == 1
