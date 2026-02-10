from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ebayflip.config import AlertSettings, AppConfig, RunSettings
from ebayflip.db import (
    add_target,
    init_db,
    list_evaluations_with_listings,
    list_targets,
    prune_stale_listings,
)
from ebayflip.ebay_client import EbayClient
from ebayflip.models import Target
from ebayflip.scheduler import run_scan

DB_PATH = ROOT_DIR / "ebayflip.sqlite"
DATA_DIR = ROOT_DIR / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.jsonl"
DEFAULT_SEED_TARGETS: tuple[str, ...] = (
    "Nintendo Switch OLED",
    "AirPods Pro 2",
    "Sony WH-1000XM5",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run marketplace arbitrage scans and persist results.")
    parser.add_argument(
        "--history-max-lines",
        type=int,
        default=200,
        help="Maximum number of snapshots to keep in data/history.jsonl.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep scanning on an interval (continuous mode).",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=float(os.getenv("AUTO_SCAN_INTERVAL_MIN", "15")),
        help="Delay between scans in watch mode.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Optional max number of scan cycles in watch mode (0 = unlimited).",
    )
    return parser.parse_args()


def _build_run_settings() -> RunSettings:
    settings = RunSettings.from_env()
    app_id = os.getenv("EBAY_APP_ID")
    if app_id:
        settings.use_playwright_fallback = False
    return settings


def _seed_targets_from_env_or_defaults() -> list[str]:
    configured = os.getenv("SCAN_TARGETS", "")
    if configured.strip():
        return [item.strip() for item in configured.split(",") if item.strip()]
    return list(DEFAULT_SEED_TARGETS)


def _ensure_scan_targets(db_path: str) -> list[str]:
    existing_enabled_targets = [target for target in list_targets(db_path) if target.enabled]
    if existing_enabled_targets:
        return []

    seeded_targets: list[str] = []
    for query in _seed_targets_from_env_or_defaults():
        add_target(
            db_path,
            Target(
                id=None,
                name=query,
                query=query,
            ),
        )
        seeded_targets.append(query)
    return seeded_targets


def _source_from_row(row: dict[str, Any], *, fallback: str) -> str:
    raw = row.get("raw_json")
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    if isinstance(decoded, dict):
        source = decoded.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return fallback


def _serialize_items(rows: list[dict[str, Any]], *, settings: RunSettings) -> list[dict[str, Any]]:
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
                "source": _source_from_row(row, fallback=settings.marketplace),
                "buy_marketplace": settings.marketplace,
                "sell_marketplace": settings.sell_marketplace,
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


def _zero_result_summary(summary: Any) -> list[dict[str, Any]]:
    zero_result_info = []
    for debug in summary.zero_result_debug:
        info: dict[str, Any] = {
            "target_name": debug.target_name,
            "target_query": debug.target_query,
            "retry_report": debug.retry_report,
            "raw_count": debug.raw_count,
            "filtered_count": debug.filtered_count,
        }
        if debug.blocked_reason:
            info["blocked_reason"] = debug.blocked_reason
        if debug.blocked_message:
            info["blocked_message"] = debug.blocked_message
        if debug.rejection_counts:
            info["rejection_counts"] = debug.rejection_counts
        zero_result_info.append(info)
    return zero_result_info


def _run_once(config: AppConfig, *, history_max_lines: int) -> dict[str, Any]:
    pruned = prune_stale_listings(config.db_path, max_age_hours=config.run.listing_max_age_hours)
    client = EbayClient(config.run, app_id=os.getenv("EBAY_APP_ID"))
    summary = run_scan(config, client)

    rows = list_evaluations_with_listings(config.db_path)
    items = _serialize_items(rows, settings=config.run)
    generated_at = datetime.now(timezone.utc).isoformat()
    zero_result_info = _zero_result_summary(summary)

    snapshot = {
        "generated_at": generated_at,
        "count": len(items),
        "marketplaces": {
            "buy": config.run.marketplace,
            "sell": config.run.sell_marketplace,
        },
        "scan_summary": {
            "scanned_targets": summary.scanned_targets,
            "new_listings": summary.new_listings,
            "evaluated": summary.evaluated,
            "deals": summary.deals,
            "stale_pruned": pruned,
            "request_cap_reached": summary.request_cap_reached,
            "last_scan": summary.last_scan,
            "zero_result_targets": zero_result_info,
            "buy_marketplace": summary.buy_marketplace,
            "sell_marketplace": summary.sell_marketplace,
        },
        "items": items,
    }
    _write_snapshot(snapshot, history_max_lines)
    return snapshot


def main() -> None:
    args = _parse_args()
    settings = _build_run_settings()
    config = AppConfig(
        db_path=str(DB_PATH),
        run=settings,
        alerts=AlertSettings(discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL")),
    )
    init_db(config.db_path)
    seeded_targets = _ensure_scan_targets(config.db_path)
    if seeded_targets:
        print(f"Seeded {len(seeded_targets)} target(s): {', '.join(seeded_targets)}")

    cycle = 0
    while True:
        cycle += 1
        snapshot = _run_once(config, history_max_lines=args.history_max_lines)
        print(
            f"Cycle {cycle}: Wrote {snapshot.get('count', 0)} items to {LATEST_PATH} "
            f"(buy={settings.marketplace} -> sell={settings.sell_marketplace})"
        )
        if not args.watch:
            break
        if args.max_cycles > 0 and cycle >= args.max_cycles:
            break
        sleep_seconds = max(5, int(args.interval_minutes * 60))
        print(f"Waiting {sleep_seconds}s before next scan...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
