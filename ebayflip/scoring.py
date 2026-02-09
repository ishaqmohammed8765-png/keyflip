from __future__ import annotations

from ebayflip.config import RunSettings
from ebayflip.models import CompStats, Evaluation, Listing


def evaluate_listing(listing: Listing, comps: CompStats, settings: RunSettings) -> Evaluation:
    reasons: list[str] = []
    resale_est = comps.median_sold_gbp or 0.0

    if comps.sold_count == 0:
        reasons.append("No sold comps found - cannot estimate profit.")
        buffer_gbp = settings.buffer_fixed_gbp + (settings.buffer_pct_of_buy * listing.total_buy_gbp)
        return Evaluation(
            resale_est_gbp=0.0,
            ebay_fee_pct=settings.ebay_fee_pct,
            other_fees_gbp=0.0,
            shipping_out_gbp=settings.shipping_out_gbp,
            buffer_gbp=buffer_gbp,
            expected_profit_gbp=0.0,
            roi=0.0,
            confidence=0.0,
            deal_score=0.0,
            decision="ignore",
            reasons=reasons,
            evaluated_at=_now_iso(),
        )

    reasons.append(
        f"Median sold GBP {resale_est:.2f} from {comps.sold_count} comps (p25 GBP {_fmt(comps.p25_sold_gbp)}, p75 GBP {_fmt(comps.p75_sold_gbp)})."
    )

    buffer_gbp = settings.buffer_fixed_gbp + (settings.buffer_pct_of_buy * listing.total_buy_gbp)
    expected_profit = (
        resale_est * (1 - settings.ebay_fee_pct)
        - listing.total_buy_gbp
        - settings.shipping_out_gbp
        - buffer_gbp
    )
    roi = expected_profit / listing.total_buy_gbp if listing.total_buy_gbp else 0.0

    confidence = _confidence_score(listing, comps)
    reasons.extend(_confidence_reasons(listing, comps, confidence))

    deal_score = _deal_score(expected_profit, roi, confidence)
    decision = _decision(expected_profit, roi, confidence, settings)

    return Evaluation(
        resale_est_gbp=resale_est,
        ebay_fee_pct=settings.ebay_fee_pct,
        other_fees_gbp=0.0,
        shipping_out_gbp=settings.shipping_out_gbp,
        buffer_gbp=buffer_gbp,
        expected_profit_gbp=expected_profit,
        roi=roi,
        confidence=confidence,
        deal_score=deal_score,
        decision=decision,
        reasons=reasons,
        evaluated_at=_now_iso(),
    )


def _confidence_score(listing: Listing, comps: CompStats) -> float:
    score = 0.4
    if comps.sold_count >= 10:
        score += 0.25
    elif comps.sold_count >= 5:
        score += 0.15
    elif comps.sold_count >= 1:
        score += 0.05
    if comps.spread_gbp is not None and comps.median_sold_gbp:
        spread_ratio = comps.spread_gbp / max(comps.median_sold_gbp, 1)
        if spread_ratio <= 0.2:
            score += 0.2
        elif spread_ratio <= 0.35:
            score += 0.1
        else:
            score -= 0.1
    if listing.seller_feedback_pct and listing.seller_feedback_pct >= 98:
        score += 0.1
    if listing.returns_accepted:
        score += 0.05
    return max(0.0, min(score, 1.0))


def _confidence_reasons(listing: Listing, comps: CompStats, confidence: float) -> list[str]:
    reasons = []
    if comps.spread_gbp is not None and comps.median_sold_gbp:
        spread_ratio = comps.spread_gbp / max(comps.median_sold_gbp, 1)
        if spread_ratio > 0.35:
            reasons.append("Wide comp spread reduced confidence.")
        else:
            reasons.append("Comp prices are tightly clustered.")
    if listing.seller_feedback_pct is not None:
        reasons.append(f"Seller feedback {listing.seller_feedback_pct:.1f}%.")
    if listing.returns_accepted:
        reasons.append("Returns accepted by seller.")
    reasons.append(f"Confidence score {confidence:.2f}.")
    return reasons


def _deal_score(profit: float, roi: float, confidence: float) -> float:
    capped_roi = max(0.0, min(roi, 1.0))
    return max(0.0, profit) * 0.6 + capped_roi * 40 + confidence * 20


def _decision(profit: float, roi: float, confidence: float, settings: RunSettings) -> str:
    if profit >= settings.min_profit_gbp and roi >= settings.min_roi and confidence >= settings.min_confidence:
        return "deal"
    if profit >= 0 and roi >= 0.10 and confidence >= 0.35:
        return "maybe"
    return "ignore"


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

