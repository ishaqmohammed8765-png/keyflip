from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any

from ebayflip.models import Target

_NOISE_WORDS = {
    "a1",
    "a2",
    "bargain",
    "boxed",
    "box",
    "bundle",
    "bundleonly",
    "condition",
    "delivery",
    "excellent",
    "fair",
    "for",
    "fully",
    "good",
    "grade",
    "great",
    "in",
    "included",
    "latest",
    "like",
    "mint",
    "new",
    "on",
    "open",
    "phone",
    "pluscase",
    "protection",
    "refurbished",
    "sale",
    "sealed",
    "smartphone",
    "unlocked",
    "used",
    "warranty",
    "with",
    "without",
}

_COLOR_WORDS = {
    "black",
    "blue",
    "gold",
    "graphite",
    "gray",
    "green",
    "grey",
    "midnight",
    "pink",
    "purple",
    "red",
    "rose",
    "silver",
    "space",
    "starlight",
    "white",
    "yellow",
}

_CAPACITY_PATTERN = re.compile(r"^\d{2,4}(gb|tb)$", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(r"[a-z0-9\-\+]+", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(slots=True)
class SmartTargetSuggestion:
    name: str
    query: str
    max_buy_gbp: float | None
    source_count: int
    avg_confidence: float
    avg_profit_gbp: float


def _normalize_text(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value.strip().lower())


def _is_useful_token(token: str) -> bool:
    if len(token) < 2:
        return False
    if token in _NOISE_WORDS or token in _COLOR_WORDS:
        return False
    if _CAPACITY_PATTERN.match(token):
        return False
    return True


def _display_name_from_query(query: str) -> str:
    parts = []
    for token in query.split():
        if any(ch.isdigit() for ch in token) or len(token) <= 3:
            parts.append(token.upper())
        else:
            parts.append(token.capitalize())
    return " ".join(parts)


def infer_target_query_from_title(title: str, max_tokens: int = 4) -> str:
    lowered = title.lower()
    tokens = [token for token in _TOKEN_PATTERN.findall(lowered) if _is_useful_token(token)]
    if not tokens:
        return ""
    strong_tokens = [token for token in tokens if any(ch.isdigit() for ch in token)]
    selected: list[str] = []
    for token in tokens:
        if len(selected) >= max_tokens:
            break
        if token not in selected:
            selected.append(token)
    if strong_tokens and selected and not any(any(ch.isdigit() for ch in token) for token in selected):
        selected[-1] = strong_tokens[0]
    return " ".join(selected[:max_tokens]).strip()


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def suggest_targets_from_evaluations(
    rows: list[dict[str, Any]],
    existing_targets: list[Target],
    *,
    limit: int = 3,
    min_confidence: float = 0.55,
    min_profit_gbp: float = 8.0,
) -> list[SmartTargetSuggestion]:
    existing_keys = {
        _normalize_text(target.query or target.name)
        for target in existing_targets
        if (target.query or target.name)
    }
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        decision = str(row.get("decision") or "").strip().lower()
        confidence = _safe_float(row.get("confidence"))
        profit = _safe_float(row.get("expected_profit_gbp"))
        title = str(row.get("title") or "").strip()
        total_buy = _safe_float(row.get("total_buy_gbp"))
        if decision and decision not in {"deal", "maybe"}:
            continue
        if confidence < min_confidence or profit < min_profit_gbp:
            continue
        if not title:
            continue
        query = infer_target_query_from_title(title)
        if not query:
            continue
        query_key = _normalize_text(query)
        if query_key in existing_keys:
            continue

        bucket = buckets.setdefault(
            query_key,
            {
                "query": query,
                "confidences": [],
                "profits": [],
                "buys": [],
            },
        )
        bucket["confidences"].append(confidence)
        bucket["profits"].append(profit)
        if total_buy > 0:
            bucket["buys"].append(total_buy)

    suggestions: list[SmartTargetSuggestion] = []
    for bucket in buckets.values():
        confidences: list[float] = bucket["confidences"]
        profits: list[float] = bucket["profits"]
        buys: list[float] = bucket["buys"]
        if not confidences or not profits:
            continue

        median_buy = statistics.median(buys) if buys else None
        conservative_max_buy = round(median_buy * 0.92, 2) if median_buy else None
        suggestions.append(
            SmartTargetSuggestion(
                name=_display_name_from_query(bucket["query"]),
                query=bucket["query"],
                max_buy_gbp=conservative_max_buy,
                source_count=len(confidences),
                avg_confidence=statistics.mean(confidences),
                avg_profit_gbp=statistics.mean(profits),
            )
        )

    def _rank_key(item: SmartTargetSuggestion) -> tuple[float, float, int]:
        # Favors consistent, profitable opportunities first.
        return (
            item.avg_profit_gbp * item.avg_confidence,
            item.avg_confidence,
            item.source_count,
        )

    ranked = sorted(suggestions, key=_rank_key, reverse=True)
    return ranked[: max(0, limit)]
