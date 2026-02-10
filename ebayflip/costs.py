from __future__ import annotations

from ebayflip.config import RunSettings


def other_fees_gbp_for_resale(resale_est_gbp: float, settings: RunSettings) -> float:
    """Fees and reserves that are not the marketplace selling fee.

    This is used in both scoring and "max buy" calculations so UI guidance matches
    profit math as closely as possible.
    """
    resale = float(resale_est_gbp or 0.0)
    payment_fee_gbp = (resale * settings.payment_fee_pct) + settings.payment_fee_fixed_gbp
    return_reserve_gbp = resale * settings.return_reserve_pct
    vat_reserve_gbp = resale * settings.vat_reserve_pct
    fixed = settings.packaging_gbp + settings.labour_gbp + settings.extra_fixed_costs_gbp
    return max(0.0, payment_fee_gbp + return_reserve_gbp + vat_reserve_gbp + fixed)

