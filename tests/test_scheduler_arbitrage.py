from __future__ import annotations

from ebayflip.config import AlertSettings, AppConfig, RunSettings
from ebayflip.ebay_client import SearchResult
from ebayflip.models import Listing, SoldComp, Target
from ebayflip.scheduler import run_scan


class DummyClient:
    def __init__(self, listing: Listing) -> None:
        self._listing = listing
        self.request_count = 0
        self.request_cap_reached = False

    def search_active_listings(self, _target: Target) -> SearchResult:
        return SearchResult(
            listings=[self._listing],
            retry_report=[],
            diagnostics=[],
            rejection_counts={},
            raw_count=1,
            filtered_count=1,
            last_request_url=self._listing.url,
            status="ok",
            blocked=None,
        )

    def search_sold_comps(self, _comp_query: str) -> list[SoldComp]:
        return [
            SoldComp(price_gbp=220.0, title="Nintendo Switch OLED"),
            SoldComp(price_gbp=230.0, title="Nintendo Switch OLED"),
        ]


class MutableCompsClient(DummyClient):
    def __init__(self, listing: Listing, comps: list[SoldComp]) -> None:
        super().__init__(listing)
        self.comps = comps

    def search_sold_comps(self, _comp_query: str) -> list[SoldComp]:
        return list(self.comps)


def test_run_scan_returns_arbitrage_summary_and_opportunities(tmp_path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    settings = RunSettings(
        marketplace="ebay",
        sell_marketplace="ebay",
        min_profit_gbp=1.0,
        min_roi=0.0,
        min_confidence=0.0,
    )
    config = AppConfig(
        db_path=str(db_path),
        run=settings,
        alerts=AlertSettings(discord_webhook_url=None),
    )
    listing = Listing(
        ebay_item_id="cl-1",
        target_id=1,
        title="Nintendo Switch OLED",
        url="https://sfbay.craigslist.org/abc/1234567890.html",
        price_gbp=140.0,
        shipping_gbp=0.0,
        total_buy_gbp=140.0,
        listing_type="fixed",
        raw_json={"source": "craigslist_html"},
    )
    client = DummyClient(listing=listing)

    from ebayflip.db import add_target, init_db

    init_db(config.db_path)
    add_target(
        config.db_path,
        Target(id=None, name="Nintendo Switch OLED", query="Nintendo Switch OLED"),
    )
    summary = run_scan(config, client)  # type: ignore[arg-type]

    assert summary.buy_marketplace == "ebay"
    assert summary.sell_marketplace == "ebay"
    assert summary.scanned_targets == 1
    assert summary.evaluated == 1
    assert len(summary.opportunities) == 1
    assert summary.opportunities[0].sell_marketplace == "ebay"
    assert summary.scanned_listings[0].source == "craigslist_html"


def test_run_scan_refreshes_empty_comps_between_cycles(tmp_path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    settings = RunSettings(
        marketplace="ebay",
        sell_marketplace="ebay",
        min_profit_gbp=1.0,
        min_roi=0.0,
        min_confidence=0.0,
        comps_ttl_hours=48,
    )
    config = AppConfig(
        db_path=str(db_path),
        run=settings,
        alerts=AlertSettings(discord_webhook_url=None),
    )
    listing = Listing(
        ebay_item_id="cl-2",
        target_id=1,
        title="Nintendo Switch OLED",
        url="https://sfbay.craigslist.org/abc/1234567891.html",
        price_gbp=140.0,
        shipping_gbp=0.0,
        total_buy_gbp=140.0,
        listing_type="fixed",
        raw_json={"source": "craigslist_html"},
    )
    client = MutableCompsClient(listing=listing, comps=[])

    from ebayflip.db import add_target, init_db, list_evaluations_with_listings

    init_db(config.db_path)
    add_target(
        config.db_path,
        Target(id=None, name="Nintendo Switch OLED", query="Nintendo Switch OLED"),
    )

    first = run_scan(config, client)  # type: ignore[arg-type]
    assert first.evaluated == 1
    rows = list_evaluations_with_listings(config.db_path)
    assert rows[0]["decision"] == "ignore"

    client.comps = [
        SoldComp(price_gbp=230.0, title="Nintendo Switch OLED"),
        SoldComp(price_gbp=240.0, title="Nintendo Switch OLED"),
    ]
    second = run_scan(config, client)  # type: ignore[arg-type]
    assert second.evaluated == 1
    rows = list_evaluations_with_listings(config.db_path)
    assert rows[0]["resale_est_gbp"] > 0
