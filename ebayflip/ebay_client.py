from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

from ebayflip import get_logger
from ebayflip.cache import CacheStore, CachedResponse
from ebayflip.config import RunSettings
from ebayflip.filtering import filter_listings
from ebayflip.ebay_api_provider import EbayApiProvider
from ebayflip.models import Listing, SoldComp, Target

LOGGER = get_logger()

HTML_SEARCH_URL = "https://www.ebay.co.uk/sch/i.html"
DEFAULT_PLAYWRIGHT_BROWSERS_PATH = "/tmp/pw-browsers"
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
]
BOT_FAILURE_MODES = {"captcha", "bot protection"}
BLOCKED_TOKENS = [
    "pardon our interruption",
    "captcha",
    "verify you are human",
    "human verification",
    "robot check",
    "robot",
    "challenge",
    "splashui",
]
CHALLENGE_SELECTORS = [
    "form#px-captcha",
    "#px-captcha",
    "#captcha-container",
    "iframe[src*='captcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='recaptcha']",
    "div[id*='challenge']",
    "form[action*='captcha']",
]


class RequestLimitError(RuntimeError):
    pass


class BlockedError(RuntimeError):
    def __init__(self, blocked: "BlockedInfo") -> None:
        super().__init__(blocked.message)
        self.blocked = blocked


@dataclass(slots=True)
class SearchAttemptLog:
    mode: str
    query: str
    category_id: Optional[str]
    condition: Optional[str]
    listing_type: str
    price_filters: dict[str, Optional[float]]
    pagination: dict[str, int]
    http_status: Optional[int]
    raw_count: int
    filtered_count: int
    request_url: Optional[str]
    item_count: Optional[int] = None
    parsed_count: Optional[int] = None
    failure_mode: Optional[str] = None
    response_length: Optional[int] = None


@dataclass(slots=True)
class SearchResult:
    listings: list[Listing]
    retry_report: list[str]
    diagnostics: list[SearchAttemptLog]
    rejection_counts: dict[str, int]
    raw_count: int
    filtered_count: int
    last_request_url: Optional[str]
    status: str = "ok"
    blocked: Optional["BlockedInfo"] = None


