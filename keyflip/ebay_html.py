from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("keyflip.ebay_html")


def _parse_price(text: str) -> tuple[Optional[float], Optional[str]]:
    if not text:
        return None, None
    text = text.replace(",", "")
    currency_map = {"£": "GBP", "€": "EUR", "$": "USD"}
    currency = None
    for symbol, cur in currency_map.items():
        if symbol in text:
            currency = cur
            text = text.replace(symbol, "")
            break
    match = re.search(r"(\d+\.?\d*)", text)
    if not match:
        return None, currency
    try:
        return float(match.group(1)), currency
    except ValueError:
        return None, currency


@dataclass(frozen=True)
class HtmlListing:
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
class HtmlSoldComp:
    price: Optional[float]
    currency: Optional[str]
    end_time: Optional[str]


class EbayHtmlClient:
    def __init__(self, session: requests.Session, *, timeout_s: float = 20.0) -> None:
        self._session = session
        self._timeout_s = timeout_s

    def search_listings(
        self,
        *,
        query_text: str,
        category_id: Optional[str],
        condition: Optional[str],
        limit: int,
        prefer_newly_listed: bool,
    ) -> list[HtmlListing]:
        log.warning("Using HTML fallback scraper for active listings. This is best-effort only.")
        params = {"_nkw": query_text}
        if category_id:
            params["_sacat"] = category_id
        if prefer_newly_listed:
            params["_sop"] = "10"
        if condition and condition.isdigit():
            params["LH_ItemCondition"] = condition
        url = f"https://www.ebay.co.uk/sch/i.html?{urlencode(params)}"
        resp = self._session.get(url, timeout=self._timeout_s)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        listings: list[HtmlListing] = []
        for item in soup.select("li.s-item"):
            link = item.select_one("a.s-item__link")
            title = item.select_one("h3.s-item__title")
            price_el = item.select_one("span.s-item__price")
            ship_el = item.select_one("span.s-item__shipping")
            cond_el = item.select_one("span.SECONDARY_INFO")
            if not link or not title or not price_el:
                continue
            listing_url = link.get("href") or ""
            listing_id = _extract_listing_id(listing_url)
            price, currency = _parse_price(price_el.get_text(strip=True))
            shipping, _ = _parse_price(ship_el.get_text(strip=True) if ship_el else "")
            condition_text = cond_el.get_text(strip=True) if cond_el else ""
            listings.append(
                HtmlListing(
                    listing_id=listing_id,
                    title=title.get_text(strip=True),
                    listing_url=listing_url,
                    price=price,
                    shipping=shipping,
                    currency=currency,
                    condition=condition_text,
                    end_time=None,
                    start_time=None,
                    seller_feedback=None,
                    location=None,
                    listing_type="HTML",
                    buy_it_now=True,
                )
            )
            if len(listings) >= limit:
                break
        return listings

    def fetch_sold_comps(
        self,
        *,
        query_text: str,
        category_id: Optional[str],
        condition: Optional[str],
        limit: int,
    ) -> list[HtmlSoldComp]:
        log.warning("Using HTML fallback scraper for sold comps. This is best-effort only.")
        params = {"_nkw": query_text, "LH_Sold": "1", "LH_Complete": "1"}
        if category_id:
            params["_sacat"] = category_id
        if condition and condition.isdigit():
            params["LH_ItemCondition"] = condition
        url = f"https://www.ebay.co.uk/sch/i.html?{urlencode(params)}"
        resp = self._session.get(url, timeout=self._timeout_s)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        comps: list[HtmlSoldComp] = []
        for item in soup.select("li.s-item"):
            price_el = item.select_one("span.s-item__price")
            if not price_el:
                continue
            price, currency = _parse_price(price_el.get_text(strip=True))
            if price is None:
                continue
            comps.append(HtmlSoldComp(price=price, currency=currency, end_time=None))
            if len(comps) >= limit:
                break
        return comps


def _extract_listing_id(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"/itm/(?:[^/]+/)?(\d+)", url)
    if match:
        return match.group(1)
    return url.split("?")[0][-32:]
