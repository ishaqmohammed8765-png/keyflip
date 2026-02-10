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


def test_search_sold_comps_uses_ebay_api_when_in_multi_source(monkeypatch) -> None:
    client = EbayClient(RunSettings(marketplace="ebay", sell_marketplace="ebay,mercari"))
    monkeypatch.setattr(client.api_provider, "enabled", lambda: True)
    monkeypatch.setattr(client.api_provider, "search_sold_comps", lambda _query: [SoldComp(price_gbp=99.0, title="API")])
    monkeypatch.setattr(client, "_search_sold_html", lambda _query: [SoldComp(price_gbp=88.0, title="HTML")])
    monkeypatch.setattr(client, "_search_sold_mercari", lambda _query: [])

    comps = client.search_sold_comps("item")
    titles = [comp.title for comp in comps]
    assert "API" in titles
    assert "HTML" not in titles


def test_search_sold_comps_keeps_partial_results_when_one_source_fails(monkeypatch) -> None:
    client = EbayClient(RunSettings(marketplace="ebay", sell_marketplace="ebay,mercari"))
    monkeypatch.setattr(client, "_search_sold_html", lambda _query: [SoldComp(price_gbp=100.0, title="A")])

    def fail(_query: str):
        raise RuntimeError("temporary fail")

    monkeypatch.setattr(client, "_search_sold_mercari", fail)
    comps = client.search_sold_comps("item")
    assert len(comps) == 1


def test_search_sold_comps_falls_back_to_active_when_no_sold(monkeypatch) -> None:
    client = EbayClient(RunSettings(marketplace="ebay", sell_marketplace="poshmark"))
    monkeypatch.setattr(client, "_search_sold_poshmark", lambda _query: [])
    monkeypatch.setattr(client, "_active_comp_fallback_marketplaces", lambda _sources: ["poshmark"])
    monkeypatch.setattr(
        client,
        "_search_active_comps_from_marketplace",
        lambda _query, _source: [SoldComp(price_gbp=125.0, title="AirPods Pro 2")],
    )
    comps = client.search_sold_comps("airpods pro 2")
    assert len(comps) == 1
    assert comps[0].price_gbp == 125.0
