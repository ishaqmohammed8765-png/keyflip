from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, Page, TimeoutError as PWTimeoutError, sync_playwright

from .config import UA

log = logging.getLogger("keyflip.fanatical_pw")

_BASE = "https://www.fanatical.com"
_ALLOWED_HOSTS = {"www.fanatical.com", "fanatical.com"}

# Accept /en/... and /en-gb/... etc, and only game pages.
_GAME_RE = re.compile(r"^/en(?:-[a-z]{2})?/game/", re.I)

# Price parsing
_PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")

@dataclass(frozen=True)
class FanaticalItem:
    title: str
    url: str

def _canonicalize(url: str) -> Optional[str]:
    try:
        p = urlparse(url)
    except Exception:
        return None
    host = (p.netloc or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None
    path = (p.path or "").rstrip("/")
    if not path:
        return None
    if not _GAME_RE.match(path + "/"):
        return None
    scheme = p.scheme or "https"
    return f"{scheme}://{host}{path}"

def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))

def _mk_page(browser: Browser) -> Page:
    ctx = browser.new_context(user_agent=UA, locale="en-GB")
    page = ctx.new_page()
    page.set_default_timeout(20_000)
    return page

def harvest_game_links(
    source_url: str,
    pages: int,
    *,
    max_links: int = 500,
    sleep_range_s: Tuple[float, float] = (0.25, 0.75),
) -> List[str]:
    """
    Playwright harvester:
    - visits Fanatical listing pages (JS-rendered)
    - extracts /en-*/game/... links
    """
    pages = max(1, int(pages))
    max_links = max(1, int(max_links))

    # Fanatical often uses ?page=2 for listings
    def page_url(base: str, n: int) -> str:
        if n <= 1:
            return base
        joiner = "&" if "?" in base else "?"
        return f"{base}{joiner}page={n}"

    out: List[str] = []
    seen: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = _mk_page(browser)

        try:
            for i in range(1, pages + 1):
                url = page_url(source_url, i)
                log.info("PW harvest: goto %s", url)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except PWTimeoutError:
                    log.warning("PW harvest timeout loading %s", url)
                    continue

                # Scroll a bit to trigger lazy loading
                try:
                    page.mouse.wheel(0, 1200)
                    _sleep(*sleep_range_s)
                except Exception:
                    pass

                # Grab all anchor hrefs
                hrefs = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
                )

                # Normalize and filter to game pages
                for href in hrefs:
                    full = urljoin(_BASE, str(href))
                    canon = _canonicalize(full)
                    if not canon:
                        continue
                    if canon in seen:
                        continue
                    seen.add(canon)
                    out.append(canon)
                    if len(out) >= max_links:
                        break

                log.info("PW harvest: page %d collected total=%d", i, len(out))
                if len(out) >= max_links:
                    break

                _sleep(*sleep_range_s)

        finally:
            try:
                page.context.close()
            except Exception:
                pass
            browser.close()

    log.info("PW harvest: total urls=%d", len(out))
    return out

def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    """
    Playwright title+price:
    - loads Fanatical product page reliably (JS + bot checks more likely to pass)
    - tries common selectors and a safe regex fallback
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = _mk_page(browser)

        try:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PWTimeoutError:
                return None, None, "failed (PW timeout)"

            # Small wait for client-side hydration
            _sleep(0.25, 0.60)

            # Title
            title = None
            try:
                h1 = page.locator("h1").first
                if h1.count() > 0:
                    t = h1.inner_text().strip()
                    if t:
                        title = t
            except Exception:
                pass

            if not title:
                try:
                    og = page.locator('meta[property="og:title"]').first
                    if og.count() > 0:
                        c = (og.get_attribute("content") or "").strip()
                        if c:
                            title = c
                except Exception:
                    pass

            # Price: try a few typical patterns
            price = None

            # 1) meta itemprop (often present)
            try:
                cur = (page.locator('meta[itemprop="priceCurrency"]').first.get_attribute("content") or "").upper()
                if cur == "GBP":
                    p = page.locator('meta[itemprop="price"]').first.get_attribute("content") or ""
                    m = re.search(r"(\d+(?:\.\d{1,2})?)", p.replace(",", ""))
                    if m:
                        price = float(m.group(1))
                        return title, price, "ok (PW meta itemprop)"
            except Exception:
                pass

            # 2) visible text regex fallback near buy buttons
            try:
                html = page.content()
                # Look for the first plausible £ price
                matches = _PRICE_RE.findall(html)
                if matches:
                    # choose min (safer on sale pages)
                    vals = []
                    for s in matches:
                        try:
                            v = float(s)
                            if 0.01 <= v <= 999.0:
                                vals.append(v)
                        except Exception:
                            continue
                    if vals:
                        price = min(vals)
                        return title, price, "ok (PW regex £ in HTML)"
            except Exception:
                pass

            return title, None, "failed (no GBP price found PW)"

        finally:
            try:
                page.context.close()
            except Exception:
                pass
            browser.close()
