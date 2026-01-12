from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from .config import UA

log = logging.getLogger("keyflip.fanatical_pw")

_BASE = "https://www.fanatical.com"
_ALLOWED_HOSTS = {"www.fanatical.com", "fanatical.com"}

# Accept /en/... and /en-gb/... etc, and ONLY game pages.
_GAME_PATH_RE = re.compile(r"^/en(?:-[a-z]{2})?/game/", re.I)

# Strip site suffixes
_TITLE_CLEAN_RE = re.compile(r"\s*(\|\s*Fanatical|-+\s*Fanatical)\s*$", re.I)

# Price parsing (GBP)
_PRICE_GBP_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
_NUM_RE = re.compile(r"(\d+(?:\.\d{1,2})?)")


@dataclass(frozen=True)
class FanaticalItem:
    title: str
    url: str


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _sleep(lo: float, hi: float) -> None:
    lo = max(0.0, float(lo))
    hi = max(lo, float(hi))
    time.sleep(random.uniform(lo, hi))


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


def _clean_title(t: str) -> str:
    t = (t or "").strip()
    t = _TITLE_CLEAN_RE.sub("", t).strip()
    return t


def _parse_float(s: str) -> Optional[float]:
    try:
        v = float(str(s).replace(",", "").strip())
        if 0.01 <= v <= 9999.0:
            return v
    except Exception:
        return None
    return None


def _first_gbp_price_in_text(text: str) -> Optional[float]:
    """
    Conservative: returns the smallest plausible £ price in the *given text*.
    Use only when the text is already scoped to a buybox-ish region.
    """
    if not text:
        return None
    vals: List[float] = []
    for m in _PRICE_GBP_RE.findall(text):
        v = _parse_float(m)
        if v is not None:
            vals.append(v)
    return min(vals) if vals else None


def _looks_like_noise_price_context(s: str) -> bool:
    """
    Simple heuristics to avoid "Save £x", "Was £x", etc when scanning a block.
    If your block is scoped well, you often don't need this, but it helps.
    """
    s = (s or "").lower()
    noise_tokens = ("save", "was", "rrp", "off", "discount", "you save", "coupon")
    return any(t in s for t in noise_tokens)


def _extract_jsonld_prices(html: str) -> List[float]:
    """
    Parse JSON-LD <script type="application/ld+json"> blocks and return GBP prices found in Offer(s).
    More reliable than "min £ in whole HTML".
    """
    if not html:
        return []

    prices: List[float] = []

    # Grab JSON-LD scripts (best-effort)
    # Note: Playwright can also query script tags directly; we do regex to avoid extra DOM calls.
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    )

    def walk(node: object) -> None:
        if isinstance(node, dict):
            # common shapes: {"offers": {...}} or {"offers": [{...}, {...}]}
            if "priceCurrency" in node and str(node.get("priceCurrency", "")).upper() == "GBP":
                # price can be str/num
                if "price" in node:
                    v = _parse_float(node.get("price"))
                    if v is not None:
                        prices.append(v)
                if "lowPrice" in node:
                    v = _parse_float(node.get("lowPrice"))
                    if v is not None:
                        prices.append(v)
                if "highPrice" in node:
                    v = _parse_float(node.get("highPrice"))
                    if v is not None:
                        prices.append(v)

            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for x in node:
                walk(x)

    for raw in scripts:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Some sites include multiple JSON objects without array wrapper; try a salvage
            # by extracting {...} blocks. Keep it conservative.
            objs = re.findall(r"\{.*?\}", raw, flags=re.S)
            for o in objs[:5]:
                try:
                    walk(json.loads(o))
                except Exception:
                    continue
            continue

        walk(data)

    # De-dupe and sort low->high
    return sorted({p for p in prices if p is not None})


# -----------------------------------------------------------------------------
# Playwright client (reusable browser/context/page)
# -----------------------------------------------------------------------------

