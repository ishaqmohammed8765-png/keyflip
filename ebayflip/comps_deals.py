from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from statistics import median
from typing import Iterable, Optional

from ebayflip.config import RunSettings
from ebayflip.models import SoldComp


JUNK_PHRASES = (
    "read description",
    "job lot",
    "joblot",
    "case only",
    "empty box",
    "box only",
    "spares repairs",
    "spares repair",
    "spares/repairs",
    "for parts",
)

OUTLIER_PHRASES = (
    "case",
    "cover",
    "box only",
    "empty box",
    "screen protector",
)


@dataclass(slots=True)
class CandidateItem:
    source: str
    title_raw: str
    title_norm: str
    buy_price_gbp: float
    condition_hint: Optional[str]
    url: Optional[str]
    attributes: dict


@dataclass(slots=True)
class CompPoint:
    price_gbp: float
    shipping_gbp: Optional[float]
    total_gbp: float
    sold_date: Optional[date]
    title: Optional[str]
    url: Optional[str]


@dataclass(slots=True)
class CompSummary:
    sold_points: list[CompPoint]
    sold_median_gbp: Optional[float]
    sold_p25_gbp: Optional[float]
    sold_p75_gbp: Optional[float]
    sample_size: int
    days: int
    query_used: str


@dataclass(slots=True)
class DealResult:
    candidate: CandidateItem
    comps: CompSummary
    expected_sell_gbp: Optional[float]
    est_fees_gbp: Optional[float]
    est_shipping_gbp: float
    buffer_gbp: float
    profit_gbp: Optional[float]
    roi: Optional[float]
    confidence: str
    notes: list[str] = field(default_factory=list)


def build_candidate_item(
    *,
    source: str,
    title_raw: str,
    buy_price_gbp: float,
    condition_hint: Optional[str] = None,
    url: Optional[str] = None,
) -> CandidateItem:
    title_norm, attributes, query = normalize_title(title_raw)
    attributes["query"] = query
    return CandidateItem(
        source=source,
        title_raw=title_raw,
        title_norm=title_norm,
        buy_price_gbp=buy_price_gbp,
        condition_hint=condition_hint,
        url=url,
        attributes=attributes,
    )


def normalize_title(title: str) -> tuple[str, dict, str]:
    text = title.lower().replace("/", " ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for phrase in JUNK_PHRASES:
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    attributes: dict[str, object] = {}
    storage_match = re.search(r"\b(\d{2,4})\s?gb\b", text)
    if storage_match:
        attributes["storage_gb"] = int(storage_match.group(1))
    unlocked = False
    if "unlocked" in text or "sim free" in text or "simfree" in text:
        unlocked = True
        attributes["unlocked"] = True
    tokens = text.split()
    if storage_match:
        storage_token = f"{storage_match.group(1)}gb"
        if storage_token not in tokens:
            tokens.append(storage_token)
    if unlocked and "unlocked" not in tokens:
        tokens.append("unlocked")
    query = " ".join(tokens).strip()
    return text, attributes, query


def filter_outlier_comps(comps: Iterable[CompPoint], candidate: CandidateItem) -> list[CompPoint]:
    candidate_text = candidate.title_norm.lower()
    allow_phrases = {phrase for phrase in OUTLIER_PHRASES if phrase in candidate_text}
    filtered: list[CompPoint] = []
    for comp in comps:
        title = (comp.title or "").lower()
        if any(phrase in title for phrase in OUTLIER_PHRASES):
            if not any(phrase in title for phrase in allow_phrases):
                continue
        filtered.append(comp)
    return filtered


def compute_comp_summary(
    comp_query: str,
    comps: Iterable[CompPoint],
    *,
    days: int,
) -> CompSummary:
    comp_list = list(comps)
    totals = sorted([comp.total_gbp for comp in comp_list if comp.total_gbp > 0])
    sample_size = len(totals)
    if sample_size == 0:
        return CompSummary(
            sold_points=comp_list,
            sold_median_gbp=None,
            sold_p25_gbp=None,
            sold_p75_gbp=None,
            sample_size=0,
            days=days,
            query_used=comp_query,
        )
    median_val = float(median(totals))
    p25 = totals[int(0.25 * (sample_size - 1))]
    p75 = totals[int(0.75 * (sample_size - 1))]
    return CompSummary(
        sold_points=comp_list,
        sold_median_gbp=median_val,
        sold_p25_gbp=p25,
        sold_p75_gbp=p75,
        sample_size=sample_size,
        days=days,
        query_used=comp_query,
    )


def confidence_from_summary(summary: CompSummary) -> str:
    if summary.sample_size == 0 or summary.sold_median_gbp in (None, 0):
        return "low"
    if (
        summary.sample_size >= 30
        and summary.sold_p25_gbp is not None
        and summary.sold_p75_gbp is not None
        and (summary.sold_p75_gbp - summary.sold_p25_gbp) / summary.sold_median_gbp <= 0.25
    ):
        return "high"
    if summary.sample_size >= 12:
        return "medium"
    return "low"


def estimate_deal(
    candidate: CandidateItem,
    summary: CompSummary,
    settings: RunSettings,
) -> DealResult:
    notes: list[str] = []
    expected_sell = summary.sold_median_gbp
    est_fees = expected_sell * settings.ebay_fee_pct if expected_sell is not None else None
    buffer_gbp = settings.buffer_fixed_gbp + candidate.buy_price_gbp * settings.buffer_pct_of_buy
    est_shipping = settings.shipping_out_gbp
    profit = (
        expected_sell - est_fees - candidate.buy_price_gbp - buffer_gbp - est_shipping
        if expected_sell is not None and est_fees is not None
        else None
    )
    roi = profit / candidate.buy_price_gbp if profit is not None and candidate.buy_price_gbp else None
    if summary.sample_size == 0:
        notes.append("No sold comps found.")
    confidence = confidence_from_summary(summary)
    if confidence == "low":
        notes.append("Low confidence due to limited comps or wide spread.")
    return DealResult(
        candidate=candidate,
        comps=summary,
        expected_sell_gbp=expected_sell,
        est_fees_gbp=est_fees,
        est_shipping_gbp=est_shipping,
        buffer_gbp=buffer_gbp,
        profit_gbp=profit,
        roi=roi,
        confidence=confidence,
        notes=notes,
    )


def analyze_candidates(
    candidates: Iterable[CandidateItem],
    comps_points: dict[str, list[CompPoint]],
    *,
    days: int,
    settings: RunSettings,
) -> list[DealResult]:
    results: list[DealResult] = []
    for candidate in candidates:
        query = candidate.attributes.get("query", candidate.title_norm)
        comps = filter_outlier_comps(comps_points.get(query, []), candidate)
        summary = compute_comp_summary(query, comps, days=days)
        results.append(estimate_deal(candidate, summary, settings))
    return results


def sold_comps_to_points(comps: Iterable[SoldComp]) -> list[CompPoint]:
    return [
        CompPoint(
            price_gbp=comp.price_gbp,
            shipping_gbp=None,
            total_gbp=comp.price_gbp,
            sold_date=None,
            title=comp.title,
            url=comp.url,
        )
        for comp in comps
    ]
