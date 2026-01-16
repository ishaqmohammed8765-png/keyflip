from __future__ import annotations

import dataclasses
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

from ebayflip import get_logger
from ebayflip.cache import CacheStore, CachedResponse
from ebayflip.config import RunSettings
from ebayflip.filtering import filter_listings
from ebayflip.models import Listing, SoldComp, Target

LOGGER = get_logger()

FINDING_ENDPOINT = "https://svcs.ebay.com/services/search/FindingService/v1"
HTML_SEARCH_URL = "https://www.ebay.co.uk/sch/i.html"


class RequestLimitError(RuntimeError):
    pass


@dataclass(slots=True)
class SearchAttemptLog:
    mode: str
    query: str
    category_id: Optional[str]
    condition: Optional[str]
    price_filters: dict[str, Optional[float]]
    pagination: dict[str, int]
    http_status: Optional[int]
    raw_count: int
    filtered_count: int
    request_url: Optional[str]


@dataclass(slots=True)
class SearchResult:
    listings: list[Listing]
    retry_report: list[str]
    diagnostics: list[SearchAttemptLog]
    rejection_counts: dict[str, int]
    raw_count: int
    filtered_count: int
    last_request_url: Optional[str]


@dataclass(slots=True)
class SearchCriteria:
    query: str
    category_id: Optional[str]
    condition: Optional[str]
    max_buy_gbp: Optional[float]
    shipping_max_gbp: Optional[float]
    listing_type: str


