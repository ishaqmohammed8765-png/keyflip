from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import requests

from ebayflip import get_logger
from ebayflip.config import RunSettings
from ebayflip.filtering import filter_listings
from ebayflip.models import Listing, SoldComp, Target

LOGGER = get_logger()

FINDING_ENDPOINT = "https://svcs.ebay.com/services/search/FindingService/v1"


class ApiClientProtocol(Protocol):
    settings: RunSettings
    app_id: Optional[str]

    def _request(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        *,
        delay: bool = False,
        use_cache: bool = True,
        store_cache: bool = True,
        max_attempts: int = 3,
    ) -> tuple[requests.Response, bool]:
        ...

    def _currency_allowed(self, currency: str) -> bool:
        ...

    def _normalize_currency(self, price: float, shipping: float, currency: str) -> tuple[float, float]:
        ...

    def _apply_missing_shipping(self, shipping_value: float, shipping_missing: bool) -> tuple[float, Optional[float]]:
        ...

    def _build_log(self, *args: Any, **kwargs: Any) -> Any:
        ...


@dataclass(slots=True)
class ApiSearchOutcome:
    listings: list[Listing]
    rejection_counts: dict[str, int]
    raw_count: int
    filtered_count: int
    last_request_url: Optional[str]


class EbayApiProvider:
    def __init__(self, client: ApiClientProtocol) -> None:
        self.client = client

    def enabled(self) -> bool:
        raw_value = os.getenv("EBAY_API_ENABLED")
        if raw_value is None:
            return False
        return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"} and bool(
            self.client.app_id
        )

    def search_active_listings(
        self,
        criteria: Any,
        target: Target,
        diagnostics: list[Any],
    ) -> ApiSearchOutcome:
        limit = self.client.settings.scan_limit_per_target
        page = 1
        listings: list[Listing] = []
        rejection_counts: dict[str, int] = {}
        total_raw = 0
        total_filtered = 0
        last_request_url: Optional[str] = None
        total_pages = 1

        while True:
            params = self._build_api_params(criteria, page, limit)
            response, _ = self.client._request(FINDING_ENDPOINT, params=params, delay=False, max_attempts=1)
            data = response.json()
            raw_items = (
                data.get("findItemsByKeywordsResponse", [{}])[0]
                .get("searchResult", [{}])[0]
                .get("item", [])
            )
            raw_listings: list[Listing] = []
            for item in raw_items:
                listing = self._parse_api_item(item, target)
                if listing:
                    raw_listings.append(listing)
            total_raw += len(raw_listings)
            filtered = filter_listings(
                raw_listings,
                self._criteria_to_target(criteria, target),
                self.client.settings,
            )
            listings.extend(filtered.listings)
            total_filtered += len(filtered.listings)
            for reason, count in filtered.rejection_counts.items():
                rejection_counts[reason] = rejection_counts.get(reason, 0) + count
            total_pages = _parse_api_total_pages(data) or 1
            last_request_url = response.url
            diagnostics.append(
                self.client._build_log(
                    mode="api",
                    criteria=criteria,
                    page=page,
                    limit=limit,
                    status=response.status_code,
                    raw_count=len(raw_listings),
                    filtered_count=len(filtered.listings),
                    request_url=response.url,
                    item_count=len(raw_items),
                    parsed_count=len(raw_listings),
                )
            )
            if listings and len(listings) >= limit:
                listings = listings[:limit]
                break
            if page == 1 and not raw_listings:
                break
            if page >= total_pages:
                break
            page += 1

        return ApiSearchOutcome(
            listings=listings,
            rejection_counts=rejection_counts,
            raw_count=total_raw,
            filtered_count=total_filtered,
            last_request_url=last_request_url,
        )

    def search_sold_comps(self, comp_query: str) -> list[SoldComp]:
        params = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": self.client.app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": comp_query,
            "paginationInput.entriesPerPage": self.client.settings.comps_limit,
            "paginationInput.pageNumber": 1,
            "GLOBAL-ID": "EBAY-GB",
        }
        params["itemFilter(0).name"] = "SoldItemsOnly"
        params["itemFilter(0).value"] = "true"
        response, _ = self.client._request(FINDING_ENDPOINT, params=params, max_attempts=1)
        data = response.json()
        items = (
            data.get("findCompletedItemsResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
        )
        comps: list[SoldComp] = []
        for item in items:
            price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
            currency = price_info.get("@currencyId", "GBP")
            price = float(price_info.get("__value__", 0.0))
            if not self.client._currency_allowed(currency):
                continue
            price_gbp, _ = self.client._normalize_currency(price, 0.0, currency)
            comps.append(
                SoldComp(
                    price_gbp=price_gbp,
                    title=str(item.get("title", [""])[0]),
                    url=str(item.get("viewItemURL", [""])[0]),
                )
            )
        return comps

    def _parse_api_item(self, item: dict[str, Any], target: Target) -> Optional[Listing]:
        price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
        currency = price_info.get("@currencyId", "GBP")
        price = float(price_info.get("__value__", 0.0))
        shipping_info = item.get("shippingInfo", [{}])[0]
        ship_price_info = shipping_info.get("shippingServiceCost", [{}])[0]
        shipping_missing = not ship_price_info
        shipping = float(ship_price_info.get("__value__", 0.0)) if ship_price_info else 0.0
        if not self.client._currency_allowed(currency):
            return None
        shipping, assumed_shipping = self.client._apply_missing_shipping(shipping, shipping_missing)
        price_gbp, shipping_gbp = self.client._normalize_currency(price, shipping, currency)
        total = price_gbp + shipping_gbp
        ebay_item_id = str(item.get("itemId", [""])[0])
        if not ebay_item_id:
            return None
        return Listing(
            ebay_item_id=ebay_item_id,
            target_id=target.id or 0,
            title=str(item.get("title", [""])[0]),
            url=str(item.get("viewItemURL", [""])[0]),
            price_gbp=price_gbp,
            shipping_gbp=shipping_gbp,
            total_buy_gbp=total,
            condition=str(item.get("condition", [{}])[0].get("conditionDisplayName", [None])[0]),
            seller_feedback_pct=_safe_float(
                item.get("sellerInfo", [{}])[0].get("positiveFeedbackPercent", [None])[0]
            ),
            seller_feedback_score=_safe_int(item.get("sellerInfo", [{}])[0].get("feedbackScore", [None])[0]),
            returns_accepted=item.get("returnsAccepted", ["false"])[0] == "true",
            listing_type=str(item.get("listingInfo", [{}])[0].get("listingType", [None])[0]),
            start_time=str(item.get("listingInfo", [{}])[0].get("startTime", [None])[0]),
            end_time=str(item.get("listingInfo", [{}])[0].get("endTime", [None])[0]),
            location=str(item.get("location", [None])[0]),
            image_url=str(item.get("galleryURL", [None])[0]),
            raw_json={
                **item,
                "source": "api",
                "shipping_missing": shipping_missing,
                "assumed_shipping_gbp": assumed_shipping,
            },
        )

    def _build_api_params(self, criteria: Any, page: int, limit: int) -> dict[str, Any]:
        params = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": self.client.app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": criteria.query,
            "paginationInput.entriesPerPage": limit,
            "paginationInput.pageNumber": page,
            "sortOrder": "StartTimeNewest",
            "GLOBAL-ID": "EBAY-GB",
        }
        item_filters = []
        if criteria.category_id:
            params["categoryId"] = criteria.category_id
        if criteria.condition:
            item_filters.append(("Condition", criteria.condition))
        if criteria.listing_type and criteria.listing_type != "any":
            item_filters.append(
                ("ListingType", "Auction" if criteria.listing_type == "auction" else "FixedPrice")
            )
        if item_filters:
            for idx, (name, value) in enumerate(item_filters):
                params[f"itemFilter({idx}).name"] = name
                params[f"itemFilter({idx}).value"] = value
        return params

    def _criteria_to_target(self, criteria: Any, target: Target) -> Target:
        return Target(
            id=target.id,
            name=target.name,
            query=criteria.query,
            category_id=criteria.category_id,
            condition=criteria.condition,
            max_buy_gbp=criteria.max_buy_gbp,
            shipping_max_gbp=criteria.shipping_max_gbp,
            listing_type=criteria.listing_type,
            country=target.country,
            enabled=target.enabled,
            created_at=target.created_at,
        )


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_api_total_pages(data: dict[str, Any]) -> Optional[int]:
    pagination = data.get("findItemsByKeywordsResponse", [{}])[0].get("paginationOutput", [{}])[0]
    total_pages = pagination.get("totalPages", [None])[0]
    try:
        return int(total_pages)
    except (TypeError, ValueError):
        return None
