from __future__ import annotations

from ebayflip.config import AlertSettings, AppConfig, RunSettings
from ebayflip.db import add_target, init_db
from ebayflip.ebay_client import EbayClient
from ebayflip.models import Target
from ebayflip.scheduler import ArbitrageScanner


def test_scanner_uses_parallel_path_when_enabled(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "parallel.sqlite")
    init_db(db_path)
    add_target(db_path, Target(id=None, name="Test 1", query="test one"))
    add_target(db_path, Target(id=None, name="Test 2", query="test two"))
    settings = RunSettings(scan_workers=2)
    config = AppConfig(db_path=db_path, run=settings, alerts=AlertSettings(discord_webhook_url=None))
    client = EbayClient(settings)
    scanner = ArbitrageScanner(config=config, client=client)
    called = {"parallel": False}

    def fake_scan_parallel(targets, *, workers):
        called["parallel"] = True
        assert workers == 2
        assert len(targets) == 2

    monkeypatch.setattr(scanner, "_scan_parallel", fake_scan_parallel)
    scanner.scan()
    assert called["parallel"] is True
