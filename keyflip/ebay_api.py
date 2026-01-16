from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from .config import RunConfig
from .ebay_html import EbayHtmlClient

log = logging.getLogger("keyflip.ebay_api")

FINDING_API_URL = "https://svcs.ebay.com/services/search/FindingService/v1"


@dataclass(frozen=True)
class EbayListing:
    listing_id: str
    title: str
    listing_url: str
    price: Optional[float]
    shipping: Optional[float]
    currency: Optional[str]
    condition: str
    end_time: Optional[str]
    start_time: Optional[str]
    seller_feedback: Optional[str]
    location: Optional[str]
    listing_type: str
    buy_it_now: bool


@dataclass(frozen=True)
class SoldComp:
    price: Optional[float]
    currency: Optional[str]
    end_time: Optional[str]


class RateLimiter:
    def __init__(self, rate_limit_per_min: int) -> None:
        self._min_interval_s = 0.0
        if rate_limit_per_min > 0:
            self._min_interval_s = 60.0 / float(rate_limit_per_min)
        self._last_call = 0.0

    def wait(self) -> None:
        if self._min_interval_s <= 0:
            return
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)
        self._last_call = time.time()


class EbayApiClient:
    def __init__(self, cfg: RunConfig) -> None:
        self._cfg = cfg
        self._app_id = cfg.resolved_app_id()
        self._session = requests.Session()
        self._limiter = RateLimiter(cfg.rate_limit_per_min)
        self._html_client = EbayHtmlClient(self._session, timeout_s=cfg.request_timeout_s)

    def search_listings(
        self,
        *,
        query_text: str,
        category_id: Optional[str],
        condition: Optional[str],
        max_entries: int,
    ) -> list[EbayListing]:
        if not self._app_id:
            return self._from_html_listings(
                self._html_client.search_listings(
                    query_text=query_text,
                    category_id=category_id,
                    condition=condition,
                    limit=max_entries,
                    prefer_newly_listed=self._cfg.prefer_newly_listed,
                )
            )

        params = self._build_params(
            op="findItemsAdvanced",
            query_text=query_text,
            category_id=category_id,
            condition=condition,
            max_entries=max_entries,
            sold=False,
        )
        data = self._request(params)
        return self._parse_listings(data)

    def fetch_sold_comps(
        self,
        *,
        query_text: str,
        category_id: Optional[str],
        condition: Optional[str],
        max_entries: int,
    ) -> list[SoldComp]:
        if not self._app_id:
            return self._from_html_comps(
                self._html_client.fetch_sold_comps(
                    query_text=query_text,
                    category_id=category_id,
                    condition=condition,
                    limit=max_entries,
                )
            )

        params = self._build_params(
            op="findCompletedItems",
            query_text=query_text,
            category_id=category_id,
            condition=condition,
            max_entries=max_entries,
            sold=True,
        )
        data = self._request(params)
        return self._parse_sold_comps(data)

    def _build_params(
        self,
        *,
        op: str,
        query_text: str,
        category_id: Optional[str],
        condition: Optional[str],
        max_entries: int,
        sold: bool,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "OPERATION-NAME": op,
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self._app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "GLOBAL-ID": self._cfg.ebay_global_id,
            "paginationInput.entriesPerPage": str(max_entries),
            "keywords": query_text,
        }
        if category_id:
            params["categoryId"] = category_id

        item_filters: list[tuple[str, str]] = []
        if self._cfg.prefer_buy_it_now:
            item_filters.append(("ListingType", "FixedPrice"))
        if condition:
            item_filters.append(("Condition", condition))
        if sold:
            item_filters.append(("SoldItemsOnly", "true"))

        for idx, (name, value) in enumerate(item_filters):
            params[f"itemFilter({idx}).name"] = name
            params[f"itemFilter({idx}).value"] = value

        if self._cfg.prefer_newly_listed and not sold:
            params["sortOrder"] = "StartTimeNewest"

        return params

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._limiter.wait()
        for attempt in range(self._cfg.max_retries + 1):
            try:
                resp = self._session.get(
                    FINDING_API_URL,
                    params=params,
                    timeout=self._cfg.request_timeout_s,
                    headers={"User-Agent": "Keyflip/1.0"},
                )
                if resp.status_code in {429, 500, 502, 503, 504}:
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, json.JSONDecodeError) as exc:
                if attempt >= self._cfg.max_retries:
                    raise
                delay = min(
                    self._cfg.backoff_max_s,
                    self._cfg.backoff_base_s * (2 ** attempt) + random.uniform(0, 0.4),
                )
                log.warning("eBay API error (%s). retrying in %.2fs", exc, delay)
                time.sleep(delay)
        return {}

    def _parse_listings(self, data: Dict[str, Any]) -> list[EbayListing]:
        items = _extract_items(data)
        listings: list[EbayListing] = []
        for item in items:
            listing_id = _first(item.get("itemId"))
            title = _first(item.get("title"))
            listing_url = _first(item.get("viewItemURL"))
            price, currency = _parse_price(item, path=["sellingStatus", "currentPrice"])
            shipping, _ = _parse_price(item, path=["shippingInfo", "shippingServiceCost"])
            condition = _first(item.get("condition", [{}])[0].get("conditionDisplayName"))
            listing_info = item.get("listingInfo", [{}])[0]
            listing_type = _first(listing_info.get("listingType"))
            end_time = _first(listing_info.get("endTime"))
            start_time = _first(listing_info.get("startTime"))
            seller_feedback = _first(item.get("sellerInfo", [{}])[0].get("feedbackScore"))
            location = _first(item.get("location"))
            buy_it_now = listing_type in {"FixedPrice", "StoreInventory"}
            listings.append(
                EbayListing(
                    listing_id=listing_id,
                    title=title,
                    listing_url=listing_url,
                    price=price,
                    shipping=shipping,
                    currency=currency,
                    condition=condition,
                    end_time=end_time,
                    start_time=start_time,
                    seller_feedback=seller_feedback,
                    location=location,
                    listing_type=listing_type,
                    buy_it_now=buy_it_now,
                )
            )
        return listings

    def _parse_sold_comps(self, data: Dict[str, Any]) -> list[SoldComp]:
        items = _extract_items(data)
        comps: list[SoldComp] = []
        for item in items:
            price, currency = _parse_price(item, path=["sellingStatus", "currentPrice"])
            end_time = _first(item.get("listingInfo", [{}])[0].get("endTime"))
            comps.append(SoldComp(price=price, currency=currency, end_time=end_time))
        return comps

    @staticmethod
    def _from_html_listings(listings: list[Any]) -> list[EbayListing]:
        results: list[EbayListing] = []
        for item in listings:
            results.append(
                EbayListing(
                    listing_id=item.listing_id,
                    title=item.title,
                    listing_url=item.listing_url,
                    price=item.price,
                    shipping=item.shipping,
                    currency=item.currency,
                    condition=item.condition,
                    end_time=item.end_time,
                    start_time=item.start_time,
                    seller_feedback=item.seller_feedback,
                    location=item.location,
                    listing_type=item.listing_type,
                    buy_it_now=item.buy_it_now,
                )
            )
        return results

    @staticmethod
    def _from_html_comps(comps: list[Any]) -> list[SoldComp]:
        results: list[SoldComp] = []
        for comp in comps:
            results.append(SoldComp(price=comp.price, currency=comp.currency, end_time=comp.end_time))
        return results


def _extract_items(data: Dict[str, Any]) -> list[Dict[str, Any]]:
    if not data:
        return []
    for key in ("findItemsAdvancedResponse", "findCompletedItemsResponse"):
        if key in data:
            response = data[key][0]
            search_result = response.get("searchResult", [{}])[0]
            return search_result.get("item", []) or []
    return []


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return "" if value is None else str(value)


def _parse_price(item: Dict[str, Any], *, path: list[str]) -> tuple[Optional[float], Optional[str]]:
    payload: Any = item
    for part in path:
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        payload = payload.get(part, {}) if isinstance(payload, dict) else {}
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return None, None
    value = payload.get("__value__")
    currency = payload.get("@currencyId")
    try:
        return (float(value) if value is not None else None), currency
    except (TypeError, ValueError):
        return None, currency


def parse_iso8601(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
