from __future__ import annotations

from ebayflip.config import RunSettings


def test_run_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MARKETPLACE", "ebay")
    monkeypatch.setenv("REQUEST_CAP", "77")
    monkeypatch.setenv("CURRENCY_WHITELIST", "GBP,USD,EUR")
    settings = RunSettings.from_env()
    assert settings.marketplace == "ebay"
    assert settings.request_cap == 77
    assert settings.currency_whitelist == ("GBP", "USD", "EUR")


def test_default_craigslist_site_uses_locale(monkeypatch) -> None:
    monkeypatch.delenv("CRAIGSLIST_SITE", raising=False)
    monkeypatch.setenv("LOCALE", "en_US")
    settings = RunSettings.from_env(marketplace="craigslist")
    assert settings.craigslist_site == "sfbay"
