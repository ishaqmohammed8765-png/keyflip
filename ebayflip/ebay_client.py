from __future__ import annotations

import random
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ebayflip import get_logger
from ebayflip.cache import CacheStore, CachedResponse
from ebayflip.config import RunSettings
from ebayflip.models import Listing, SoldComp, Target

LOGGER = get_logger()

FINDING_ENDPOINT = "https://svcs.ebay.com/services/search/FindingService/v1"
HTML_SEARCH_URL = "https://www.ebay.co.uk/sch/i.html"


class EbayClient:
    def __init__(self, settings: RunSettings, app_id: Optional[str] = None) -> None:
        self.settings = settings
        self.app_id = app_id
        self.request_count = 0
        self.cache = CacheStore(".cache/ebayflip_cache.sqlite", ttl_seconds=600)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; EbayFlipScanner/1.0; +https://www.ebay.co.uk)",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )

    def _request(self, url: str, params: Optional[dict[str, Any]] = None) -> requests.Response:
        delay = random.uniform(0.6, 1.4)
        time.sleep(delay)
        cache_key = url
        if params:
            cache_key = f"{url}?{urlencode(params, doseq=True)}"
        cached = self.cache.get(cache_key)
        if cached:
            return _cached_to_response(cached)
        response = self.session.get(url, params=params, timeout=20)
        response.raise_for_status()
        self.cache.set(cache_key, response)
        self.request_count += 1
        return response

    def search_active_listings(self, target: Target) -> list[Listing]:
        if self.app_id:
            try:
                return self._search_active_api(target)
            except Exception as exc:
                LOGGER.warning("API search failed, falling back to HTML: %s", exc)
        return self._search_active_html(target)

    def search_sold_comps(self, comp_query: str) -> list[SoldComp]:
        if self.app_id:
            try:
                return self._search_sold_api(comp_query)
            except Exception as exc:
                LOGGER.warning("API comps failed, falling back to HTML: %s", exc)
        return self._search_sold_html(comp_query)

    def _search_active_api(self, target: Target) -> list[Listing]:
        params = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": self.app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": target.query,
            "paginationInput.entriesPerPage": self.settings.scan_limit_per_target,
            "paginationInput.pageNumber": 1,
            "sortOrder": "StartTimeNewest",
            "GLOBAL-ID": "EBAY-GB",
        }
        item_filters = []
        if target.category_id:
            params["categoryId"] = target.category_id
        if target.condition:
            item_filters.append(("Condition", target.condition))
        if target.listing_type and target.listing_type != "any":
            item_filters.append(("ListingType", "Auction" if target.listing_type == "auction" else "FixedPrice"))
        if target.max_buy_gbp:
            item_filters.append(("MaxPrice", str(target.max_buy_gbp)))
        if item_filters:
            for idx, (name, value) in enumerate(item_filters):
                params[f"itemFilter({idx}).name"] = name
                params[f"itemFilter({idx}).value"] = value

        response = self._request(FINDING_ENDPOINT, params=params)
        data = response.json()
        items = (
            data.get("findItemsByKeywordsResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
        )
        listings: list[Listing] = []
        for item in items:
            listing = self._parse_api_item(item, target)
            if listing:
                listings.append(listing)
        return listings

    def _parse_api_item(self, item: dict[str, Any], target: Target) -> Optional[Listing]:
        price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
        currency = price_info.get("@currencyId", "GBP")
        price = float(price_info.get("__value__", 0.0))
        shipping_info = item.get("shippingInfo", [{}])[0]
        ship_price_info = shipping_info.get("shippingServiceCost", [{}])[0]
        shipping = float(ship_price_info.get("__value__", 0.0)) if ship_price_info else 0.0
        if not self._currency_allowed(currency):
            return None
        price_gbp, shipping_gbp = self._normalize_currency(price, shipping, currency)
        total = price_gbp + shipping_gbp
        if target.max_buy_gbp and total > target.max_buy_gbp:
            return None
        if target.shipping_max_gbp and shipping_gbp > target.shipping_max_gbp:
            return None
        ebay_item_id = str(item.get("itemId", [""])[0])
        if not ebay_item_id:
            return None
        listing = Listing(
            ebay_item_id=ebay_item_id,
            target_id=target.id or 0,
            title=str(item.get("title", [""])[0]),
            url=str(item.get("viewItemURL", [""])[0]),
            price_gbp=price_gbp,
            shipping_gbp=shipping_gbp,
            total_buy_gbp=total,
            condition=str(item.get("condition", [{}])[0].get("conditionDisplayName", [None])[0]),
            seller_feedback_pct=_safe_float(item.get("sellerInfo", [{}])[0].get("positiveFeedbackPercent", [None])[0]),
            seller_feedback_score=_safe_int(item.get("sellerInfo", [{}])[0].get("feedbackScore", [None])[0]),
            returns_accepted=item.get("returnsAccepted", ["false"])[0] == "true",
            listing_type=str(item.get("listingInfo", [{}])[0].get("listingType", [None])[0]),
            start_time=str(item.get("listingInfo", [{}])[0].get("startTime", [None])[0]),
            end_time=str(item.get("listingInfo", [{}])[0].get("endTime", [None])[0]),
            location=str(item.get("location", [None])[0]),
            image_url=str(item.get("galleryURL", [None])[0]),
            raw_json=item,
        )
        return listing

    def _search_sold_api(self, comp_query: str) -> list[SoldComp]:
        params = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": self.app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": comp_query,
            "paginationInput.entriesPerPage": self.settings.comps_limit,
            "paginationInput.pageNumber": 1,
            "GLOBAL-ID": "EBAY-GB",
        }
        params["itemFilter(0).name"] = "SoldItemsOnly"
        params["itemFilter(0).value"] = "true"
        response = self._request(FINDING_ENDPOINT, params=params)
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
            if not self._currency_allowed(currency):
                continue
            price_gbp, _ = self._normalize_currency(price, 0.0, currency)
            comps.append(
                SoldComp(
                    price_gbp=price_gbp,
                    title=str(item.get("title", [""])[0]),
                    url=str(item.get("viewItemURL", [""])[0]),
                )
            )
        return comps

    def _search_active_html(self, target: Target) -> list[Listing]:
        params = {
            "_nkw": target.query,
            "_sop": "10",
        }
        if target.category_id:
            params["_sacat"] = target.category_id
        response = self._request(HTML_SEARCH_URL, params=params)
        soup = BeautifulSoup(response.text, "lxml")
        items = soup.select("li.s-item")
        listings: list[Listing] = []
        for item in items[: self.settings.scan_limit_per_target]:
            title_el = item.select_one("h3.s-item__title")
            link_el = item.select_one("a.s-item__link")
            price_el = item.select_one("span.s-item__price")
            if not title_el or not link_el or not price_el:
                continue
            title = title_el.get_text(strip=True)
            if title.lower() == "shop on ebay":
                continue
            url = link_el.get("href") or ""
            ebay_item_id = _extract_item_id(url)
            if not ebay_item_id:
                continue
            price_value, currency = _parse_price(price_el.get_text())
            if not self._currency_allowed(currency):
                continue
            shipping_el = item.select_one("span.s-item__shipping")
            shipping_value, shipping_currency = _parse_price(shipping_el.get_text()) if shipping_el else (0.0, "GBP")
            if shipping_currency != currency and shipping_currency:
                shipping_value = 0.0
            price_gbp, shipping_gbp = self._normalize_currency(price_value, shipping_value, currency)
            total = price_gbp + shipping_gbp
            if target.max_buy_gbp and total > target.max_buy_gbp:
                continue
            if target.shipping_max_gbp and shipping_gbp > target.shipping_max_gbp:
                continue
            listing = Listing(
                ebay_item_id=ebay_item_id,
                target_id=target.id or 0,
                title=title,
                url=url,
                price_gbp=price_gbp,
                shipping_gbp=shipping_gbp,
                total_buy_gbp=total,
                listing_type=_infer_listing_type(item),
                location=_get_text(item.select_one("span.s-item__location")),
                image_url=item.select_one("img") and item.select_one("img").get("src"),
                raw_json={"source": "html"},
            )
            listings.append(listing)
        return listings

    def _search_sold_html(self, comp_query: str) -> list[SoldComp]:
        params = {
            "_nkw": comp_query,
            "LH_Sold": "1",
            "LH_Complete": "1",
            "_sop": "13",
        }
        response = self._request(HTML_SEARCH_URL, params=params)
        soup = BeautifulSoup(response.text, "lxml")
        comps: list[SoldComp] = []
        for item in soup.select("li.s-item")[: self.settings.comps_limit]:
            title_el = item.select_one("h3.s-item__title")
            link_el = item.select_one("a.s-item__link")
            price_el = item.select_one("span.s-item__price")
            if not title_el or not price_el:
                continue
            title = title_el.get_text(strip=True)
            if title.lower() == "shop on ebay":
                continue
            price_value, currency = _parse_price(price_el.get_text())
            if not self._currency_allowed(currency):
                continue
            price_gbp, _ = self._normalize_currency(price_value, 0.0, currency)
            comps.append(
                SoldComp(
                    price_gbp=price_gbp,
                    title=title,
                    url=link_el.get("href") if link_el else None,
                )
            )
        return comps

    def _currency_allowed(self, currency: str) -> bool:
        if currency in self.settings.currency_whitelist:
            return True
        return self.settings.allow_non_gbp

    def _normalize_currency(self, price: float, shipping: float, currency: str) -> tuple[float, float]:
        if currency == "GBP":
            return price, shipping
        rate = self.settings.gbp_exchange_rate
        return price * rate, shipping * rate


