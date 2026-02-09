from __future__ import annotations

import dataclasses
import re
from typing import Any, Optional


def broaden_query(query: str) -> str:
    if not query:
        return query
    cleaned = re.sub(r'(["\'])(.*?)\1', r"\2", query)
    cleaned = re.sub(r"(?<=\D)(?=\d)|(?<=\d)(?=\D)", " ", cleaned)
    cleaned = re.sub(r"\b\d+\s?(gb|tb)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+\s?(gig|gigabyte|terabyte)s?\b", "", cleaned, flags=re.IGNORECASE)
    colors = (
        "black",
        "white",
        "silver",
        "gray",
        "grey",
        "blue",
        "red",
        "green",
        "graphite",
        "gold",
        "pink",
        "purple",
        "midnight",
        "starlight",
    )
    pattern = r"\b(" + "|".join(colors) + r")\b"
    cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_retry_steps(base: Any) -> list[tuple[str, Any]]:
    steps: list[tuple[str, Any]] = [("initial", base)]
    if getattr(base, "category_id", None):
        steps.append(("removed category filter", dataclasses.replace(base, category_id=None)))
    if getattr(base, "condition", None):
        steps.append(("removed condition filter", dataclasses.replace(base, condition=None)))
    listing_type = getattr(base, "listing_type", None)
    if listing_type and listing_type != "any":
        steps.append(("removed listing type filter", dataclasses.replace(base, listing_type="any")))
    if getattr(base, "max_buy_gbp", None) is not None or getattr(base, "shipping_max_gbp", None) is not None:
        steps.append(
            (
                "removed price filters",
                dataclasses.replace(base, max_buy_gbp=None, shipping_max_gbp=None),
            )
        )
    widened_query = broaden_query(getattr(base, "query", ""))
    if widened_query and widened_query != getattr(base, "query", ""):
        steps.append(
            (
                f"broadened query from '{getattr(base, 'query', '')}' to '{widened_query}'",
                dataclasses.replace(base, query=widened_query),
            )
        )
    return steps


def total_max(max_buy: Optional[float], shipping_max: Optional[float]) -> Optional[float]:
    if max_buy is None and shipping_max is None:
        return None
    if max_buy is None:
        return shipping_max
    if shipping_max is None:
        return max_buy
    return max_buy + shipping_max
