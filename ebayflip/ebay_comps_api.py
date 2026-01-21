from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Any, Optional

import requests

from ebayflip.config import RunSettings
from ebayflip.comps_deals import CompPoint


FINDING_ENDPOINT = "https://svcs.ebay.com/services/search/FindingService/v1"


@dataclass(slots=True)
class EbayApiConfig:
    enabled: bool
    app_id: Optional[str]
    marketplace: str
    comps_days: int
    comps_limit: int

    @classmethod
    def from_env(cls) -> "EbayApiConfig":
        enabled = _read_bool("EBAY_API_ENABLED")
        app_id = os.getenv("EBAY_APP_ID")
        marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB").strip() or "EBAY_GB"
        comps_days = _read_int("EBAY_COMPS_DAYS", 30)
        comps_limit = _read_int("EBAY_COMPS_LIMIT", 60)
        return cls(
            enabled=enabled,
            app_id=app_id,
            marketplace=marketplace,
            comps_days=comps_days,
            comps_limit=comps_limit,
        )

    def validate(self) -> None:
        if not self.enabled:
            raise ValueError(
                "eBay API mode is disabled. Set EBAY_API_ENABLED=1 and EBAY_APP_ID to use sold comps."
            )
        if not self.app_id:
            raise ValueError(
                "EBAY_API_ENABLED=1 requires EBAY_APP_ID. Add your eBay App ID to the environment."
            )


class EbayCompsApiClient:
    def __init__(self, config: EbayApiConfig, settings: RunSettings) -> None:
        self.config = config
        self.settings = settings
        self.session = requests.Session()

    def fetch_sold_comps(
        self,
        query: str,
        *,
        days: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[CompPoint]:
        self.config.validate()
        days = days or self.config.comps_days
        limit = limit or self.config.comps_limit
        end_time_from = _format_ebay_time(datetime.utcnow() - timedelta(days=days))
        params = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": self.config.app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": query,
            "paginationInput.entriesPerPage": limit,
            "paginationInput.pageNumber": 1,
            "GLOBAL-ID": self.config.marketplace,
        }
        params["itemFilter(0).name"] = "SoldItemsOnly"
        params["itemFilter(0).value"] = "true"
        params["itemFilter(1).name"] = "EndTimeFrom"
        params["itemFilter(1).value"] = end_time_from
        response = self.session.get(FINDING_ENDPOINT, params=params, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(
                f"eBay API request failed with {response.status_code}. "
                "Check EBAY_APP_ID and marketplace configuration."
            )
        data = response.json()
        errors = (
            data.get("findCompletedItemsResponse", [{}])[0]
            .get("errorMessage", [{}])[0]
            .get("error", [])
        )
        if errors:
            message = errors[0].get("message", ["Unknown error"])[0]
            raise RuntimeError(f"eBay API error: {message}")
        items = (
            data.get("findCompletedItemsResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
        )
        comps: list[CompPoint] = []
        for item in items:
            comp = _parse_comp_item(item, self.settings)
            if comp:
                comps.append(comp)
        return comps


def _parse_comp_item(item: dict[str, Any], settings: RunSettings) -> Optional[CompPoint]:
    price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
    currency = price_info.get("@currencyId", "GBP")
    price = _safe_float(price_info.get("__value__", 0.0))
    if price is None:
        return None
    shipping_info = item.get("shippingInfo", [{}])[0]
    ship_price_info = shipping_info.get("shippingServiceCost", [{}])[0] if shipping_info else {}
    ship_value = _safe_float(ship_price_info.get("__value__", 0.0)) if ship_price_info else None
    shipping_type = str(shipping_info.get("shippingType", [""])[0]) if shipping_info else ""
    if ship_value is None and "free" in shipping_type.lower():
        ship_value = 0.0
    if currency != "GBP":
        if not settings.allow_non_gbp:
            return None
        price = price * settings.gbp_exchange_rate
        ship_value = ship_value * settings.gbp_exchange_rate if ship_value is not None else None
    shipping_gbp = ship_value
    total_gbp = price + (shipping_gbp or 0.0)
    sold_date = _parse_date(
        item.get("listingInfo", [{}])[0].get("endTime", [None])[0]
        if item.get("listingInfo")
        else None
    )
    return CompPoint(
        price_gbp=price,
        shipping_gbp=shipping_gbp,
        total_gbp=total_gbp,
        sold_date=sold_date,
        title=str(item.get("title", [""])[0]) if item.get("title") else None,
        url=str(item.get("viewItemURL", [""])[0]) if item.get("viewItemURL") else None,
    )


def _read_bool(name: str) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return False
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _format_ebay_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
