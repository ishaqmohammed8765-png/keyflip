from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
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

# Accept /game/... and /en/... /en-gb/... etc (ONLY game pages).
_GAME_PATH_RE = re.compile(r"^/(?:en(?:-[a-z]{2})/)?game/", re.I)

_TITLE_CLEAN_RE = re.compile(r"\s*(\|\s*Fanatical|-+\s*Fanatical)\s*$", re.I)

_PRICE_GBP_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")


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


def _parse_float(s: object) -> Optional[float]:
    try:
        v = float(str(s).replace(",", "").strip())
        if 0.01 <= v <= 9999.0:
            return v
    except Exception:
        return None
    return None


def _looks_like_noise_price_context(s: str) -> bool:
    s = (s or "").lower()
    noise_tokens = (
        "save",
        "was",
        "rrp",
        "off",
        "discount",
        "you save",
        "coupon",
        "lowest",
        "historical",
        "bundle",
    )
    return any(t in s for t in noise_tokens)


def _first_gbp_price_in_text(text: str) -> Optional[float]:
    if not text:
        return None
    vals: List[float] = []
    for m in _PRICE_GBP_RE.findall(text):
        v = _parse_float(m)
        if v is not None:
            vals.append(v)
    return min(vals) if vals else None


def _extract_jsonld_prices(html: str) -> List[float]:
    if not html:
        return []

    prices: List[float] = []
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    )

    def walk(node: object) -> None:
        if isinstance(node, dict):
            cur = str(node.get("priceCurrency", "")).upper()
            if cur == "GBP":
                for k in ("price", "lowPrice", "highPrice"):
                    if k in node:
                        v = _parse_float(node.get(k))
                        if v is not None:
                            prices.append(v)

            ps = node.get("priceSpecification")
            if isinstance(ps, dict):
                cur2 = str(ps.get("priceCurrency", "")).upper()
                if cur2 == "GBP":
                    v = _parse_float(ps.get("price"))
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
            walk(json.loads(raw))
        except Exception:
            continue

    return sorted({p for p in prices if p is not None})


def _extract_gbp_from_lines(text: str) -> Optional[float]:
    if not text:
        return None

    vals: List[float] = []
    for line in (text or "").splitlines():
        ln = line.strip()
        if not ln:
            continue
        if _looks_like_noise_price_context(ln):
            continue
        p = _first_gbp_price_in_text(ln)
        if p is not None:
            vals.append(p)

    return min(vals) if vals else None


# -----------------------------------------------------------------------------
# Playwright client
# -----------------------------------------------------------------------------

