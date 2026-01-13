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

# CDKeys currently redirects to Loaded. We support both.
_BASE = "https://www.loaded.com"
_ALLOWED_HOSTS = {
    "www.loaded.com",
    "loaded.com",
    "www.cdkeys.com",
    "cdkeys.com",
}

# Product pages on Loaded are typically root-level slugs (NOT /game/...)
# We treat "category" pages as: /pc, /pc/games, /pc/steam, /explore/..., /deals, etc.
# Product pages are generally "/<slug>" (no extra path segments) and usually contain hyphens.
_CATEGORY_PREFIXES = (
    "/pc",
    "/playstation",
    "/xbox",
    "/nintendo",
    "/gift-cards",
    "/explore",
    "/deals",
    "/loaded-gift-cards",
    "/blog",
    "/faqs",
    "/privacy-policy",
    "/terms",
    "/customer",
    "/checkout",
    "/cart",
    "/search",
)

# Title cleanup
_TITLE_CLEAN_RE = re.compile(r"\s*(\|\s*(?:Loaded|CDKeys)\s*|-\s*(?:Loaded|CDKeys)\s*)$", re.I)

# Price parsing
_PRICE_GBP_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
# Some DOM blocks may show "GBP12.34" (rare), handle it:
_PRICE_GBP_WORD_RE = re.compile(r"\bGBP\s*(\d+(?:\.\d{1,2})?)\b", re.I)

# Very conservative number parse
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


def _is_probably_product_path(path: str) -> bool:
    """
    Loaded product pages are typically root-level paths like:
      /the-last-of-us-part-i-pc-steam
      /hacktag-pc-steam
    Category pages are multi-segment (/pc/..., /explore/..., etc).
    """
    if not path:
        return False

    p = path.rstrip("/")
    if not p or p == "/":
        return False

    # Exclude obvious categories and account/checkout pages
    for pref in _CATEGORY_PREFIXES:
        if p == pref or p.startswith(pref + "/"):
            return False

    # Product pages are usually single segment "/slug"
    # i.e. exactly one "/" at start, and no further slashes.
    if p.count("/") != 1:
        return False

    slug = p.lstrip("/")
    if len(slug) < 4:
        return False

    # Most product slugs contain hyphens and/or platform suffixes
    if "-" not in slug:
        return False

    return True


