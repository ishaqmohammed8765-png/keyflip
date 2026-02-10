from __future__ import annotations

from ebayflip.config import RunSettings


def test_run_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MARKETPLACE", "ebay")
    monkeypatch.setenv("SELL_MARKETPLACE", "ebay,mercari,poshmark")
    monkeypatch.setenv("REQUEST_CAP", "77")
    monkeypatch.setenv("MIN_PROFIT_GBP", "12")
    monkeypatch.setenv("CURRENCY_WHITELIST", "GBP,USD,EUR")
    settings = RunSettings.from_env()
    assert settings.marketplace == "ebay"
    assert settings.sell_marketplace == "ebay,mercari,poshmark"
    assert settings.request_cap == 77
    assert settings.min_profit_gbp == 12
    assert settings.currency_whitelist == ("GBP", "USD", "EUR")


def test_default_craigslist_site_uses_locale(monkeypatch) -> None:
    monkeypatch.delenv("CRAIGSLIST_SITE", raising=False)
    monkeypatch.setenv("LOCALE", "en_US")
    settings = RunSettings.from_env(marketplace="craigslist")
    assert settings.craigslist_site == "sfbay"


def test_default_ebay_site_domain_uses_locale(monkeypatch) -> None:
    monkeypatch.delenv("EBAY_SITE_DOMAIN", raising=False)
    monkeypatch.setenv("LOCALE", "en_US")
    settings = RunSettings.from_env()
    assert settings.ebay_site_domain == "www.ebay.com"


def test_ebay_site_domain_override(monkeypatch) -> None:
    monkeypatch.setenv("EBAY_SITE_DOMAIN", "www.ebay.co.uk")
    settings = RunSettings.from_env()
    assert settings.ebay_site_domain == "www.ebay.co.uk"


def test_delivery_only_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv("DELIVERY_ONLY", raising=False)
    settings = RunSettings.from_env()
    assert settings.delivery_only is True


def test_marketplace_craigslist_is_supported(monkeypatch) -> None:
    monkeypatch.setenv("MARKETPLACE", "craigslist")
    settings = RunSettings.from_env()
    assert settings.marketplace == "craigslist"


def test_sanitize_sell_marketplaces(monkeypatch) -> None:
    monkeypatch.setenv("SELL_MARKETPLACE", "craigslist,ebay,poshmark,foo")
    settings = RunSettings.from_env()
    assert settings.sell_marketplace == "ebay,poshmark"
