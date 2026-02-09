from __future__ import annotations

from ebayflip.models import Target
from ebayflip.target_suggestions import infer_target_query_from_title, suggest_targets_from_evaluations


def test_infer_target_query_from_title_removes_noise_terms() -> None:
    title = "Apple iPhone 14 Pro 256GB Unlocked Blue Excellent Condition"
    assert infer_target_query_from_title(title) == "apple iphone 14 pro"


def test_suggest_targets_excludes_existing_targets() -> None:
    rows = [
        {
            "decision": "deal",
            "confidence": 0.80,
            "expected_profit_gbp": 25.0,
            "title": "Apple iPhone 14 Pro 256GB Unlocked",
            "total_buy_gbp": 220.0,
        },
        {
            "decision": "maybe",
            "confidence": 0.70,
            "expected_profit_gbp": 18.0,
            "title": "Apple iPhone 14 Pro 128GB Blue",
            "total_buy_gbp": 210.0,
        },
        {
            "decision": "deal",
            "confidence": 0.85,
            "expected_profit_gbp": 35.0,
            "title": "Sony WH-1000XM5 Black Boxed",
            "total_buy_gbp": 180.0,
        },
    ]
    existing = [Target(id=1, name="Sony WH-1000XM5", query="sony wh-1000xm5")]
    suggestions = suggest_targets_from_evaluations(rows, existing, limit=3, min_confidence=0.6, min_profit_gbp=10.0)

    assert len(suggestions) == 1
    assert suggestions[0].query == "apple iphone 14 pro"
    assert suggestions[0].max_buy_gbp == 197.8


def test_suggest_targets_ranks_by_quality_signal() -> None:
    rows = [
        {
            "decision": "deal",
            "confidence": 0.90,
            "expected_profit_gbp": 20.0,
            "title": "Nintendo Switch OLED White",
            "total_buy_gbp": 120.0,
        },
        {
            "decision": "deal",
            "confidence": 0.80,
            "expected_profit_gbp": 14.0,
            "title": "Google Pixel 7 Pro 128GB",
            "total_buy_gbp": 150.0,
        },
    ]
    suggestions = suggest_targets_from_evaluations(rows, [], limit=2, min_confidence=0.6, min_profit_gbp=10.0)
    assert len(suggestions) == 2
    assert suggestions[0].query == "nintendo switch oled"