def _get_text(el: Optional[Any]) -> Optional[str]:
    return el.get_text(strip=True) if el else None


def _infer_listing_type(item: Any) -> Optional[str]:
    bids = item.select_one("span.s-item__bids")
    if bids:
        return "auction"
    purchase = item.select_one("span.s-item__purchase-options")
    if purchase and "Buy It Now" in purchase.get_text():
        return "bin"
    return None


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


def _parse_price(text: str) -> tuple[float, str]:
    if not text:
        return 0.0, "GBP"
    cleaned = text.replace(",", "").strip()
    currency = "GBP"
    if "US" in cleaned or "$" in cleaned:
        currency = "USD"
    if "EUR" in cleaned or "€" in cleaned:
        currency = "EUR"
    cleaned = (
        cleaned.replace("£", "")
        .replace("US $", "")
        .replace("$", "")
        .replace("EUR", "")
        .replace("€", "")
    )
    for token in ["to", "-", "per", "each"]:
        if token in cleaned:
            cleaned = cleaned.split(token)[0]
    cleaned = cleaned.strip()
    value = 0.0
    try:
        value = float(cleaned)
    except ValueError:
        value = 0.0
    return value, currency


def _extract_item_id(url: str) -> str:
    if not url:
        return ""
    parts = url.split("/")
    for part in parts[::-1]:
        if part.isdigit():
            return part
    return url.split("?")[-1]


def _cached_to_response(cached: CachedResponse) -> requests.Response:
    response = requests.Response()
    response.status_code = cached.status_code
    response._content = cached.text.encode("utf-8")
    response.headers = cached.headers
    return response
