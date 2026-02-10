from __future__ import annotations

import json
from types import SimpleNamespace

from ebayflip.dashboard_data import load_latest_scan
from scanner.run_scan import _zero_result_summary


def test_load_latest_scan_strips_blocked_zero_targets_by_default(tmp_path) -> None:
    path = tmp_path / "latest.json"
    path.write_text(
        json.dumps(
            {
                "scan_summary": {
                    "zero_result_targets": [
                        {"target_name": "A", "blocked_reason": "splashui_challenge"},
                        {"target_name": "B", "raw_count": 0, "filtered_count": 0},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    payload = load_latest_scan(path)
    assert payload is not None
    zero_targets = payload.get("scan_summary", {}).get("zero_result_targets", [])
    assert len(zero_targets) == 1
    assert zero_targets[0]["target_name"] == "B"


def test_zero_result_summary_drops_blocked_targets(monkeypatch) -> None:
    monkeypatch.setenv("DROP_BLOCKED_TARGETS", "1")
    summary = SimpleNamespace(
        zero_result_debug=[
            SimpleNamespace(
                target_name="A",
                target_query="a",
                retry_report=[],
                raw_count=0,
                filtered_count=0,
                blocked_reason="splashui_challenge",
                blocked_message="blocked",
                rejection_counts={},
            ),
            SimpleNamespace(
                target_name="B",
                target_query="b",
                retry_report=["step"],
                raw_count=0,
                filtered_count=0,
                blocked_reason=None,
                blocked_message=None,
                rejection_counts={},
            ),
        ]
    )
    out = _zero_result_summary(summary)
    assert len(out) == 1
    assert out[0]["target_name"] == "B"