class EbayClient:
    def __init__(self, settings: RunSettings, app_id: Optional[str] = None) -> None:
        self.settings = settings
        self.app_id = app_id
        self.request_count = 0
        self.request_cap_reached = False
        self.cache = CacheStore(".cache/ebayflip_cache.sqlite", ttl_seconds=300)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; EbayFlipScanner/1.0; +https://www.ebay.co.uk)",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )

    def _request(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        *,
        delay: bool = False,
    ) -> tuple[requests.Response, bool]:
        cache_key = url
        if params:
            cache_key = f"{url}?{urlencode(params, doseq=True)}"
        cached = self.cache.get(cache_key)
        if cached:
            return _cached_to_response(cached, cache_key), True
        max_attempts = 3
        response: Optional[requests.Response] = None
        for attempt in range(max_attempts):
            if self.request_count >= self.settings.request_cap:
                self.request_cap_reached = True
                raise RequestLimitError("Request cap reached.")
            if delay:
                time.sleep(random.uniform(0.6, 1.4))
            response = self.session.get(url, params=params, timeout=20)
            self.request_count += 1
            status = response.status_code
            if status in {429} or 500 <= status <= 599:
                if attempt < max_attempts - 1:
                    backoff = (2**attempt) + random.uniform(0.1, 0.6)
                    time.sleep(backoff)
                    continue
            response.raise_for_status()
            self.cache.set(cache_key, response)
            return response, False
        if response is not None:
            response.raise_for_status()
        raise requests.HTTPError("Request failed before receiving a response.")

    def search_active_listings(self, target: Target) -> SearchResult:
        try:
            if self.app_id:
                try:
                    return self._search_active_with_retry(target, mode="api")
                except RequestLimitError as exc:
                    LOGGER.info("Request cap reached during API listing search: %s", exc)
                    return _empty_search_result()
                except Exception as exc:
                    LOGGER.warning("API search failed, falling back to HTML: %s", exc)
            return self._search_active_with_retry(target, mode="html")
        except RequestLimitError as exc:
            LOGGER.info("Request cap reached during HTML listing search: %s", exc)
            return _empty_search_result()
        except requests.RequestException as exc:
            LOGGER.warning("Active listing search failed: %s", exc)
            return _empty_search_result()
        except Exception:
            LOGGER.exception("Unexpected error while searching active listings.")
            return _empty_search_result()

    def search_sold_comps(self, comp_query: str) -> list[SoldComp]:
        try:
            if self.app_id:
                try:
                    return self._search_sold_api(comp_query)
                except RequestLimitError as exc:
                    LOGGER.info("Request cap reached during API comps search: %s", exc)
                    return []
                except Exception as exc:
                    LOGGER.warning("API comps failed, falling back to HTML: %s", exc)
            return self._search_sold_html(comp_query)
        except RequestLimitError as exc:
            LOGGER.info("Request cap reached during HTML comps search: %s", exc)
            return []
        except requests.RequestException as exc:
            LOGGER.warning("Sold comps search failed: %s", exc)
            return []
        except Exception:
            LOGGER.exception("Unexpected error while searching sold comps.")
            return []

    def _search_active_with_retry(self, target: Target, mode: str) -> SearchResult:
        base = SearchCriteria(
            query=target.query,
            category_id=target.category_id,
            condition=target.condition,
            max_buy_gbp=target.max_buy_gbp,
            shipping_max_gbp=target.shipping_max_gbp,
            listing_type=target.listing_type,
        )
        steps = _build_retry_steps(base)

        retry_report: list[str] = []
        diagnostics: list[SearchAttemptLog] = []
        last_rejections: dict[str, int] = {}
        last_raw_count = 0
        last_filtered_count = 0
        last_request_url: Optional[str] = None

        for idx, (label, criteria) in enumerate(steps):
            if idx > 0:
                retry_report.append(label)
            if mode == "api":
                outcome = self._search_active_api(criteria, target, diagnostics)
            else:
                outcome = self._search_active_html(criteria, target, diagnostics)
            last_rejections = outcome.rejection_counts
            last_raw_count = outcome.raw_count
            last_filtered_count = outcome.filtered_count
            last_request_url = outcome.last_request_url
            if outcome.listings:
                return SearchResult(
                    listings=outcome.listings,
                    retry_report=retry_report,
                    diagnostics=diagnostics,
                    rejection_counts=last_rejections,
                    raw_count=last_raw_count,
                    filtered_count=last_filtered_count,
                    last_request_url=last_request_url,
                )
            if self.request_cap_reached:
                break

        return SearchResult(
            listings=[],
            retry_report=retry_report,
            diagnostics=diagnostics,
            rejection_counts=last_rejections,
            raw_count=last_raw_count,
            filtered_count=last_filtered_count,
            last_request_url=last_request_url,
        )

    def _search_active_api(
        self,
        criteria: SearchCriteria,
        target: Target,
        diagnostics: list[SearchAttemptLog],
    ) -> SearchResult:
        limit = self.settings.scan_limit_per_target
        page = 1
        listings: list[Listing] = []
        rejection_counts: Counter[str] = Counter()
        total_raw = 0
        total_filtered = 0
        last_request_url: Optional[str] = None
        total_pages = 1

        while True:
            params = self._build_api_params(criteria, page, limit)
            response, _ = self._request(FINDING_ENDPOINT, params=params, delay=False)
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
            filtered = filter_listings(raw_listings, _criteria_to_target(criteria, target), self.settings)
            listings.extend(filtered.listings)
            total_filtered += len(filtered.listings)
            for reason, count in filtered.rejection_counts.items():
                rejection_counts[reason] += count
            total_pages = _parse_api_total_pages(data) or 1
            last_request_url = response.url
            diagnostics.append(
                self._build_log(
                    mode="api",
                    criteria=criteria,
                    page=page,
                    limit=limit,
                    status=response.status_code,
                    raw_count=len(raw_listings),
                    filtered_count=len(filtered.listings),
                    request_url=response.url,
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

        return SearchResult(
            listings=listings,
            retry_report=[],
            diagnostics=diagnostics,
            rejection_counts=dict(rejection_counts),
            raw_count=total_raw,
            filtered_count=total_filtered,
            last_request_url=last_request_url,
        )

    def _parse_api_item(self, item: dict[str, Any], target: Target) -> Optional[Listing]:
        price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
        currency = price_info.get("@currencyId", "GBP")
        price = float(price_info.get("__value__", 0.0))
        shipping_info = item.get("shippingInfo", [{}])[0]
        ship_price_info = shipping_info.get("shippingServiceCost", [{}])[0]
        shipping_missing = not ship_price_info
        shipping = float(ship_price_info.get("__value__", 0.0)) if ship_price_info else 0.0
        if not self._currency_allowed(currency):
            return None
        price_gbp, shipping_gbp = self._normalize_currency(price, shipping, currency)
        total = price_gbp + shipping_gbp
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
            raw_json={**item, "source": "api", "shipping_missing": shipping_missing},
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
        response, _ = self._request(FINDING_ENDPOINT, params=params)
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

    def _search_active_html(
        self,
        criteria: SearchCriteria,
        target: Target,
        diagnostics: list[SearchAttemptLog],
    ) -> SearchResult:
        limit = self.settings.scan_limit_per_target
        params = {
            "_nkw": criteria.query,
            "_sop": "10",
            "_pgn": 1,
        }
        response, _ = self._request(HTML_SEARCH_URL, params=params, delay=True)
        soup = BeautifulSoup(response.text, "lxml")
        items = soup.select("li.s-item")
        raw_listings: list[Listing] = []
        for item in items:
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
            shipping_missing = shipping_el is None
            shipping_value, shipping_currency = _parse_price(shipping_el.get_text()) if shipping_el else (0.0, "GBP")
            if shipping_currency != currency and shipping_currency:
                shipping_value = 0.0
            price_gbp, shipping_gbp = self._normalize_currency(price_value, shipping_value, currency)
            total = price_gbp + shipping_gbp
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
                image_url=_get_image_url(item),
                raw_json={"source": "html", "shipping_missing": shipping_missing},
            )
            raw_listings.append(listing)

        filtered = filter_listings(raw_listings, _criteria_to_target(criteria, target), self.settings)
        listings = filtered.listings[:limit]
        diagnostics.append(
            self._build_log(
                mode="html",
                criteria=criteria,
                page=1,
                limit=limit,
                status=response.status_code,
                raw_count=len(raw_listings),
                filtered_count=len(filtered.listings),
                request_url=response.url,
            )
        )
        return SearchResult(
            listings=listings,
            retry_report=[],
            diagnostics=diagnostics,
            rejection_counts=filtered.rejection_counts,
            raw_count=len(raw_listings),
            filtered_count=len(filtered.listings),
            last_request_url=response.url,
        )

    def _search_sold_html(self, comp_query: str) -> list[SoldComp]:
        params = {
            "_nkw": comp_query,
            "LH_Sold": "1",
            "LH_Complete": "1",
            "_sop": "13",
        }
        response, _ = self._request(HTML_SEARCH_URL, params=params, delay=True)
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

    def _build_api_params(self, criteria: SearchCriteria, page: int, limit: int) -> dict[str, Any]:
        params = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": self.app_id,
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

    def _build_log(
        self,
        *,
        mode: str,
        criteria: SearchCriteria,
        page: int,
        limit: int,
        status: Optional[int],
        raw_count: int,
        filtered_count: int,
        request_url: Optional[str],
    ) -> SearchAttemptLog:
        price_filters = {
            "max_buy_gbp": criteria.max_buy_gbp,
            "shipping_max_gbp": criteria.shipping_max_gbp,
            "total_max_gbp": _total_max(criteria.max_buy_gbp, criteria.shipping_max_gbp),
        }
        log = SearchAttemptLog(
            mode=mode,
            query=criteria.query,
            category_id=criteria.category_id,
            condition=criteria.condition,
            price_filters=price_filters,
            pagination={"page": page, "limit": limit},
            http_status=status,
            raw_count=raw_count,
            filtered_count=filtered_count,
            request_url=request_url,
        )
        LOGGER.info(
            "eBay search [%s] query=%s category=%s condition=%s price_filters=%s page=%s limit=%s status=%s raw=%s filtered=%s url=%s",
            mode,
            criteria.query,
            criteria.category_id,
            criteria.condition,
            price_filters,
            page,
            limit,
            status,
            raw_count,
            filtered_count,
            request_url,
        )
        return log


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
    match = re.search(r"/(\d{9,})", url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("item", "itm", "itemId"):
        if key in query and query[key]:
            return query[key][0]
    return ""


def _get_image_url(item: Any) -> Optional[str]:
    image_el = item.select_one("img")
    if not image_el:
        return None
    return image_el.get("src") or image_el.get("data-src")


def _cached_to_response(cached: CachedResponse, url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = cached.status_code
    response._content = cached.text.encode("utf-8")
    response.headers = cached.headers
    response.url = url
    return response


def _parse_api_total_pages(data: dict[str, Any]) -> Optional[int]:
    pagination = data.get("findItemsByKeywordsResponse", [{}])[0].get("paginationOutput", [{}])[0]
    total_pages = pagination.get("totalPages", [None])[0]
    try:
        return int(total_pages)
    except (TypeError, ValueError):
        return None


def _criteria_to_target(criteria: SearchCriteria, target: Target) -> Target:
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


def _broaden_query(query: str) -> str:
    if not query:
        return query
    cleaned = re.sub(r'(["\']).*?\1', "", query)
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


def _build_retry_steps(base: SearchCriteria) -> list[tuple[str, SearchCriteria]]:
    steps: list[tuple[str, SearchCriteria]] = [("initial", base)]
    if base.category_id:
        steps.append(("removed category filter", dataclasses.replace(base, category_id=None)))
    if base.condition:
        steps.append(("removed condition filter", dataclasses.replace(base, condition=None)))
    if base.max_buy_gbp is not None or base.shipping_max_gbp is not None:
        steps.append(
            (
                "removed price filters",
                dataclasses.replace(base, max_buy_gbp=None, shipping_max_gbp=None),
            )
        )
    widened_query = _broaden_query(base.query)
    if widened_query and widened_query != base.query:
        steps.append(
            (
                f"broadened query from '{base.query}' to '{widened_query}'",
                dataclasses.replace(base, query=widened_query),
            )
        )
    return steps


def _total_max(max_buy: Optional[float], shipping_max: Optional[float]) -> Optional[float]:
    if max_buy is None and shipping_max is None:
        return None
    if max_buy is None:
        return shipping_max
    if shipping_max is None:
        return max_buy
    return max_buy + shipping_max


def _empty_search_result() -> SearchResult:
    return SearchResult(
        listings=[],
        retry_report=[],
        diagnostics=[],
        rejection_counts={},
        raw_count=0,
        filtered_count=0,
        last_request_url=None,
    )