class FanaticalPWClient:
    """
    Reusable Playwright client to avoid launching Chromium for every URL.
    Use:
        with FanaticalPWClient(headless=True) as c:
            links = c.harvest_game_links(...)
            title, price, status = c.read_title_and_price_gbp(url)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        default_timeout_ms: int = 25_000,
        locale: str = "en-GB",
        viewport: Optional[dict] = None,
    ) -> None:
        self.headless = headless
        self.default_timeout_ms = int(default_timeout_ms)
        self.locale = locale
        self.viewport = viewport or {"width": 1280, "height": 800}

        self._pw = None
        self.browser: Optional[Browser] = None
        self.ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def __enter__(self) -> "FanaticalPWClient":
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)

        self.ctx = self.browser.new_context(
            user_agent=UA,
            locale=self.locale,
            viewport=self.viewport,
        )
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(self.default_timeout_ms)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        try:
            if self.ctx is not None:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass

    # ---------------------------
    # Harvester
    # ---------------------------

    def _goto(self, url: str, *, wait: str = "domcontentloaded") -> bool:
        assert self.page is not None
        try:
            self.page.goto(url, wait_until=wait)
            return True
        except PWTimeoutError:
            return False
        except Exception:
            return False

    def _try_accept_cookies(self) -> None:
        """
        Best-effort cookie accept. Avoid count(); just attempt clicks quickly.
        """
        assert self.page is not None
        candidates = (
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
        )
        for sel in candidates:
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=700):
                    loc.click(timeout=900)
                    return
            except Exception:
                continue

    def _collect_anchors(self) -> List[str]:
        assert self.page is not None
        try:
            return self.page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
            )
        except Exception:
            return []

    def _scroll_until_stable(
        self,
        *,
        max_rounds: int = 12,
        sleep_range_s: Tuple[float, float] = (0.25, 0.60),
        stable_rounds: int = 2,
    ) -> List[str]:
        """
        Scroll until we stop discovering new canonical game links.
        """
        assert self.page is not None

        seen: set[str] = set()
        out: List[str] = []
        stable = 0

        for _ in range(max(1, int(max_rounds))):
            before = len(seen)

            hrefs = self._collect_anchors()
            for href in hrefs:
                canon = _canonicalize_game_url(href)
                if not canon or canon in seen:
                    continue
                seen.add(canon)
                out.append(canon)

            # stop condition: no new links for N rounds
            if len(seen) == before:
                stable += 1
            else:
                stable = 0

            if stable >= max(1, int(stable_rounds)):
                break

            # scroll down
            try:
                self.page.mouse.wheel(0, 1600)
            except Exception:
                pass
            _sleep(*sleep_range_s)

        return out

    def harvest_game_links(
        self,
        source_url: str,
        pages: int,
        *,
        max_links: int = 500,
        sleep_range_s: Tuple[float, float] = (0.25, 0.75),
    ) -> List[str]:
        """
        Visits Fanatical listing pages and extracts /en-*/game/... links.
        """
        assert self.page is not None

        pages = max(1, int(pages))
        max_links = max(1, int(max_links))

        def page_url(base: str, n: int) -> str:
            if n <= 1:
                return base
            joiner = "&" if "?" in base else "?"
            return f"{base}{joiner}page={n}"

        out: List[str] = []
        seen: set[str] = set()

        for n in range(1, pages + 1):
            url = page_url(source_url, n)
            log.info("PW harvest: goto %s", url)

            ok = self._goto(url)
            if not ok:
                log.warning("PW harvest: timeout/failed loading %s", url)
                continue

            _sleep(*sleep_range_s)
            self._try_accept_cookies()

            links = self._scroll_until_stable(max_rounds=12, sleep_range_s=sleep_range_s, stable_rounds=2)
            random.shuffle(links)

            added = 0
            for u in links:
                if u in seen:
                    continue
                seen.add(u)
                out.append(u)
                added += 1
                if len(out) >= max_links:
                    break

            log.info("PW harvest: page=%d added=%d total=%d", n, added, len(out))
            if len(out) >= max_links:
                break

            _sleep(*sleep_range_s)

        log.info("PW harvest: total urls=%d", len(out))
        return out

    # ---------------------------
    # Title + price extraction (GBP)
    # ---------------------------

    def _extract_title(self) -> Optional[str]:
        assert self.page is not None

        # h1 first
        try:
            h1 = self.page.locator("h1").first
            if h1.is_visible(timeout=1200):
                t = (h1.inner_text() or "").strip()
                if t:
                    return _clean_title(t)
        except Exception:
            pass

        # og:title fallback
        try:
            og = self.page.locator('meta[property="og:title"]').first
            c = (og.get_attribute("content") or "").strip()
            if c:
                return _clean_title(c)
        except Exception:
            pass

        return None

    def _extract_price_meta_itemprop(self) -> Optional[float]:
        """
        If Fanatical exposes schema.org itemprop meta tags, use those (high trust).
        """
        assert self.page is not None
        try:
            cur = (
                self.page.locator('meta[itemprop="priceCurrency"]').first.get_attribute("content") or ""
            ).upper()
            if cur != "GBP":
                return None

            raw = self.page.locator('meta[itemprop="price"]').first.get_attribute("content") or ""
            m = _NUM_RE.search(raw.replace(",", ""))
            return _parse_float(m.group(1)) if m else None
        except Exception:
            return None

    def _extract_price_buybox_locators(self) -> Optional[float]:
        """
        Try several likely "primary price" selectors (no site is stable forever).
        The goal is to grab *the main displayed price*, not min price on page.
        """
        assert self.page is not None

        # A small curated set of selectors that often correspond to the main price.
        # We keep this intentionally modest and robust.
        selectors = [
            # common patterns for price text blocks
            "[data-test='price']",
            "[data-testid*='price']",
            "[class*='price']",
            "[id*='price']",
            # sometimes there is a buybox / purchase panel
            "[class*='buy'] [class*='price']",
            "[class*='purchase'] [class*='price']",
        ]

        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if not loc.is_visible(timeout=600):
                    continue
                txt = (loc.inner_text(timeout=900) or "").strip()
                if not txt:
                    continue

                # Avoid obvious noise blocks
                if _looks_like_noise_price_context(txt):
                    continue

                p = _first_gbp_price_in_text(txt)
                if p is not None:
                    return p
            except Exception:
                continue

        return None

    def _extract_price_jsonld(self) -> Optional[float]:
        """
        Parse JSON-LD offers and return the lowest GBP offer price.
        """
        assert self.page is not None
        try:
            html = self.page.content()
        except Exception:
            return None

        prices = _extract_jsonld_prices(html)
        return prices[0] if prices else None

    def _extract_price_scoped_fallback(self) -> Optional[float]:
        """
        LAST resort: scan a *scoped* chunk of the DOM (not whole HTML).
        This reduces false lows from "save/was/from" elsewhere on the page.
        """
        assert self.page is not None

        # Try to find a purchase/buy panel and scan only that.
        candidates = [
            "[class*='buy']",
            "[class*='purchase']",
            "[class*='checkout']",
            "[class*='add-to-cart']",
            "main",
        ]

        for sel in candidates:
            try:
                loc = self.page.locator(sel).first
                if not loc.is_visible(timeout=600):
                    continue
                txt = (loc.inner_text(timeout=1200) or "").strip()
                if not txt:
                    continue

                # Remove obvious "was/save" lines by filtering line-by-line.
                lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
                cleaned = "\n".join([ln for ln in lines if not _looks_like_noise_price_context(ln)])
                p = _first_gbp_price_in_text(cleaned)
                if p is not None:
                    return p
            except Exception:
                continue

        return None

    def read_title_and_price_gbp(self, url: str) -> Tuple[Optional[str], Optional[float], str]:
        """
        Loads Fanatical game page and extracts:
          - title
          - GBP price (primary price, not "min £ anywhere")

        Price strategies (in order):
          1) schema.org meta itemprop (GBP)
          2) likely buybox price locators
          3) JSON-LD offer parsing (GBP)
          4) scoped DOM fallback (buy-ish panel / main), regex £

        Returns: (title, price_gbp, status)
        """
        assert self.page is not None

        url = _canonicalize_game_url(url) or url

        try:
            ok = self._goto(url)
            if not ok:
                return None, None, "failed (PW timeout)"

            _sleep(0.25, 0.70)
            self._try_accept_cookies()

            title = self._extract_title()

            # Price 1: meta itemprop
            p = self._extract_price_meta_itemprop()
            if p is not None:
                return title, p, "ok (PW meta itemprop GBP)"

            # Price 2: buybox-ish locators
            p = self._extract_price_buybox_locators()
            if p is not None:
                return title, p, "ok (PW buybox locator GBP)"

            # Price 3: JSON-LD
            p = self._extract_price_jsonld()
            if p is not None:
                return title, p, "ok (PW JSON-LD offers GBP)"

            # Price 4: scoped fallback
            p = self._extract_price_scoped_fallback()
            if p is not None:
                return title, p, "ok (PW scoped regex GBP)"

            return title, None, "failed (no GBP price found PW)"

        except Exception as e:
            return None, None, f"failed (PW exception: {type(e).__name__})"


# -----------------------------------------------------------------------------
# Backwards-compatible function wrappers
# -----------------------------------------------------------------------------

def harvest_game_links(
    source_url: str,
    pages: int,
    *,
    max_links: int = 500,
    sleep_range_s: Tuple[float, float] = (0.25, 0.75),
    headless: bool = True,
) -> List[str]:
    """
    Backwards-compatible wrapper: launches one browser for this harvest call.
    Prefer using FanaticalPWClient directly when doing many operations.
    """
    with FanaticalPWClient(headless=headless) as c:
        return c.harvest_game_links(
            source_url,
            pages,
            max_links=max_links,
            sleep_range_s=sleep_range_s,
        )


def read_title_and_price_gbp(
    url: str,
    *,
    headless: bool = True,
) -> Tuple[Optional[str], Optional[float], str]:
    """
    Backwards-compatible wrapper: launches one browser for this single read.
    Prefer using FanaticalPWClient for batch reads.
    """
    with FanaticalPWClient(headless=headless) as c:
        return c.read_title_and_price_gbp(url)
