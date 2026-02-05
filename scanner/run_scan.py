from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ebayflip.config import AlertSettings, AppConfig, RunSettings
from ebayflip.db import init_db, list_evaluations_with_listings
from ebayflip.ebay_client import EbayClient
from ebayflip.scheduler import run_scan

DB_PATH = ROOT_DIR / "ebayflip.sqlite"
DATA_DIR = ROOT_DIR / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.jsonl"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single marketplace scan and persist results.")
    parser.add_argument(
        "--history-max-lines",
        type=int,
        default=200,
        help="Maximum number of snapshots to keep in data/history.jsonl.",
    )
    return parser.parse_args()


def _build_run_settings() -> RunSettings:
    settings = RunSettings()
    app_id = os.getenv("EBAY_APP_ID")
    if app_id:
        settings.use_playwright_fallback = False
    return settings


def _serialize_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        reasons = row.get("reasons_json")
        parsed_reasons: list[str] = []
        if isinstance(reasons, str) and reasons:
            try:
                decoded = json.loads(reasons)
                if isinstance(decoded, list):
                    parsed_reasons = [str(reason) for reason in decoded]
            except json.JSONDecodeError:
                parsed_reasons = []

        items.append(
            {
                "listing_id": row.get("listing_id"),
                "title": row.get("title"),
                "url": row.get("url"),
                "total_buy_gbp": row.get("total_buy_gbp"),
                "resale_est_gbp": row.get("resale_est_gbp"),
                "expected_profit_gbp": row.get("expected_profit_gbp"),
                "roi": row.get("roi"),
                "confidence": row.get("confidence"),
                "deal_score": row.get("deal_score"),
                "decision": row.get("decision"),
                "reasons": parsed_reasons,
                "evaluated_at": row.get("evaluated_at"),
                "image_url": row.get("image_url"),
                "location": row.get("location"),
                "listing_type": row.get("listing_type"),
            }
        )
    return items


def _write_snapshot(snapshot: dict[str, Any], history_max_lines: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    if history_max_lines <= 0:
        return

    existing_lines: list[str] = []
    if HISTORY_PATH.exists():
        existing_lines = [line for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    existing_lines.append(json.dumps(snapshot))
    pruned_lines = existing_lines[-history_max_lines:]
    HISTORY_PATH.write_text("\n".join(pruned_lines) + "\n", encoding="utf-8")


def main() -> None:
    args = _parse_args()
    settings = _build_run_settings()
    config = AppConfig(
        db_path=str(DB_PATH),
        run=settings,
        alerts=AlertSettings(discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL")),
    )
    init_db(config.db_path)

    client = EbayClient(settings, app_id=os.getenv("EBAY_APP_ID"))
    summary = run_scan(config, client)

    rows = list_evaluations_with_listings(config.db_path)
    items = _serialize_items(rows)

    generated_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "generated_at": generated_at,
        "count": len(items),
        "scan_summary": {
            "scanned_targets": summary.scanned_targets,
            "new_listings": summary.new_listings,
            "evaluated": summary.evaluated,
            "deals": summary.deals,
            "request_cap_reached": summary.request_cap_reached,
            "last_scan": summary.last_scan,
        },
        "items": items,
    }
    _write_snapshot(snapshot, args.history_max_lines)
    print(f"Wrote {len(items)} items to {LATEST_PATH}")


if __name__ == "__main__":
    main()
