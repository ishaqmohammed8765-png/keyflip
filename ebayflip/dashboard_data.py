from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import os

DECISION_ORDER = {"deal": 0, "maybe": 1, "ignore": 2}
CSV_FIELDS = [
    "decision",
    "title",
    "url",
    "total_buy_gbp",
    "resale_est_gbp",
    "expected_profit_gbp",
    "roi",
    "confidence",
    "deal_score",
    "flip_grade",
    "risk_band",
    "max_total_buy_target_gbp",
    "break_even_buy_gbp",
    "suggested_offer_gbp",
    "buy_edge_gbp",
    "is_actionable",
    "location",
    "listing_type",
    "evaluated_at",
    "reasons",
]


def load_latest_scan(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if os.getenv("DROP_BLOCKED_TARGETS", "1").strip().lower() in {"1", "true", "yes", "y", "on"}:
        summary = payload.get("scan_summary")
        if isinstance(summary, dict):
            zero_targets = summary.get("zero_result_targets")
            if isinstance(zero_targets, list):
                summary["zero_result_targets"] = [
                    entry for entry in zero_targets
                    if not (isinstance(entry, dict) and entry.get("blocked_reason"))
                ]
    return payload


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return entries


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda x: (
            DECISION_ORDER.get(x.get("decision", "ignore"), 3),
            -(x.get("deal_score") or 0),
        ),
    )


def summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    deal_count = sum(1 for item in items if item.get("decision") == "deal")
    maybe_count = sum(1 for item in items if item.get("decision") == "maybe")
    total_profit = sum(
        item.get("expected_profit_gbp", 0)
        for item in items
        if item.get("decision") in ("deal", "maybe") and (item.get("expected_profit_gbp") or 0) > 0
    )
    best_score = max((item.get("deal_score") or 0.0 for item in items), default=0.0)
    return {
        "deal_count": deal_count,
        "maybe_count": maybe_count,
        "total_profit": total_profit,
        "total_items": len(items),
        "best_score": best_score,
    }


def filter_items(
    items: list[dict[str, Any]],
    *,
    decision: str = "All",
    search_term: str = "",
    min_score: float = 0.0,
    min_profit: Optional[float] = None,
) -> list[dict[str, Any]]:
    filtered = items
    if decision != "All":
        filtered = [item for item in filtered if item.get("decision") == decision]
    if search_term:
        search_lower = search_term.lower().strip()
        filtered = [item for item in filtered if search_lower in (item.get("title") or "").lower()]
    if min_score > 0:
        filtered = [item for item in filtered if (item.get("deal_score") or 0.0) >= min_score]
    if min_profit is not None:
        filtered = [item for item in filtered if (item.get("expected_profit_gbp") or 0.0) >= min_profit]
    return filtered


def items_to_csv_bytes(items: list[dict[str, Any]]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = dict(item)
        reasons = row.get("reasons") or []
        row["reasons"] = "; ".join(str(reason) for reason in reasons)
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def history_summary_rows(history: list[dict[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in reversed(history[-limit:]):
        summary = entry.get("scan_summary") or {}
        rows.append(
            {
                "Scan Time": str(entry.get("generated_at", "-"))[:19],
                "Items": entry.get("count", 0),
                "Targets Scanned": summary.get("scanned_targets", 0),
                "New Listings": summary.get("new_listings", 0),
                "Deals": summary.get("deals", 0),
                "Evaluated": summary.get("evaluated", 0),
                "Cap Reached": "Yes" if summary.get("request_cap_reached") else "No",
            }
        )
    return rows


def scan_age_seconds(payload: dict[str, Any] | None) -> Optional[float]:
    if not payload:
        return None
    generated_at = payload.get("generated_at")
    if not generated_at:
        return None
    try:
        generated = datetime.fromisoformat(str(generated_at))
    except (TypeError, ValueError):
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - generated).total_seconds()
