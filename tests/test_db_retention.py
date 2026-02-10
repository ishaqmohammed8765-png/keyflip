from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ebayflip.db import add_target, get_connection, init_db, prune_stale_listings, upsert_listing
from ebayflip.models import Listing, Target


def test_prune_stale_listings_removes_old_rows(tmp_path) -> None:
    db_path = str(tmp_path / "retention.sqlite")
    init_db(db_path)
    target_id = add_target(db_path, Target(id=None, name="switch", query="switch"))

    old_listing = Listing(
        ebay_item_id="old-1",
        target_id=target_id,
        title="Old Listing",
        url="https://example.test/old",
        price_gbp=100.0,
        shipping_gbp=5.0,
        total_buy_gbp=105.0,
    )
    new_listing = Listing(
        ebay_item_id="new-1",
        target_id=target_id,
        title="New Listing",
        url="https://example.test/new",
        price_gbp=120.0,
        shipping_gbp=5.0,
        total_buy_gbp=125.0,
    )
    old_id, _ = upsert_listing(db_path, old_listing)
    upsert_listing(db_path, new_listing)

    old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    with get_connection(db_path) as conn:
        conn.execute("UPDATE listings SET last_seen_at = ? WHERE id = ?", (old_time, old_id))

    removed = prune_stale_listings(db_path, max_age_hours=72)
    assert removed == 1

    with get_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()["c"]
    assert count == 1

