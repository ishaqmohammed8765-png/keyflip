from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

import requests


@dataclass(slots=True)
class CachedRate:
    rate: float
    expires_at: datetime


class FxConverter:
    """Small FX helper with cached live rates and deterministic fallback behavior."""

    def __init__(self, *, fallback_gbp_rate: float, enabled: bool = True, cache_minutes: int = 360) -> None:
        self.fallback_gbp_rate = float(fallback_gbp_rate)
        self.enabled = enabled
        self.cache_minutes = max(10, int(cache_minutes))
        self._cache: dict[tuple[str, str], CachedRate] = {}
        self._lock = Lock()

    def to_gbp(self, amount: float, currency: str) -> float:
        if not currency or currency.upper() == "GBP":
            return amount
        rate = self.get_rate(currency=currency, target="GBP")
        return amount * rate

    def get_rate(self, *, currency: str, target: str) -> float:
        src = (currency or "GBP").upper()
        dst = (target or "GBP").upper()
        if src == dst:
            return 1.0
        key = (src, dst)
        now = datetime.now(timezone.utc)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at > now:
                return cached.rate

        rate = self._fetch_rate(src, dst) if self.enabled else None
        if rate is None:
            rate = self._fallback_rate(src, dst)

        with self._lock:
            self._cache[key] = CachedRate(
                rate=rate,
                expires_at=now + timedelta(minutes=self.cache_minutes),
            )
        return rate

    def _fetch_rate(self, src: str, dst: str) -> Optional[float]:
        try:
            response = requests.get(f"https://open.er-api.com/v6/latest/{src}", timeout=6)
            response.raise_for_status()
            payload = response.json()
            rates = payload.get("rates")
            if not isinstance(rates, dict):
                return None
            value = rates.get(dst)
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _fallback_rate(self, src: str, dst: str) -> float:
        # Conservative fallback: static USD->GBP rate from settings, with simple EUR proxy.
        if src == "GBP" and dst != "GBP":
            inv = self._fallback_rate(dst, "GBP")
            return 1.0 / inv if inv > 0 else 1.0
        if src == "USD" and dst == "GBP":
            return self.fallback_gbp_rate
        if src == "EUR" and dst == "GBP":
            # Approximate EUR->GBP from configured USD->GBP anchor.
            return max(0.5, min(1.2, self.fallback_gbp_rate * 1.10))
        if src == "JPY" and dst == "GBP":
            return max(0.003, min(0.02, self.fallback_gbp_rate / 110.0))
        return 1.0

