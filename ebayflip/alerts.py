from __future__ import annotations

import json
from typing import Optional

import requests

from ebayflip import get_logger

LOGGER = get_logger()


def send_discord_alert(
    webhook_url: Optional[str],
    title: str,
    listing_url: str,
    buy_total: float,
    resale_est: float,
    expected_profit: float,
    roi: float,
    confidence: float,
    reasons: list[str],
) -> bool:
    if not webhook_url:
        return False
    content = (
        f"**{title}**\n"
        f"Buy total: £{buy_total:.2f} | Resale est: £{resale_est:.2f}\n"
        f"Profit: £{expected_profit:.2f} | ROI: {roi:.2%} | Confidence: {confidence:.2f}\n"
        f"{listing_url}\n"
        f"Why flagged: {', '.join(reasons[:3])}"
    )
    payload = {"content": content}
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.warning("Discord alert failed: %s", exc)
        return False
    return True