@dataclass(frozen=True, slots=True)
class BlockedInfo:
    status: str
    reason: str
    url: Optional[str]
    debug_artifacts: list[str]
    message: str
    detail: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PlaywrightResult:
    html: Optional[str]
    blocked: Optional[BlockedInfo]
    debug_artifacts: list[str]


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
        self.api_provider = EbayApiProvider(self)
        self._apply_session_headers()

    def _apply_session_headers(self) -> None:
        user_agent = random.choice(USER_AGENTS)
        self.session.headers.update(_default_headers(user_agent))

    def _api_enabled(self) -> bool:
        return self.api_provider.enabled()

    def _refresh_session(self, reason: str) -> None:
        LOGGER.info("Refreshing HTTP session due to %s", reason)
        self.session = requests.Session()
        self._apply_session_headers()

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
        cache_key = url
        if params:
            cache_key = f"{url}?{urlencode(params, doseq=True)}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached:
                return _cached_to_response(cached, cache_key), True
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
            if store_cache:
                self.cache.set(cache_key, response)
            return response, False
        if response is not None:
            response.raise_for_status()
        raise requests.HTTPError("Request failed before receiving a response.")

    def search_active_listings(self, target: Target) -> SearchResult:
        try:
            if self._api_enabled():
                try:
                    return self._search_active_with_retry(target, mode="api")
                except RequestLimitError as exc:
                    LOGGER.info("Request cap reached during API listing search: %s", exc)
                    return _empty_search_result()
                except BlockedError:
                    raise
                except Exception as exc:
                    LOGGER.warning("API search failed, falling back to HTML: %s", exc)
            return self._search_active_with_retry(target, mode="html")
        except RequestLimitError as exc:
            LOGGER.info("Request cap reached during HTML listing search: %s", exc)
            return _empty_search_result()
        except BlockedError as exc:
            return SearchResult(
                listings=[],
                retry_report=[],
                diagnostics=[],
                rejection_counts={},
                raw_count=0,
                filtered_count=0,
                last_request_url=exc.blocked.url,
                status="blocked",
                blocked=exc.blocked,
            )
        except requests.RequestException as exc:
            LOGGER.warning("Active listing search failed: %s", exc)
            return _empty_search_result()
        except Exception:
            LOGGER.exception("Unexpected error while searching active listings.")
            return _empty_search_result()

    def search_sold_comps(self, comp_query: str) -> list[SoldComp]:
        try:
            if self._api_enabled():
                try:
                    return self.api_provider.search_sold_comps(comp_query)
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

        max_attempts = 1
        for idx, (label, criteria) in enumerate(steps[:max_attempts]):
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
            if outcome.status == "blocked":
                return SearchResult(
                    listings=[],
                    retry_report=retry_report,
                    diagnostics=diagnostics,
                    rejection_counts=last_rejections,
                    raw_count=last_raw_count,
                    filtered_count=last_filtered_count,
                    last_request_url=last_request_url,
                    status=outcome.status,
                    blocked=outcome.blocked,
                )
            if outcome.listings:
                return SearchResult(
                    listings=outcome.listings,
                    retry_report=retry_report,
                    diagnostics=diagnostics,
                    rejection_counts=last_rejections,
                    raw_count=last_raw_count,
                    filtered_count=last_filtered_count,
                    last_request_url=last_request_url,
                    status=outcome.status,
                    blocked=outcome.blocked,
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
            status="ok",
            blocked=None,
        )

    def _search_active_api(
        self,
        criteria: SearchCriteria,
        target: Target,
        diagnostics: list[SearchAttemptLog],
    ) -> SearchResult:
        outcome = self.api_provider.search_active_listings(criteria, target, diagnostics)
        return SearchResult(
            listings=outcome.listings,
            retry_report=[],
            diagnostics=diagnostics,
            rejection_counts=outcome.rejection_counts,
            raw_count=outcome.raw_count,
            filtered_count=outcome.filtered_count,
            last_request_url=outcome.last_request_url,
            status="ok",
            blocked=None,
        )

    def _search_active_html(
        self,
        criteria: SearchCriteria,
        target: Target,
        diagnostics: list[SearchAttemptLog],
        *,
        page: int = 1,
    ) -> SearchResult:
        limit = self.settings.scan_limit_per_target
        params = _build_html_params(criteria, page)
        response, cached = fetch_html(
            self,
            HTML_SEARCH_URL,
            params=params,
            delay=True,
            max_attempts=1,
        )
        response_text = response.text
        debug_path = _save_debug_html(response_text, prefix="ebay_search")
        failure_mode = _detect_failure_mode(response_text)
        response_length = len(response_text)
        redirect_chain = [resp.url for resp in response.history] if response.history else []
        LOGGER.info(
            "eBay HTML response status=%s url=%s cached=%s length=%s redirects=%s debug_html=%s",
            response.status_code,
            response.url,
            cached,
            response_length,
            redirect_chain,
            debug_path,
        )
        request_headers = dict(response.request.headers) if response.request else dict(self.session.headers)
        LOGGER.info("eBay HTML request headers=%s", request_headers)
        LOGGER.info("eBay HTML response headers=%s", _filter_headers(response.headers))
        if response_text:
            LOGGER.info("eBay HTML response snippet=%s", response_text[:2000])
        if failure_mode:
            LOGGER.warning("eBay HTML failure mode detected: %s", failure_mode)

        listing_container_present = _has_listing_container(response_text)
        soup = BeautifulSoup(response_text, "lxml")
        title = _get_text(soup.title)
        blocked_detail = _detect_blocked_detail(
            response.url,
            response_text,
            title=title,
            listing_container_present=listing_container_present,
        )
        if not blocked_detail and failure_mode in BOT_FAILURE_MODES:
            blocked_detail = "captcha" if failure_mode == "captcha" else "challenge"
        if blocked_detail:
            blocked_artifacts = [
                debug_path,
                _save_debug_metadata(
                    {"url": response.url, "title": title, "detail": blocked_detail},
                    prefix="ebay_search_blocked",
                ),
            ]
            blocked_info = _build_blocked_info(
                detail=blocked_detail,
                url=response.url,
                debug_artifacts=blocked_artifacts,
            )
            LOGGER.warning(
                "eBay HTML blocked detection: detail=%s url=%s artifacts=%s",
                blocked_detail,
                response.url,
                blocked_artifacts,
            )
            _log_blocked_summary(blocked_info)
            diagnostics.append(
                self._build_log(
                    mode="html",
                    criteria=criteria,
                    page=page,
                    limit=limit,
                    status=response.status_code,
                    raw_count=0,
                    filtered_count=0,
                    request_url=response.url,
                    item_count=0,
                    parsed_count=0,
                    failure_mode=blocked_detail,
                    response_length=response_length,
                )
            )
            return SearchResult(
                listings=[],
                retry_report=[],
                diagnostics=diagnostics,
                rejection_counts={},
                raw_count=0,
                filtered_count=0,
                last_request_url=response.url,
                status="blocked",
                blocked=blocked_info,
            )

        raw_listings, parse_metrics = parse_html(response_text, target, self)
        item_count = parse_metrics["card_count"]
        no_priced_listings = bool(raw_listings) and all(listing.price_gbp <= 0 for listing in raw_listings)
        LOGGER.info(
            "eBay HTML parse metrics cards=%s titles=%s links=%s prices=%s",
            parse_metrics["card_count"],
            parse_metrics["title_count"],
            parse_metrics["link_count"],
            parse_metrics["price_count"],
        )

        playwright_html: Optional[str] = None
        fallback_reasons: list[str] = []
        if failure_mode:
            fallback_reasons.append(f"failure_mode={failure_mode}")
        if not raw_listings:
            fallback_reasons.append("no_listings")
        if parse_metrics.get("price_count", 0) == 0:
            fallback_reasons.append("no_prices")
        if no_priced_listings:
            fallback_reasons.append("zero_prices")

        needs_playwright = _should_fallback_to_playwright(
            failure_mode,
            parse_metrics,
            raw_listings,
        ) or no_priced_listings
        if needs_playwright and _playwright_fallback_enabled(self.settings):
            LOGGER.info("Attempting Playwright fallback for eBay search. reasons=%s", fallback_reasons)
            playwright_result = fetch_with_playwright(response.url, self.session.headers)
            if playwright_result.blocked:
                diagnostics.append(
                    self._build_log(
                        mode="playwright",
                        criteria=criteria,
                        page=page,
                        limit=limit,
                        status=None,
                        raw_count=0,
                        filtered_count=0,
                        request_url=playwright_result.blocked.url or response.url,
                        item_count=0,
                        parsed_count=0,
                        failure_mode="captcha_or_challenge",
                        response_length=None,
                    )
                )
                return SearchResult(
                    listings=[],
                    retry_report=[],
                    diagnostics=diagnostics,
                    rejection_counts={},
                    raw_count=0,
                    filtered_count=0,
                    last_request_url=playwright_result.blocked.url or response.url,
                    status="blocked",
                    blocked=playwright_result.blocked,
                )
            playwright_html = playwright_result.html
            if playwright_html:
                debug_path = _save_debug_html(playwright_html, prefix="ebay_search_playwright")
                LOGGER.info("Playwright HTML saved to %s", debug_path)
                raw_listings, parse_metrics = parse_html(playwright_html, target, self)
                item_count = parse_metrics["card_count"]
                no_priced_listings = bool(raw_listings) and all(
                    listing.price_gbp <= 0 for listing in raw_listings
                )
                LOGGER.info(
                    "Playwright parse metrics cards=%s titles=%s links=%s prices=%s",
                    parse_metrics["card_count"],
                    parse_metrics["title_count"],
                    parse_metrics["link_count"],
                    parse_metrics["price_count"],
                )
        elif needs_playwright:
            LOGGER.warning("Playwright fallback disabled; returning HTML results only.")

        if not raw_listings or no_priced_listings:
            soup = BeautifulSoup(playwright_html or response_text, "lxml")
            raw_listings = _parse_json_ld_listings(soup, target, self)
            no_priced_listings = bool(raw_listings) and all(
                listing.price_gbp <= 0 for listing in raw_listings
            )
        if not raw_listings or no_priced_listings:
            raw_listings = _parse_initial_state_listings(soup, target, self)

        final_html = playwright_html or response_text
        final_container_present = _has_listing_container(final_html)
        if parse_metrics.get("price_count", 0) == 0 and not final_container_present:
            blocked_detail = "missing_prices_and_container"
        elif parse_metrics.get("price_count", 0) == 0 and (
            parse_metrics.get("title_count", 0) > 0 or parse_metrics.get("link_count", 0) > 0
        ):
            blocked_detail = "zero_prices"
        elif raw_listings and all(listing.price_gbp <= 0 for listing in raw_listings):
            blocked_detail = "zero_prices"
        else:
            blocked_detail = None
        if blocked_detail:
            blocked_artifacts = [
                _save_debug_html(final_html, prefix="ebay_zero_prices"),
                _save_debug_metadata(
                    {
                        "url": response.url,
                        "detail": blocked_detail,
                        "failure_mode": failure_mode,
                        "metrics": parse_metrics,
                    },
                    prefix="ebay_search_blocked",
                ),
            ]
            blocked_info = _build_blocked_info(
                detail=blocked_detail,
                url=response.url,
                debug_artifacts=blocked_artifacts,
            )
            LOGGER.warning(
                "eBay HTML blocked detection: detail=%s url=%s artifacts=%s",
                blocked_detail,
                response.url,
                blocked_artifacts,
            )
            _log_blocked_summary(blocked_info)
            diagnostics.append(
                self._build_log(
                    mode="html",
                    criteria=criteria,
                    page=page,
                    limit=limit,
                    status=response.status_code,
                    raw_count=0,
                    filtered_count=0,
                    request_url=response.url,
                    item_count=item_count,
                    parsed_count=0,
                    failure_mode=blocked_detail,
                    response_length=response_length,
                )
            )
            return SearchResult(
                listings=[],
                retry_report=[],
                diagnostics=diagnostics,
                rejection_counts={},
                raw_count=0,
                filtered_count=0,
                last_request_url=response.url,
                status="blocked",
                blocked=blocked_info,
            )

        filtered = filter_listings(raw_listings, _criteria_to_target(criteria, target), self.settings)
        listings = filtered.listings[:limit]
        diagnostics.append(
            self._build_log(
                mode="html",
                criteria=criteria,
                page=page,
                limit=limit,
                status=response.status_code,
                raw_count=len(raw_listings),
                filtered_count=len(filtered.listings),
                request_url=response.url,
                item_count=item_count,
                parsed_count=len(raw_listings),
                failure_mode=failure_mode,
                response_length=response_length,
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
            status="ok",
            blocked=None,
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
        if not comps:
            comps = _parse_json_ld_comps(soup, self)
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

    def _apply_missing_shipping(self, shipping_value: float, shipping_missing: bool) -> tuple[float, Optional[float]]:
        if shipping_missing and self.settings.allow_missing_shipping_price:
            assumed = self.settings.assumed_inbound_shipping_gbp
            return assumed, assumed
        return shipping_value, None

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
        item_count: Optional[int] = None,
        parsed_count: Optional[int] = None,
        failure_mode: Optional[str] = None,
        response_length: Optional[int] = None,
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
            listing_type=criteria.listing_type,
            price_filters=price_filters,
            pagination={"page": page, "limit": limit},
            http_status=status,
            raw_count=raw_count,
            filtered_count=filtered_count,
            request_url=request_url,
            item_count=item_count,
            parsed_count=parsed_count,
            failure_mode=failure_mode,
            response_length=response_length,
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


def build_url(
    query: str,
    *,
    page: int = 1,
    category: Optional[str] = None,
    condition: Optional[str] = None,
    listing_type: str = "any",
) -> str:
    params = {
        "_nkw": query,
        "_sop": "10",
        "_pgn": page,
    }
    if category:
        params["_sacat"] = category
    if condition:
        params["LH_ItemCondition"] = condition
    if listing_type and listing_type != "any":
        if listing_type == "auction":
            params["LH_Auction"] = "1"
        else:
            params["LH_BIN"] = "1"
    return f"{HTML_SEARCH_URL}?{urlencode(params)}"


def normalize_price(text: str) -> tuple[float, str]:
    if not text:
        return 0.0, "GBP"
    cleaned = text.strip()
    if "free" in cleaned.lower():
        return 0.0, "GBP"
    return _parse_price(cleaned)


def _parse_shipping_text(text: Optional[str]) -> tuple[float, str, bool]:
    if not text:
        return 0.0, "GBP", True
    lowered = text.lower()
    if "not specified" in lowered or "varies" in lowered or "calculate" in lowered:
        return 0.0, "GBP", True
    value, currency = normalize_price(text)
    missing = False
    if "free" in lowered:
        missing = False
    elif value == 0.0 and ("not specified" in lowered or "varies" in lowered):
        missing = True
    return value, currency, missing


def fetch_ebay_search(
    query: str,
    *,
    page: int = 1,
    category: Optional[str] = None,
    condition: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    settings = RunSettings(scan_limit_per_target=limit)
    client = EbayClient(settings)
    criteria = SearchCriteria(
        query=query,
        category_id=category,
        condition=condition,
        max_buy_gbp=None,
        shipping_max_gbp=None,
        listing_type="any",
    )
    target = Target(id=0, name=query, query=query, category_id=category, condition=condition)
    params = _build_html_params(criteria, page)
    response, _ = fetch_html(client, HTML_SEARCH_URL, params=params, delay=True, max_attempts=1)
    html = response.text
    failure_mode = _detect_failure_mode(html)
    listings, metrics = parse_html(html, target, client)
    playwright_html: Optional[str] = None
    no_priced_listings = bool(listings) and all(listing.price_gbp <= 0 for listing in listings)
    needs_playwright = _should_fallback_to_playwright(failure_mode, metrics, listings) or no_priced_listings
    if needs_playwright and _playwright_fallback_enabled(client.settings):
        playwright_result = fetch_with_playwright(response.url, client.session.headers)
        if playwright_result.blocked:
            LOGGER.warning(
                "Playwright blocked detection during fetch_ebay_search: %s",
                playwright_result.blocked.debug_artifacts,
            )
            return []
        playwright_html = playwright_result.html
        if playwright_html:
            listings, metrics = parse_html(playwright_html, target, client)
    if not listings:
        soup = BeautifulSoup(playwright_html or html, "lxml")
        listings = _parse_json_ld_listings(soup, target, client)
    if not listings:
        listings = _parse_initial_state_listings(soup, target, client)
    return [
        {
            "title": listing.title,
            "price_gbp": listing.price_gbp,
            "shipping_gbp": listing.shipping_gbp,
            "url": listing.url,
            "condition": listing.condition,
            "listing_type": listing.listing_type,
            "location": listing.location,
            "image_url": listing.image_url,
        }
        for listing in listings[:limit]
    ]


def fetch_html(
    client: EbayClient,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    delay: bool = False,
    use_cache: bool = True,
    store_cache: bool = True,
        max_attempts: int = 3,
) -> tuple[requests.Response, bool]:
    return client._request(
        url,
        params=params,
        delay=delay,
        use_cache=use_cache,
        store_cache=store_cache,
        max_attempts=max_attempts,
    )


def parse_html(
    html: str,
    target: Target,
    client: EbayClient,
) -> tuple[list[Listing], dict[str, int]]:
    soup = BeautifulSoup(html, "lxml")
    return _parse_html_listings(soup, target, client)


def _get_text(el: Optional[Any]) -> Optional[str]:
    return el.get_text(strip=True) if el else None


def _build_html_params(criteria: SearchCriteria, page: int) -> dict[str, Any]:
    params = {
        "_nkw": criteria.query,
        "_sop": "10",
        "_pgn": page,
    }
    if criteria.category_id:
        params["_sacat"] = criteria.category_id
    if criteria.condition:
        params["LH_ItemCondition"] = criteria.condition
    if criteria.listing_type and criteria.listing_type != "any":
        if criteria.listing_type == "auction":
            params["LH_Auction"] = "1"
        else:
            params["LH_BIN"] = "1"
    return params


def _filter_headers(headers: dict[str, str]) -> dict[str, str]:
    keep = {
        "content-type",
        "content-encoding",
        "cache-control",
        "set-cookie",
        "location",
        "server",
    }
    return {key: value for key, value in headers.items() if key.lower() in keep}


def _default_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": _accept_encoding_header(),
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Referer": "https://www.ebay.co.uk/",
    }


def _needs_bot_retry(response: requests.Response, failure_mode: Optional[str]) -> bool:
    if response.status_code in {403, 429}:
        return True
    if failure_mode in BOT_FAILURE_MODES:
        return True
    return False


def _accept_encoding_header() -> str:
    encodings = ["gzip", "deflate"]
    if importlib.util.find_spec("brotli") or importlib.util.find_spec("brotlicffi"):
        encodings.append("br")
    return ", ".join(encodings)


def _set_playwright_env_defaults() -> str:
    path = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if not path:
        path = DEFAULT_PLAYWRIGHT_BROWSERS_PATH
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
    return path


_set_playwright_env_defaults()


def _playwright_available() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except ModuleNotFoundError:
        return False


def _playwright_fallback_enabled(settings: RunSettings) -> bool:
    raw_value = os.getenv("EBAY_USE_PLAYWRIGHT")
    if raw_value is None:
        return settings.use_playwright_fallback
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _ensure_playwright_browsers_installed() -> bool:
    if not _playwright_available():
        LOGGER.warning("Playwright not installed; skipping browser fallback.")
        return False
    _set_playwright_env_defaults()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        LOGGER.warning("Playwright import failed: %s", exc)
        return False
    browser_path: Optional[Path] = None
    try:
        with sync_playwright() as playwright:
            browser_path = Path(playwright.chromium.executable_path)
    except Exception as exc:
        LOGGER.warning("Playwright browser detection failed: %s", exc)
    if browser_path and browser_path.exists():
        return True
    install_cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    LOGGER.info("Installing Playwright browsers with %s", " ".join(install_cmd))
    try:
        result = subprocess.run(
            install_cmd,
            check=True,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        if result.stdout:
            LOGGER.info("Playwright install stdout: %s", result.stdout.strip())
        if result.stderr:
            LOGGER.info("Playwright install stderr: %s", result.stderr.strip())
        return True
    except subprocess.CalledProcessError as exc:
        LOGGER.error(
            "Playwright browser install failed (exit=%s). stdout=%s stderr=%s",
            exc.returncode,
            (exc.stdout or "").strip(),
            (exc.stderr or "").strip(),
        )
    except Exception:
        LOGGER.exception("Playwright browser install failed.")
    return False


def _save_debug_html(text: str, *, prefix: str) -> str:
    debug_dir = Path(".cache/ebayflip_debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = debug_dir / f"{prefix}_{timestamp}.html"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _save_debug_metadata(payload: dict[str, Any], *, prefix: str) -> str:
    debug_dir = Path(".cache/ebayflip_debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = debug_dir / f"{prefix}_{timestamp}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def _save_debug_screenshot(page: Any, *, prefix: str) -> Optional[str]:
    debug_dir = Path(".cache/ebayflip_debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = debug_dir / f"{prefix}_{timestamp}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        LOGGER.exception("Failed to capture Playwright screenshot.")
        return None
    return str(path)


def safe_screenshot(page: Any, path: str) -> Optional[str]:
    try:
        if page and not page.is_closed():
            page.screenshot(path=path, full_page=True)
            return path
    except Exception:
        LOGGER.debug("Safe screenshot failed.", exc_info=True)
    return None


def safe_content(page: Any) -> Optional[str]:
    try:
        if page and not page.is_closed():
            return page.content()
    except Exception:
        LOGGER.debug("Safe content failed.", exc_info=True)
    return None


def _safe_page_title(page: Any) -> Optional[str]:
    try:
        if page and not page.is_closed():
            return page.title()
    except Exception:
        LOGGER.exception("Failed to read Playwright page title.")
    return None


def _safe_page_content(page: Any) -> Optional[str]:
    return safe_content(page)


def _safe_page_url(page: Any) -> Optional[str]:
    try:
        if page and not page.is_closed():
            return page.url
    except Exception:
        LOGGER.exception("Failed to read Playwright page URL.")
    return None


def safe_close(page: Any, context: Any, browser: Any) -> None:
    try:
        if page and not page.is_closed():
            page.close()
    except Exception:
        LOGGER.debug("Playwright page already closed or could not close.", exc_info=True)
    try:
        if context:
            context.close()
    except Exception:
        LOGGER.debug("Playwright context already closed or could not close.", exc_info=True)
    try:
        if browser:
            browser.close()
    except Exception:
        LOGGER.debug("Playwright browser already closed or could not close.", exc_info=True)


def _log_blocked_summary(blocked: BlockedInfo) -> None:
    LOGGER.warning(
        "eBay blocked summary blocked=%s reason=%s url=%s debug_artifacts=%s",
        True,
        blocked.reason,
        blocked.url,
        blocked.debug_artifacts,
    )


def _detect_failure_mode(text: str) -> Optional[str]:
    if not text:
        return "empty response"
    lowered = text.lower()
    patterns = {
        "captcha": [
            "captcha",
            "verify you are human",
            "human verification",
            "robot check",
            "recaptcha",
            "hcaptcha",
        ],
        "bot protection": [
            "access denied",
            "unusual traffic",
            "pardon our interruption",
            "akamai",
            "perimeterx",
            "incapsula",
            "blocked",
            "forbidden",
            "request blocked",
            "temporarily unavailable",
            "automated queries",
            "service unavailable",
            "suspicious activity",
        ],
        "consent wall": ["consent", "cookie", "privacy choices"],
        "js required": [
            "enable javascript",
            "please enable javascript",
            "javascript required",
            "please enable cookies",
            "turn on javascript",
        ],
    }
    for label, tokens in patterns.items():
        if any(token in lowered for token in tokens):
            return label
    if 'name="robots"' in lowered and ("noindex" in lowered or "nofollow" in lowered):
        return "robots meta"
    if "s-item" in lowered and "s-item__price" not in lowered and "srp" in lowered:
        return "missing price blocks"
    if "s-item" not in lowered and "srp" not in lowered and "search" in lowered:
        return "empty template"
    return None


def _is_challenge_url(url: Optional[str]) -> bool:
    if not url:
        return False
    lowered = url.lower()
    if "/splashui/challenge" in lowered:
        return True
    if "splashui" in lowered and "challenge" in lowered:
        return True
    return False


def _has_listing_container(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "lxml")
    return bool(soup.select_one("ul.srp-results, li.s-item, div.s-item__wrapper, div.s-item"))


def _detect_blocked_detail(
    url: Optional[str],
    html: str,
    *,
    title: Optional[str] = None,
    listing_container_present: Optional[bool] = None,
    price_count: Optional[int] = None,
) -> Optional[str]:
    if _is_challenge_url(url):
        return "splashui_challenge"
    lowered = html.lower() if html else ""
    title_lowered = title.lower() if title else ""
    if any(token in lowered for token in BLOCKED_TOKENS) or any(
        token in title_lowered for token in BLOCKED_TOKENS
    ):
        if any(token in lowered for token in ("captcha", "hcaptcha", "recaptcha")) or any(
            token in title_lowered for token in ("captcha", "hcaptcha", "recaptcha")
        ):
            return "captcha"
        return "challenge"
    if listing_container_present is False and price_count == 0:
        return "missing_prices_and_container"
    if listing_container_present is False:
        return "missing_listing_container"
    return None


def _blocked_reason_for_detail(detail: str) -> str:
    if detail == "captcha":
        return "captcha"
    if detail == "splashui_challenge":
        return "splashui_challenge"
    if detail in {"challenge", "missing_listing_container", "missing_prices_and_container", "zero_prices"}:
        return "splashui_challenge"
    return "splashui_challenge"


def _detect_blocked_from_metadata(url: Optional[str], title: Optional[str]) -> Optional[str]:
    if _is_challenge_url(url):
        return "splashui_challenge"
    title_lowered = title.lower() if title else ""
    if any(token in title_lowered for token in BLOCKED_TOKENS):
        if any(token in title_lowered for token in ("captcha", "hcaptcha", "recaptcha")):
            return "captcha"
        return "challenge"
    return None


def _build_blocked_info(
    *,
    detail: str,
    url: Optional[str],
    debug_artifacts: list[str],
) -> BlockedInfo:
    reason = _blocked_reason_for_detail(detail)
    return BlockedInfo(
        status="blocked",
        reason=reason,
        url=url,
        debug_artifacts=debug_artifacts,
        message="eBay served a human verification page; automated scraping is currently blocked.",
        detail=detail,
    )


def _capture_playwright_debug(page: Any, *, prefix: str) -> list[str]:
    artifacts: list[str] = []
    url = _safe_page_url(page)
    title = _safe_page_title(page)
    html = safe_content(page)
    if html:
        artifacts.append(_save_debug_html(html, prefix=prefix))
    screenshot_path = None
    if page and not page.is_closed():
        debug_dir = Path(".cache/ebayflip_debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        screenshot_path = safe_screenshot(page, str(debug_dir / f"{prefix}_{timestamp}.png"))
    if screenshot_path:
        artifacts.append(screenshot_path)
    metadata = {"url": url, "title": title}
    artifacts.append(_save_debug_metadata(metadata, prefix=prefix))
    return artifacts


def _safe_close_playwright(page: Any, context: Any, browser: Any) -> None:
    safe_close(page, context, browser)


def _should_fallback_to_playwright(
    failure_mode: Optional[str],
    parse_metrics: dict[str, int],
    raw_listings: list[Listing],
) -> bool:
    if failure_mode:
        return True
    if not raw_listings:
        return True
    if parse_metrics.get("price_count", 0) == 0:
        return True
    return False


def fetch_with_playwright(url: str, headers: dict[str, str]) -> PlaywrightResult:
    if not _ensure_playwright_browsers_installed():
        LOGGER.error("Playwright browser install missing or failed; skipping browser fallback.")
        return PlaywrightResult(html=None, blocked=None, debug_artifacts=[])
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    page = None
    browser = None
    context = None
    debug_artifacts: list[str] = []
    try:
        with sync_playwright() as playwright:
            user_agent = headers.get("User-Agent")
            launch_args = [
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
            if os.getenv("EBAY_NO_SANDBOX") == "1":
                launch_args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
            browser = playwright.chromium.launch(
                headless=True,
                args=launch_args,
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-GB",
                timezone_id="Europe/London",
                user_agent=user_agent,
            )
            page = context.new_page()
            page.set_extra_http_headers({key: value for key, value in headers.items() if key.lower() != "host"})
            time.sleep(random.uniform(0.2, 0.7))
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            current_url = _safe_page_url(page)
            current_title = _safe_page_title(page)
            metadata_blocked = _detect_blocked_from_metadata(current_url, current_title)
            if metadata_blocked:
                debug_artifacts = _capture_playwright_debug(page, prefix="ebay_playwright_blocked")
                blocked_info = _build_blocked_info(
                    detail=metadata_blocked,
                    url=current_url,
                    debug_artifacts=debug_artifacts,
                )
                LOGGER.warning(
                    "Playwright blocked detection: detail=%s url=%s artifacts=%s",
                    blocked_info.detail,
                    current_url,
                    debug_artifacts,
                )
                _log_blocked_summary(blocked_info)
                _safe_close_playwright(page, context, browser)
                return PlaywrightResult(html=None, blocked=blocked_info, debug_artifacts=debug_artifacts)
            listing_container_present = False
            block_selector_hit = False
            try:
                selector = "li.s-item, div.s-item__wrapper, div.s-item, ul.srp-results"
                challenge_selector = ",".join(CHALLENGE_SELECTORS)
                page.wait_for_selector(
                    f"{selector}, {challenge_selector}",
                    timeout=15000,
                )
            except PlaywrightTimeoutError:
                LOGGER.warning("Playwright wait timed out; continuing with captured HTML.")
            else:
                if page.query_selector(selector):
                    listing_container_present = True
                if challenge_selector and page.query_selector(challenge_selector):
                    block_selector_hit = True
            html = _safe_page_content(page) or ""
            blocked_detail = None
            if block_selector_hit:
                blocked_detail = "captcha"
            if not blocked_detail:
                blocked_detail = _detect_blocked_detail(
                    current_url,
                    html,
                    title=current_title,
                    listing_container_present=listing_container_present,
                )
            if blocked_detail:
                debug_artifacts = _capture_playwright_debug(page, prefix="ebay_playwright_blocked")
                blocked_info = _build_blocked_info(
                    detail=blocked_detail,
                    url=current_url,
                    debug_artifacts=debug_artifacts,
                )
                LOGGER.warning(
                    "Playwright blocked detection: detail=%s url=%s artifacts=%s",
                    blocked_detail,
                    current_url,
                    debug_artifacts,
                )
                _log_blocked_summary(blocked_info)
                _safe_close_playwright(page, context, browser)
                return PlaywrightResult(html=html or None, blocked=blocked_info, debug_artifacts=debug_artifacts)
            failure_mode = _detect_failure_mode(html)
            if failure_mode:
                debug_artifacts = _capture_playwright_debug(page, prefix="ebay_playwright_failure")
                LOGGER.warning(
                    "Playwright failure mode detected: %s artifacts=%s",
                    failure_mode,
                    debug_artifacts,
                )
            _safe_close_playwright(page, context, browser)
            return PlaywrightResult(html=html or None, blocked=None, debug_artifacts=debug_artifacts)
    except Exception:
        LOGGER.exception("Playwright fallback failed.")
        if page:
            debug_artifacts = _capture_playwright_debug(page, prefix="ebay_playwright_error")
            LOGGER.warning("Playwright failure debug saved artifacts=%s", debug_artifacts)
        _safe_close_playwright(page, context, browser)
        return PlaywrightResult(html=None, blocked=None, debug_artifacts=debug_artifacts)


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
    if "GBP" in cleaned or "£" in cleaned:
        currency = "GBP"
    cleaned = (
        cleaned.replace("£", "")
        .replace("US $", "")
        .replace("$", "")
        .replace("EUR", "")
        .replace("€", "")
    )
    cleaned = cleaned.replace("from", "")
    for token in ["to", "-", "per", "each"]:
        if token in cleaned:
            cleaned = cleaned.split(token)[0]
    cleaned = cleaned.strip()
    numbers = re.findall(r"(\d+(?:\.\d+)?)", cleaned)
    if not numbers:
        return 0.0, currency
    values = [float(value) for value in numbers]
    return min(values), currency


def _extract_item_id(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"/(\d{9,})", url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("item", "itm", "itemId", "itemid", "iid", "listingId", "listing_id"):
        if key in query and query[key]:
            for value in query[key]:
                if not value:
                    continue
                match = re.search(r"(\d{9,})", value)
                if match:
                    return match.group(1)
    return ""


def _get_image_url(item: Any) -> Optional[str]:
    image_el = item.select_one("img")
    if not image_el:
        return None
    return image_el.get("src") or image_el.get("data-src")


def _parse_html_listings(
    soup: BeautifulSoup, target: Target, client: EbayClient
) -> tuple[list[Listing], dict[str, int]]:
    cards = _extract_listing_cards(soup)
    metrics = {
        "card_count": len(cards),
        "title_count": 0,
        "link_count": 0,
        "price_count": 0,
    }
    listings: list[Listing] = []
    seen_ids: set[str] = set()
    for card in cards:
        title = _extract_listing_title(card)
        if title:
            metrics["title_count"] += 1
        if title and title.lower() == "shop on ebay":
            continue
        link = _extract_listing_link(card)
        if link:
            metrics["link_count"] += 1
        ebay_item_id = _extract_item_id(link) if link else ""
        if not ebay_item_id:
            ebay_item_id = _extract_item_id_from_card(card)
        if not ebay_item_id and link:
            ebay_item_id = _extract_item_id(link)
        if not link and ebay_item_id:
            link = f"https://www.ebay.co.uk/itm/{ebay_item_id}"
        if not ebay_item_id:
            continue
        if ebay_item_id in seen_ids:
            continue
        seen_ids.add(ebay_item_id)

        price_text = _extract_listing_price_text(card)
        if price_text:
            metrics["price_count"] += 1
        price_value, currency = normalize_price(price_text or "")
        if price_text and not client._currency_allowed(currency):
            continue

        shipping_text = _extract_listing_shipping_text(card)
        shipping_value, shipping_currency, shipping_missing = _parse_shipping_text(shipping_text)
        if shipping_text and shipping_currency and shipping_currency != currency:
            shipping_value = 0.0

        shipping_value, assumed_shipping = client._apply_missing_shipping(shipping_value, shipping_missing)
        price_gbp, shipping_gbp = client._normalize_currency(price_value, shipping_value, currency or "GBP")
        total = price_gbp + shipping_gbp
        condition = _extract_listing_condition(card)
        seller_feedback_pct, seller_feedback_score = _extract_seller_feedback(card)

        listing = Listing(
            ebay_item_id=ebay_item_id,
            target_id=target.id or 0,
            title=title or "Unknown title",
            url=link or "",
            price_gbp=price_gbp,
            shipping_gbp=shipping_gbp,
            total_buy_gbp=total,
            condition=condition,
            seller_feedback_pct=seller_feedback_pct,
            seller_feedback_score=seller_feedback_score,
            listing_type=_infer_listing_type(card),
            location=_get_text(card.select_one("span.s-item__location")),
            image_url=_get_image_url(card),
            raw_json={
                "source": "html",
                "shipping_missing": shipping_missing,
                "assumed_shipping_gbp": assumed_shipping,
                "price_text": price_text,
                "shipping_text": shipping_text,
            },
        )
        listings.append(listing)
    return listings, metrics


def _extract_listing_cards(soup: BeautifulSoup) -> list[Any]:
    selectors = ["li.s-item", "div.s-item__wrapper", "div.s-item"]
    cards: list[Any] = []
    for selector in selectors:
        cards.extend(soup.select(selector))
    if not cards:
        cards = soup.select("ul.srp-results > li")
    return cards


def _extract_listing_title(card: Any) -> Optional[str]:
    selectors = [
        "h3.s-item__title",
        "span.s-item__title",
        "div.s-item__title span",
        "span[role='heading']",
        "h3[role='heading']",
        "div[role='heading']",
        "*[data-testid='s-item__title']",
        "*[class*='s-item__title']",
    ]
    for selector in selectors:
        el = card.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    link_el = card.select_one("a.s-item__link")
    if link_el:
        for key in ("title", "aria-label"):
            value = link_el.get(key)
            if value:
                return value.strip()
    return None


def _extract_listing_link(card: Any) -> Optional[str]:
    link_el = card.select_one("a.s-item__link")
    if link_el and link_el.get("href"):
        return link_el.get("href")
    link_el = card.select_one("a[href*='/itm/']")
    if link_el and link_el.get("href"):
        return link_el.get("href")
    return None


def _extract_listing_price_text(card: Any) -> Optional[str]:
    selectors = [
        "span.s-item__price",
        "div.s-item__details span.s-item__price",
        "span.s-item__price span",
        "span.s-item__price span.POSITIVE",
        "*[data-testid='s-item__price']",
        "*[class*='s-item__price']",
        "span[aria-label*='£']",
        "span[aria-label*='$']",
        "span[aria-label*='GBP']",
        "span[aria-label*='EUR']",
    ]
    for selector in selectors:
        el = card.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return None


def _extract_listing_shipping_text(card: Any) -> Optional[str]:
    selectors = [
        "span.s-item__shipping",
        "span.s-item__logisticsCost",
        "span.s-item__shipping.s-item__logisticsCost",
        "*[data-testid='s-item__shipping']",
        "*[data-testid='s-item__logisticsCost']",
        "span[aria-label*='postage']",
        "span[aria-label*='shipping']",
    ]
    for selector in selectors:
        el = card.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return None


def _extract_listing_condition(card: Any) -> Optional[str]:
    selectors = [
        "span.SECONDARY_INFO",
        "span.s-item__subtitle",
    ]
    for selector in selectors:
        el = card.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return None


def _extract_seller_feedback(card: Any) -> tuple[Optional[float], Optional[int]]:
    seller_text = _get_text(card.select_one("span.s-item__seller-info-text"))
    if not seller_text:
        return None, None
    pct_match = re.search(r"([\d.]+)%\s*positive", seller_text)
    pct = _safe_float(pct_match.group(1)) if pct_match else None
    score_match = re.search(r"([\d,]+)\s+feedback", seller_text)
    score = _safe_int(score_match.group(1).replace(",", "")) if score_match else None
    return pct, score


def _extract_item_id_from_card(card: Any) -> str:
    attrs = {
        "data-itemid",
        "data-item-id",
        "data-view",
        "data-entityid",
        "data-entity-id",
        "data-listingid",
        "data-listing-id",
        "data-id",
    }
    for attr in attrs:
        value = card.get(attr)
        if not value:
            continue
        if isinstance(value, list):
            value = " ".join(str(part) for part in value)
        if isinstance(value, str):
            match = re.search(r"(\d{9,})", value)
            if match:
                return match.group(1)
    for attr, value in getattr(card, "attrs", {}).items():
        if attr in attrs:
            continue
        if not any(token in attr for token in ("item", "listing", "entity", "view", "id")):
            continue
        if isinstance(value, list):
            value = " ".join(str(part) for part in value)
        if isinstance(value, str):
            match = re.search(r"\b(\d{9,})\b", value)
            if match:
                return match.group(1)
    return ""


def _parse_json_ld_listings(
    soup: BeautifulSoup, target: Target, client: EbayClient
) -> list[Listing]:
    listings: list[Listing] = []
    seen_ids: set[str] = set()
    for item in _iter_json_ld_items(soup, client):
        if not item.url or not item.title:
            continue
        ebay_item_id = _extract_item_id(item.url)
        if not ebay_item_id or ebay_item_id in seen_ids:
            continue
        seen_ids.add(ebay_item_id)
        shipping_value, assumed_shipping = client._apply_missing_shipping(0.0, True)
        listings.append(
            Listing(
                ebay_item_id=ebay_item_id,
                target_id=target.id or 0,
                title=item.title,
                url=item.url,
                price_gbp=item.price_gbp,
                shipping_gbp=shipping_value,
                total_buy_gbp=item.price_gbp + shipping_value,
                listing_type=None,
                location=None,
                image_url=item.image_url,
                raw_json={
                    "source": "html-jsonld",
                    "shipping_missing": True,
                    "assumed_shipping_gbp": assumed_shipping,
                },
            )
        )
    return listings


def _parse_initial_state_listings(
    soup: BeautifulSoup, target: Target, client: EbayClient
) -> list[Listing]:
    state = _extract_initial_state(soup)
    if not state:
        return []
    items = _iter_initial_state_items(state)
    listings: list[Listing] = []
    seen_ids: set[str] = set()
    for item in items:
        ebay_item_id = _get_state_text(item, ["itemId", "item_id", "id"])
        if not ebay_item_id or ebay_item_id in seen_ids:
            continue
        title = _get_state_text(item, ["title", "itemTitle", "titleText", "name"])
        if not title:
            continue
        price_value, currency = _get_state_price(item)
        if price_value is None or not client._currency_allowed(currency):
            continue
        price_gbp, _ = client._normalize_currency(price_value, 0.0, currency)
        url = _get_state_text(item, ["itemUrl", "viewItemUrl", "url"])
        if not url:
            url = f"https://www.ebay.co.uk/itm/{ebay_item_id}"
        image_url = _get_state_text(item, ["imageUrl", "image", "thumbnailUrl"])
        shipping_value, assumed_shipping = client._apply_missing_shipping(0.0, True)
        listings.append(
            Listing(
                ebay_item_id=ebay_item_id,
                target_id=target.id or 0,
                title=title,
                url=url,
                price_gbp=price_gbp,
                shipping_gbp=shipping_value,
                total_buy_gbp=price_gbp + shipping_value,
                listing_type=None,
                location=None,
                image_url=image_url,
                raw_json={
                    "source": "html-initial-state",
                    "shipping_missing": True,
                    "assumed_shipping_gbp": assumed_shipping,
                },
            )
        )
        seen_ids.add(ebay_item_id)
    return listings


def _parse_json_ld_comps(soup: BeautifulSoup, client: EbayClient) -> list[SoldComp]:
    comps: list[SoldComp] = []
    seen_urls: set[str] = set()
    for item in _iter_json_ld_items(soup, client):
        if not item.title:
            continue
        if item.url and item.url in seen_urls:
            continue
        if item.url:
            seen_urls.add(item.url)
        comps.append(
            SoldComp(
                price_gbp=item.price_gbp,
                title=item.title,
                url=item.url,
            )
        )
    return comps


@dataclass(frozen=True, slots=True)
class JsonLdItem:
    title: str
    url: Optional[str]
    price_gbp: float
    image_url: Optional[str]


def _iter_json_ld_items(soup: BeautifulSoup, client: EbayClient) -> list[JsonLdItem]:
    entries = _extract_json_ld_entries(soup)
    items: list[JsonLdItem] = []
    for entry in entries:
        payload, offers = _extract_json_ld_payload(entry)
        if not payload:
            continue
        title = _get_json_ld_text(payload, entry, "name")
        url = _get_json_ld_text(payload, entry, "url")
        price = _get_json_ld_text(offers, None, "price") or _get_json_ld_text(offers, None, "lowPrice")
        currency = _get_json_ld_text(offers, None, "priceCurrency") or "GBP"
        if price is None:
            continue
        price_value = _safe_float(price) or 0.0
        if not client._currency_allowed(currency):
            continue
        price_gbp, _ = client._normalize_currency(price_value, 0.0, currency)
        items.append(
            JsonLdItem(
                title=str(title) if title is not None else "",
                url=str(url) if url else None,
                price_gbp=price_gbp,
                image_url=_get_json_ld_image(payload) or _get_json_ld_image(entry),
            )
        )
    return items


def _extract_json_ld_entries(soup: BeautifulSoup) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        payload = script.string or script.get_text(strip=True)
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        entries.extend(_walk_json_ld_entries(data))
    return entries


def _extract_initial_state(soup: BeautifulSoup) -> Optional[dict[str, Any]]:
    state = _extract_script_json_by_id(soup, "__NEXT_DATA__")
    if state:
        return state
    for script in soup.find_all("script"):
        payload = script.string or script.get_text(strip=True)
        if not payload:
            continue
        parsed = _extract_state_from_payload(payload)
        if parsed:
            return parsed
    return None


def _extract_script_json_by_id(soup: BeautifulSoup, script_id: str) -> Optional[dict[str, Any]]:
    script = soup.find("script", id=script_id)
    if not script:
        return None
    payload = script.string or script.get_text(strip=True)
    if not payload:
        return None
    return _load_json_payload(payload)


def _extract_state_from_payload(payload: str) -> Optional[dict[str, Any]]:
    markers = [
        "__INITIAL_STATE__",
        "__PRELOADED_STATE__",
        "__APOLLO_STATE__",
    ]
    for marker in markers:
        if marker in payload:
            state = _extract_json_payload(payload, marker)
            if state:
                return state
    if payload.lstrip().startswith("{") and payload.rstrip().endswith("}"):
        return _load_json_payload(payload)
    return None


def _load_json_payload(payload: str) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_json_payload(text: str, marker: str) -> Optional[dict[str, Any]]:
    marker_index = text.find(marker)
    if marker_index == -1:
        return None
    brace_start = text.find("{", marker_index)
    if brace_start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(brace_start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace_start : idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _walk_json_ld_entries(data: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            found.extend(_walk_json_ld_entries(item))
        return found
    if not isinstance(data, dict):
        return found
    item_list = data.get("itemListElement")
    if isinstance(item_list, list):
        for entry in item_list:
            if isinstance(entry, dict):
                found.append(entry)
            elif isinstance(entry, str):
                found.append({"url": entry})
    for value in data.values():
        if isinstance(value, (dict, list)):
            found.extend(_walk_json_ld_entries(value))
    return found


def _iter_initial_state_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in (
        "items",
        "itemList",
        "itemListElement",
        "searchResults",
        "results",
        "itemSummaries",
    ):
        value = state.get(key)
        if isinstance(value, list):
            candidates.extend([item for item in value if isinstance(item, dict)])
    if candidates:
        return candidates
    return [item for item in _walk_state_entries(state) if _looks_like_listing(item)]


def _walk_state_entries(data: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(data, dict):
        entries.append(data)
        for value in data.values():
            entries.extend(_walk_state_entries(value))
    elif isinstance(data, list):
        for item in data:
            entries.extend(_walk_state_entries(item))
    return entries


def _looks_like_listing(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if any(key in item for key in ("itemId", "item_id", "id")) and any(
        key in item for key in ("title", "itemTitle", "titleText", "name")
    ):
        return True
    return False


def _get_state_text(item: dict[str, Any], keys: list[str]) -> Optional[str]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for sub_key in ("value", "text", "string"):
                sub_value = value.get(sub_key)
                if isinstance(sub_value, str) and sub_value.strip():
                    return sub_value.strip()
    return None


def _get_state_price(item: dict[str, Any]) -> tuple[Optional[float], str]:
    price_fields = [
        "price",
        "priceValue",
        "currentPrice",
        "buyNowPrice",
        "priceWithCurrency",
        "priceText",
        "displayPrice",
        "amount",
    ]
    currency_fields = ["currency", "currencyCode", "currencyId"]
    for key in price_fields:
        value = item.get(key)
        if isinstance(value, dict):
            amount = value.get("value") or value.get("amount") or value.get("price")
            currency = value.get("currency") or value.get("currencyCode") or "GBP"
            amount_value = _safe_float(amount)
            if amount_value is not None:
                return amount_value, str(currency)
            if isinstance(value.get("text"), str):
                return _parse_price(value["text"])
        if isinstance(value, (int, float)):
            currency = _get_state_text(item, currency_fields) or "GBP"
            return float(value), currency
        if isinstance(value, str):
            amount_value, currency = _parse_price(value)
            if amount_value:
                return amount_value, currency
    return None, "GBP"


def _extract_json_ld_payload(entry: dict[str, Any]) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    payload: Optional[dict[str, Any]] = None
    if isinstance(entry, dict):
        item = entry.get("item")
        if isinstance(item, dict):
            payload = item
        else:
            payload = entry
    offers = payload.get("offers") if isinstance(payload, dict) else None
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    return payload if isinstance(payload, dict) else None, offers


def _get_json_ld_text(payload: Optional[dict[str, Any]], fallback: Optional[dict[str, Any]], key: str) -> Any:
    if isinstance(payload, dict) and key in payload:
        return payload.get(key)
    if isinstance(fallback, dict):
        return fallback.get(key)
    return None


def _get_json_ld_image(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    image = payload.get("image")
    if isinstance(image, list):
        return image[0] if image else None
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        return image.get("url")
    return None


def _cached_to_response(cached: CachedResponse, url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = cached.status_code
    response._content = cached.text.encode("utf-8")
    response.headers = cached.headers
    response.url = url
    return response


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
    cleaned = re.sub(r'(["\'])(.*?)\1', r"\2", query)
    cleaned = re.sub(r"(?<=\D)(?=\d)|(?<=\d)(?=\D)", " ", cleaned)
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
    if not cleaned:
        return cleaned
    words = cleaned.split()
    if len(words) < 5:
        filler = ["for", "sale", "used", "listing", "deal"]
        for token in filler:
            if len(words) >= 5:
                break
            if token not in words:
                words.append(token)
    return " ".join(words)


def _build_retry_steps(base: SearchCriteria) -> list[tuple[str, SearchCriteria]]:
    steps: list[tuple[str, SearchCriteria]] = [("initial", base)]
    if base.category_id:
        steps.append(("removed category filter", dataclasses.replace(base, category_id=None)))
    if base.condition:
        steps.append(("removed condition filter", dataclasses.replace(base, condition=None)))
    if base.listing_type and base.listing_type != "any":
        steps.append(("removed listing type filter", dataclasses.replace(base, listing_type="any")))
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
        status="ok",
        blocked=None,
    )


if __name__ == "__main__":
    results = fetch_ebay_search("iphone 14")
    print(f"Found {len(results)} items")
    print(results[:3])
