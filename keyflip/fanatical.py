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

# Accept /en/... and /en-gb/... etc, and ONLY game pages.
_GAME_PATH_RE = re.compile(r"^/en(?:-[a-z]{2})?/game/", re.I)

# Price parsing
_PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")

# Strip site suffixes
_TITLE_CLEAN_RE = re.compile(r"\s*(\|\s*Fanatical|-+\s*Fanatical)\s*$", re.I)


@dataclass(frozen=True)
class FanaticalItem:
    title: str
    url: str


def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(max(0.0, lo), max(0.0, hi)))


def _canonicalize_game_url(href_or_url: str) -> Optional[str]:
    """
    Turn relative/absolute into a canonical https://{host}/en-*/game/... URL without query/fragment.
    """
    if not href_or_url:
        return None
    full = urljoin(_BASE, str(href_or_url).strip())
    try:
        p = urlparse(full)
    except Exception:
        return None

    host = (p.netloc or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None

    path = (p.path or "").rstrip("/")
    if not path:
        return None

    if not _GAME_PATH_RE.match(path + "/"):
        return None

    scheme = p.scheme or "https"
    return f"{scheme}://{host}{path}"


def _new_page(browser: Browser) -> Page:
    ctx = browser.new_context(
        user_agent=UA,
        locale="en-GB",
        viewport={"width": 1280, "height": 800},
    )
    page = ctx.new_page()
    # Default timeout for locators/navigation
    page.set_default_timeout(25_000)
    return page


def _close_page(page: Page) -> None:
    try:
        page.context.close()
    except Exception:
        pass


def _collect_anchors(page: Page) -> List[str]:
    """
    Pull all hrefs on the page.
    """
    try:
        return page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
        )
    except Exception:
        return []


def _scroll_and_collect(page: Page, *, rounds: int = 5, sleep_range_s: Tuple[float, float] = (0.25, 0.60)) -> List[str]:
    """
    Scroll a few times to trigger lazy-load, collecting links each round.
    """
    seen: set[str] = set()
    out: List[str] = []

    for _ in range(max(1, rounds)):
        hrefs = _collect_anchors(page)
        for href in hrefs:
            canon = _canonicalize_game_url(href)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            out.append(canon)

        # scroll down
        try:
            page.mouse.wheel(0, 1400)
        except Exception:
            pass
        _sleep(*sleep_range_s)

    return out


def harvest_game_links(
    source_url: str,
    pages: int,
    *,
    max_links: int = 500,
    sleep_range_s: Tuple[float, float] = (0.25, 0.75),
) -> List[str]:
    """
    Playwright Fanatical harvester:
    - Visits Fanatical listing pages (JS-rendered)
    - Extracts /en-*/game/... links
    - Dedupes and returns up to max_links

    NOTE: This assumes source_url is a Fanatical listing page like:
      https://www.fanatical.com/en/on-sale
      https://www.fanatical.com/en/top-sellers
      https://www.fanatical.com/en/new
      https://www.fanatical.com/en/trending
    """
    pages = max(1, int(pages))
    max_links = max(1, int(max_links))

    def page_url(base: str, n: int) -> str:
        if n <= 1:
            return base
        joiner = "&" if "?" in base else "?"
        return f"{base}{joiner}page={n}"

    out: List[str] = []
    seen: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = _new_page(browser)

        try:
            for n in range(1, pages + 1):
                url = page_url(source_url, n)
                log.info("PW harvest: goto %s", url)

                try:
                    page.goto(url, wait_until="domcontentloaded")
                except PWTimeoutError:
                    log.warning("PW harvest: timeout loading %s", url)
                    continue

                _sleep(*sleep_range_s)

                # Try to accept cookie popup (best-effort)
                for sel in (
                    "button:has-text('Accept')",
                    "button:has-text('I agree')",
                    "button:has-text('Accept all')",
                ):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=1500)
                            break
                    except Exception:
                        pass

                # Collect links with a few scroll rounds
                links = _scroll_and_collect(page, rounds=6, sleep_range_s=sleep_range_s)
                random.shuffle(links)

                for u in links:
                    if u in seen:
                        continue
                    seen.add(u)
                    out.append(u)
                    if len(out) >= max_links:
                        break

                log.info("PW harvest: page=%d new=%d total=%d", n, len(links), len(out))

                if len(out) >= max_links:
                    break

                _sleep(*sleep_range_s)

        finally:
            _close_page(page)
            browser.close()

    log.info("PW harvest: total urls=%d", len(out))
    return out


# ---------------------------
# Title + price extraction (GBP)
# ---------------------------

def _clean_title(t: str) -> str:
    t = (t or "").strip()
    t = _TITLE_CLEAN_RE.sub("", t).strip()
    return t


def _first_price_from_text(text: str) -> Optional[float]:
    """
    Find the smallest plausible £ price in the text.
    """
    if not text:
        return None
    matches = _PRICE_RE.findall(text)
    vals: List[float] = []
    for s in matches:
        try:
            v = float(s)
            if 0.01 <= v <= 999.0:
                vals.append(v)
        except Exception:
            continue
    return min(vals) if vals else None


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    """
    Playwright product page reader:
    - Loads Fanatical game page
    - Extracts title
    - Extracts GBP price via (in order):
      1) meta itemprop price+currency
      2) JSON-LD offer blocks in page content (regex)
      3) visible text / HTML regex fallback (min £)
    """
    url = _canonicalize_game_url(url) or url

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = _new_page(browser)

        try:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PWTimeoutError:
                return None, None, "failed (PW timeout)"

            _sleep(0.25, 0.70)

            # Title
            title: Optional[str] = None
            try:
                h1 = page.locator("h1").first
                if h1.count() > 0:
                    t = h1.inner_text().strip()
                    if t:
                        title = _clean_title(t)
            except Exception:
                pass

            if not title:
                try:
                    og = page.locator('meta[property="og:title"]').first
                    if og.count() > 0:
                        c = (og.get_attribute("content") or "").strip()
                        if c:
                            title = _clean_title(c)
                except Exception:
                    pass

            # Price strategy 1: meta itemprop
            try:
                cur = (page.locator('meta[itemprop="priceCurrency"]').first.get_attribute("content") or "").upper()
                if cur == "GBP":
                    raw = page.locator('meta[itemprop="price"]').first.get_attribute("content") or ""
                    raw = raw.replace(",", "").strip()
                    m = re.search(r"(\d+(?:\.\d{1,2})?)", raw)
                    if m:
                        return title, float(m.group(1)), "ok (PW meta itemprop GBP)"
            except Exception:
                pass

            # Price strategy 2: scan page content for structured offers (still robust)
            try:
                html = page.content()
                # Prefer window near buy UI if present, else whole html
                p = _first_price_from_text(html)
                if p is not None:
                    return title, p, "ok (PW regex £ in HTML)"
            except Exception:
                pass

            # Price strategy 3: visible text fallback
            try:
                body_txt = page.locator("body").inner_text(timeout=2000)
                p = _first_price_from_text(body_txt)
                if p is not None:
                    return title, p, "ok (PW regex £ in visible text)"
            except Exception:
                pass

            return title, None, "failed (no GBP price found PW)"

        finally:
            _close_page(page)
            browser.close()