class FanaticalPWClient:
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
    # Navigation helpers
    # ---------------------------

    def _goto(self, url: str, *, wait: str = "networkidle") -> bool:
        assert self.page is not None
        try:
            self.page.goto(url, wait_until=wait)
            return True
        except PWTimeoutError:
            return False
        except Exception:
            return False

    def _try_accept_cookies(self) -> None:
        assert self.page is not None
        for sel in (
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
            "button[aria-label*='accept' i]",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=800):
                    loc.click(timeout=1200)
                    return
            except Exception:
                continue

    def _page_looks_blocked(self) -> Optional[str]:
        """
        Detect obvious 'blocked' / bot-check pages.
        Returns a reason string if blocked-ish, else None.
        """
        assert self.page is not None
        try:
            title = (self.page.title() or "").lower()
            html = (self.page.content() or "").lower()
        except Exception:
            return None

        tokens = (
            "access denied",
            "request blocked",
            "captcha",
            "cloudflare",
            "verify you are human",
            "unusual traffic",
        )
        if any(t in title for t in tokens) or any(t in html for t in tokens):
            return "blocked/captcha detected"
        return None

    # ---------------------------
    # Link extraction (robust)
    # ---------------------------

    def _collect_hrefs(self) -> List[str]:
        """
        Collect hrefs from anchors. No filtering here; canonicalizer filters later.
        """
        assert self.page is not None
        try:
            return self.page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
            )
        except Exception:
            return []

    def _collect_hrefs_fallback(self) -> List[str]:
        """
        Fallback: grab any element with an href attribute (rare but helps).
        """
        assert self.page is not None
        try:
            return self.page.evaluate(
                """() => Array.from(document.querySelectorAll('[href]'))
                    .map(e => e.getAttribute('href'))
                    .filter(Boolean)"""
            )
        except Exception:
            return []

    def _wait_for_listing_content(self) -> None:
        """
        Wait for something meaningful to appear on Fanatical listing pages.
        We don't rely on exact selectors (they change).
        """
        assert self.page is not None
        # Try common patterns that indicate cards/links have rendered.
        for sel in (
            "a[href*='/game/']",
            "a[href*='/en/game/']",
            "a[href]",
        ):
            try:
                self.page.wait_for_selector(sel, timeout=6000)
                return
            except Exception:
                continue

    def _scroll_to_load_more(
        self,
        *,
        max_rounds: int = 16,
        stable_rounds: int = 3,
        sleep_range_s: Tuple[float, float] = (0.25, 0.6),
    ) -> List[str]:
        """
        Scroll using window.scrollTo and detect page height changes (more reliable in headless).
        """
        assert self.page is not None

        seen: set[str] = set()
        out: List[str] = []
        stable = 0
        last_height = 0

        for _ in range(max(1, int(max_rounds))):
            # collect
            for href in (self._collect_hrefs() or []):
                canon = _canonicalize_game_url(href)
                if canon and canon not in seen:
                    seen.add(canon)
                    out.append(canon)

            # height / stability
            try:
                height = int(self.page.evaluate("() => document.body.scrollHeight || 0"))
            except Exception:
                height = last_height

            if height <= last_height:
                stable += 1
            else:
                stable = 0
            last_height = height

            if stable >= max(1, int(stable_rounds)):
                break

            # scroll to bottom
            try:
                self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass

            _sleep(*sleep_range_s)

        # final fallback collect
        if not out:
            for href in (self._collect_hrefs_fallback() or []):
                canon = _canonicalize_game_url(href)
                if canon and canon not in seen:
                    seen.add(canon)
                    out.append(canon)

        return out

    def harvest_game_links(
        self,
        source_url: str,
        pages: int,
        *,
        max_links: int = 500,
        sleep_range_s: Tuple[float, float] = (0.25, 0.75),
    ) -> List[str]:
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

            if not self._goto(url, wait="networkidle"):
                continue

            self._try_accept_cookies()

            blocked = self._page_looks_blocked()
            if blocked:
                log.warning("Fanatical listing appears blocked: %s (url=%s)", blocked, url)
                continue

            # Wait for dynamic content
            self._wait_for_listing_content()
            _sleep(*sleep_range_s)

            links = self._scroll_to_load_more(
                max_rounds=16,
                stable_rounds=3,
                sleep_range_s=sleep_range_s,
            )
            random.shuffle(links)

            for u in links:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
                    if len(out) >= max_links:
                        break

            if len(out) >= max_links:
                break

        return out

    # ---------------------------
    # Title + price extraction (GBP)
    # ---------------------------

    def read_title_and_price_gbp(self, url: str) -> Tuple[Optional[str], Optional[float], str]:
        assert self.page is not None

        url = _canonicalize_game_url(url) or url

        if not self._goto(url, wait="networkidle"):
            return None, None, "failed (PW timeout)"

        _sleep(0.25, 0.70)
        self._try_accept_cookies()

        blocked = self._page_looks_blocked()
        if blocked:
            return None, None, f"failed ({blocked})"

        title: Optional[str] = None
        try:
            title = _clean_title(self.page.locator("h1").first.inner_text())
        except Exception:
            pass

        # JSON-LD first
        try:
            html = self.page.content()
            prices = _extract_jsonld_prices(html)
            if prices:
                return title, prices[0], "ok (PW JSON-LD offers GBP)"
        except Exception:
            pass

        # DOM fallback
        dom_selectors = (
            "[data-testid*='price' i]",
            "[class*='price' i]",
            "[id*='price' i]",
            "div:has-text('£')",
            "span:has-text('£')",
        )
        try:
            for sel in dom_selectors:
                loc = self.page.locator(sel).first
                try:
                    txt = loc.inner_text(timeout=1500)
                except Exception:
                    continue
                txt = (txt or "").strip()
                if not txt:
                    continue
                if _looks_like_noise_price_context(txt):
                    continue
                p = _extract_gbp_from_lines(txt) or _first_gbp_price_in_text(txt)
                if p is not None:
                    return title, p, "ok (PW DOM GBP fallback)"
        except Exception:
            pass

        # Body last resort
        try:
            body = self.page.inner_text("body")
            p = _extract_gbp_from_lines(body)
            if p is not None:
                return title, p, "ok (PW BODY GBP fallback)"
        except Exception:
            pass

        return title, None, "failed (no GBP price found PW)"


# -----------------------------------------------------------------------------
# Backwards-compatible wrappers
# -----------------------------------------------------------------------------

def harvest_game_links(source_url: str, pages: int) -> List[str]:
    with FanaticalPWClient() as c:
        return c.harvest_game_links(source_url, pages)


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    with FanaticalPWClient() as c:
        return c.read_title_and_price_gbp(url)