def _canonicalize_product_url(href_or_url: str) -> Optional[str]:
    """
    Turn relative/absolute into a canonical https://{host}/{slug} URL without query/fragment,
    but only if it looks like a product page.
    """
    if not href_or_url:
        return None

    # Use Loaded as base for relative URLs
    full = urljoin(_BASE, str(href_or_url).strip())
    try:
        p = urlparse(full)
    except Exception:
        return None

    host = (p.netloc or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None

    path = (p.path or "").rstrip("/")
    if not _is_probably_product_path(path):
        return None

    scheme = p.scheme or "https"
    return f"{scheme}://{host}{path}"


def _extract_jsonld_gbp_prices(html: str) -> List[float]:
    """
    Parse JSON-LD <script type="application/ld+json"> blocks and return GBP prices.
    """
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

            # nested priceSpecification pattern
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


def _first_gbp_price_in_text(text: str) -> Optional[float]:
    """
    Find a plausible GBP price in a small-ish text block.
    """
    if not text:
        return None

    vals: List[float] = []

    for m in _PRICE_GBP_RE.findall(text):
        v = _parse_float(m)
        if v is not None:
            vals.append(v)

    for m in _PRICE_GBP_WORD_RE.findall(text):
        v = _parse_float(m)
        if v is not None:
            vals.append(v)

    return min(vals) if vals else None


# -----------------------------------------------------------------------------
# Playwright client (reusable browser/context/page)
# -----------------------------------------------------------------------------

class FanaticalPWClient:
    """
    IMPORTANT:
    - Name kept as FanaticalPWClient so you don't need to change core.py imports.
    - Implementation targets CDKeys/Loaded.
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
        try:
            self._pw = sync_playwright().start()
            self.browser = self._pw.chromium.launch(headless=self.headless)
        except Exception as e:
            msg = str(e).lower()
            if "executable doesn't exist" in msg or ("browser" in msg and "not found" in msg):
                if self._pw:
                    try:
                        self._pw.stop()
                    except Exception:
                        pass
                    self._pw = None
                raise RuntimeError(
                    "Playwright browser binaries not found. Please restart the application to install them."
                ) from e
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            raise

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
    # Page helpers
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
                if loc.is_visible(timeout=600):
                    loc.click(timeout=900)
                    return
            except Exception:
                continue

    def _try_set_currency_gbp(self) -> None:
        """
        Loaded often defaults to PLN in cloud IPs.
        We try to switch currency to GBP via the currency dropdown (best effort).
        If this fails, price extraction in GBP may fail (and core will skip items).
        """
        assert self.page is not None

        # If we already see £ somewhere, don't waste time.
        try:
            if self.page.locator("text=£").first.is_visible(timeout=300):
                return
        except Exception:
            pass

        # Try common UI patterns (text-only, very forgiving)
        # Step 1: open currency dropdown (often shows "PLN", "EUR", etc)
        opener_selectors = (
            "text=PLN",
            "text=EUR",
            "text=USD",
            "text=Open   Close",  # sometimes the dropdown is a toggle near currency text
        )

        opened = False
        for sel in opener_selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=300):
                    loc.click(timeout=800)
                    opened = True
                    break
            except Exception:
                continue

        # Step 2: click GBP option
        if opened:
            for sel in (
                "text=GBP",
                "a:has-text('GBP')",
                "button:has-text('GBP')",
            ):
                try:
                    opt = self.page.locator(sel).first
                    if opt.is_visible(timeout=600):
                        opt.click(timeout=900)
                        _sleep(0.15, 0.35)
                        return
                except Exception:
                    continue

        # If we couldn't open it, try a direct click on a visible currency label area
        for sel in (
            "[class*='currency' i]",
            "[data-testid*='currency' i]",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=400):
                    loc.click(timeout=800)
                    _sleep(0.10, 0.25)
                    opt = self.page.locator("text=GBP").first
                    if opt.is_visible(timeout=600):
                        opt.click(timeout=900)
                        _sleep(0.15, 0.35)
                        return
            except Exception:
                continue

    # ---------------------------
    # Harvester
    # ---------------------------

    def _collect_product_anchors(self) -> List[str]:
        """
        Collect anchors and filter later by canonicalizer.
        We bias toward product-like links by excluding obvious nav categories.
        """
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
        max_rounds: int = 10,
        sleep_range_s: Tuple[float, float] = (0.20, 0.55),
        stable_rounds: int = 2,
    ) -> List[str]:
        assert self.page is not None

        seen: set[str] = set()
        out: List[str] = []
        stable = 0

        for _ in range(max(1, int(max_rounds))):
            before = len(seen)

            for href in self._collect_product_anchors():
                canon = _canonicalize_product_url(href)
                if canon and canon not in seen:
                    seen.add(canon)
                    out.append(canon)

            if len(seen) == before:
                stable += 1
            else:
                stable = 0

            if stable >= max(1, int(stable_rounds)):
                break

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
        assert self.page is not None

        pages = max(1, int(pages))
        max_links = max(1, int(max_links))

        def page_url(base: str, n: int) -> str:
            if n <= 1:
                return base
            joiner = "&" if "?" in base else "?"
            # Loaded uses ?p=2 etc for pagination
            if "p=" in base:
                return base
            return f"{base}{joiner}p={n}"

        out: List[str] = []
        seen: set[str] = set()

        for n in range(1, pages + 1):
            url = page_url(source_url, n)
            log.info("PW harvest (Loaded): goto %s", url)

            if not self._goto(url):
                continue

            _sleep(*sleep_range_s)
            self._try_accept_cookies()
            self._try_set_currency_gbp()

            links = self._scroll_until_stable(
                max_rounds=10,
                sleep_range_s=sleep_range_s,
                stable_rounds=2,
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
        """
        Return (title, price_gbp, notes).
        If we cannot find a GBP price, returns (title, None, "failed (...)").
        """
        assert self.page is not None

        # Canonicalize product URL where possible
        url = _canonicalize_product_url(url) or url

        if not self._goto(url):
            return None, None, "failed (PW timeout)"

        _sleep(0.20, 0.55)
        self._try_accept_cookies()
        self._try_set_currency_gbp()

        # Title: usually h1 on product pages
        title: Optional[str] = None
        try:
            title = _clean_title(self.page.locator("h1").first.inner_text(timeout=1500))
        except Exception:
            pass

        # 1) JSON-LD (best)
        try:
            html = self.page.content()
            prices = _extract_jsonld_gbp_prices(html)
            if prices:
                return title, prices[0], "ok (PW JSON-LD GBP)"
        except Exception:
            pass

        # 2) DOM fallback: look for visible price-ish blocks
        try:
            candidates = (
                "[data-testid*='price' i]",
                "[class*='price' i]",
                "[id*='price' i]",
                "span:has-text('£')",
                "div:has-text('£')",
                "span:has-text('GBP')",
                "div:has-text('GBP')",
            )
            for sel in candidates:
                loc = self.page.locator(sel).first
                try:
                    if not loc.is_visible(timeout=400):
                        continue
                except Exception:
                    continue

                try:
                    txt = loc.inner_text(timeout=1200)
                except Exception:
                    continue

                p = _first_gbp_price_in_text(txt or "")
                if p is not None:
                    return title, p, "ok (PW DOM GBP)"
        except Exception:
            pass

        # 3) Last resort: scan body text for a GBP pattern (still conservative)
        try:
            body = self.page.inner_text("body")
            p = _first_gbp_price_in_text(body or "")
            if p is not None:
                return title, p, "ok (PW BODY GBP)"
        except Exception:
            pass

        # Helpful note: if the site stayed in PLN, core will skip items.
        try:
            if self.page.locator("text=PLN").first.is_visible(timeout=300):
                return title, None, "failed (site currency stayed PLN; could not switch to GBP)"
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
