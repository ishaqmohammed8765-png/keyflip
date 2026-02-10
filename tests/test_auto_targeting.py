from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.db import init_db, list_targets
from scanner.run_scan import _ensure_scan_targets


def test_ensure_scan_targets_adds_popular_targets(tmp_path) -> None:
    db_path = str(tmp_path / "auto_targets.sqlite")
    init_db(db_path)
    settings = RunSettings(
        auto_popular_targets=True,
        popular_targets_per_category=1,
        auto_smart_targets=False,
    )
    added = _ensure_scan_targets(db_path, settings=settings, discovery_rows=[])
    targets = list_targets(db_path)

    assert added
    assert len(targets) >= 3

