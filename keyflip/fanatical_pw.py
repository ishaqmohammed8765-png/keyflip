from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PWError,
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from .config import UA

# =============================================================================
# Loaded / CDKeys Playwright client
# =============================================================================
#
# NOTE ON LEGACY NAMES:
# This project previously used "fanatical" naming in imports.
# To avoid changing other modules, we KEEP the public class/function names:
#   - FanaticalPWClient
#   - harvest_game_links()
#   - read_title_and_price_gbp()
#
# Internally, everything is renamed and documented for Loaded/CDKeys.
# =============================================================================

log = logging.getLogger("keyflip.loaded_pw")

_BASE = "https://www.loaded.com"
_ALLOWED_HOSTS = {
    "www.loaded.com",
    "loaded.com",
    "www.cdkeys.com",
    "cdkeys.com",
}

# Known non-product / category-ish prefixes (best-effort)
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

_TITLE_CLEAN_RE = re.compile(
    r"\s*(\|\s*(?:Loaded|CDKeys)\s*|-\s*(?:Loaded|CDKeys)\s*)$",
    re.I,
)

_PRICE_GBP_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
_PRICE_GBP_WORD_RE = re.compile(r"\bGBP\s*(\d+(?:\.\d{1,2})?)\b", re.I)

# Avoid matching absurd or irrelevant numbers.
_MIN_PRICE = 0.01
_MAX_PRICE = 9999.0


# -----------------------------------------------------------------------------
# Compatibility dataclass (not required by this module, but kept for imports)
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class FanaticalItem:
    title: str
    url: str


# -----------------------------------------------------------------------------
# Small helpers
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
        if _MIN_PRICE <= v <= _MAX_PRICE:
            return v
    except Exception:
        return None
    return None


def _force_https(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme and p.scheme.lower() != "https":
            return urlunparse(("https", p.netloc, p.path, "", p.query, ""))
        if not p.scheme:
            return urlunparse(("https", p.netloc, p.path, "", p.query, ""))
    except Exception:
        pass
    return url


def _strip_query_and_fragment(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url.rstrip("/")


def _is_probably_product_path(path: str) -> bool:
    """
    Loaded product pages are commonly root-level paths like:
      /the-last-of-us-part-i-pc-steam

    We avoid being too strict to prevent "0 links harvested" failure modes.
    Heuristics:
      - not empty, not "/"
      - not under known category prefixes
      - either single segment OR looks like a product even if 2 segments (rare)
      - slug length >= 4
    """
    if not path:
        return False

    p = path.rstrip("/")
    if not p or p == "/":
        return False

    for pref in _CATEGORY_PREFIXES:
        if p == pref or p.startswith(pref + "/"):
            return False

    # Prefer root-level "/slug" but allow occasional "/something/slug" if it still looks product-like
    segs = [s for s in p.split("/") if s]
    if not segs:
        return False

    if len(segs) == 1:
        slug = segs[0]
    elif len(segs) == 2:
        # sometimes marketing paths might include one extra segment; keep very conservative
        slug = segs[1]
        # and the first segment must be short-ish
        if len(segs[0]) > 24:
            return False
    else:
        return False

    if len(slug) < 4:
        return False

    # "Must contain hyphen" was too strict; loosen:
    # accept if it has hyphen OR digits OR is long enough to be a real slug
    if "-" not in slug and not any(ch.isdigit() for ch in slug) and len(slug) < 12:
        return False

    return True


def _canonicalize_product_url(href_or_url: str) -> Optional[str]:
    """
    Convert relative/absolute to canonical https://{host}{path} (no query/fragment),
    only if it looks like a product page.
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
    if not _is_probably_product_path(path):
        return None

    url = f"https://{host}{path}"
    return url


def _extract_jsonld_gbp_prices(html: str) -> List[float]:
    """
    Parse JSON-LD <script type="application/ld+json"> blocks and return GBP prices.
    """
    if not html:
        return []

    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    )

    prices: List[float] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            cur = str(node.get("priceCurrency", "")).upper()
            if cur == "GBP":
                for k in ("price", "lowPrice", "highPrice"):
                    if k in node:
                        v = _parse_float(node.get(k))
                        if v is not None:
                            prices.append(v)

            # common nested pattern
            ps = node.get("priceSpecification")
            if isinstance(ps, dict):
                cur2 = str(ps.get("priceCurrency", "")).upper()
                if cur2 == "GBP":
                    v = _parse_float(ps.get("price"))
                    if v is not None:
                        prices.append(v)

            # offer(s) objects
            offers = node.get("offers")
            if isinstance(offers, dict):
                walk(offers)
            elif isinstance(offers, list):
                for x in offers:
                    walk(x)

            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for x in node:
                walk(x)

    for raw in scripts:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            walk(json.loads(raw))
        except Exception:
            continue

    return sorted({p for p in prices if p is not None})


def _first_gbp_price_in_text(text: str) -> Optional[float]:
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


def _pick_best_price(prices: Iterable[float]) -> Optional[float]:
    """
    If multiple GBP prices are found, pick the most plausible:
    - prefer the lowest (usually "current price") but only among plausible bounds
    - JSON-LD typically includes only the actual price anyway
    """
    clean = [p for p in prices if p is not None and _MIN_PRICE <= p <= _MAX_PRICE]
    return min(clean) if clean else None


# -----------------------------------------------------------------------------
# Playwright client (reusable browser/context/page)
# -----------------------------------------------------------------------------
class FanaticalPWClient:
    """
    Playwright client for Loaded/CDKeys product harvesting and GBP price extraction.

    Public name retained for compatibility with existing imports.
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
            # common Streamlit Cloud failure: browser binaries missing
            if "executable doesn't exist" in msg or ("browser" in msg and "not found" in msg):
                try:
                    if self._pw:
                        self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                raise RuntimeError(
                    "Playwright browser binaries not found. "
                    "On Streamlit Cloud, ensure Playwright is installed and browsers are available, "
                    "then restart the app."
                ) from e
            try:
                if self._pw:
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
        for closer in (self.page, self.ctx, self.browser):
            try:
                if closer is not None:
                    closer.close()  # type: ignore[attr-defined]
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
        """
        Navigate with a conservative wait. If the site is heavily JS-driven,
        callers may follow with a short wait_for_load_state or a small sleep.
        """
        assert self.page is not None

        url = _force_https(url)
        try:
            self.page.goto(url, wait_until=wait)
            return True
        except PWTimeoutError:
            return False
        except PWError:
            return False
        except Exception:
            return False

    def _tiny_settle(self) -> None:
        """
        Give React sites a brief moment to paint critical elements.
        """
        _sleep(0.12, 0.28)

    def _try_accept_cookies(self) -> None:
        """
        Best-effort cookie dialog dismissal.
        """
        assert self.page is not None
        selectors = (
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
            "button[aria-label*='accept' i]",
            "button:has-text('OK')",
        )
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.click(timeout=900)
                    self._tiny_settle()
                    return
            except Exception:
                continue

    def _try_set_currency_gbp(self) -> bool:
        """
        Best-effort currency switch to GBP.

        Key improvement:
        - We no longer assume currency state based on arbitrary '£' presence.
        - We attempt to locate common currency UI patterns by attributes and
          by searching for GBP options.
        - Returns True if we believe we clicked a GBP option.
        """
        assert self.page is not None

        # If there's already a clear GBP marker in a typical UI spot, skip.
        try:
            if self.page.locator("text=GBP").first.is_visible(timeout=350):
                return True
        except Exception:
            pass

        # Strategy:
        # 1) If a GBP option exists in DOM, try to open any likely menu first, then click GBP.
        # 2) If not, try clicking likely currency triggers and re-try GBP.
        gbp_clicked = False

        def click_gbp() -> bool:
            for sel in (
                "a:has-text('GBP')",
                "button:has-text('GBP')",
                "[role='option']:has-text('GBP')",
                "[role='menuitem']:has-text('GBP')",
                "text=GBP",
            ):
                try:
                    opt = self.page.locator(sel).first
                    if opt.is_visible(timeout=450):
                        opt.click(timeout=1000)
                        self._tiny_settle()
                        return True
                except Exception:
                    continue
            return False

        # likely triggers
        triggers = (
            # common data/role hooks
            "[data-testid*='currency' i]",
            "[data-test*='currency' i]",
            "[class*='currency' i]",
            # accessible menus
            "button[aria-haspopup='listbox']",
            "button[aria-haspopup='menu']",
            "[role='button'][aria-haspopup='listbox']",
            "[role='button'][aria-haspopup='menu']",
            # fallback: visible currency codes
            "text=PLN",
            "text=EUR",
            "text=USD",
        )

        # Try direct GBP click (sometimes list is already open/visible)
        if click_gbp():
            return True

        # Try opening triggers then clicking GBP
        for trg in triggers:
            try:
                t = self.page.locator(trg).first
                if not t.is_visible(timeout=350):
                    continue
                t.click(timeout=900)
                self._tiny_settle()
                if click_gbp():
                    gbp_clicked = True
                    break
            except Exception:
                continue

        return gbp_clicked

    # ---------------------------
    # Harvesting
    # ---------------------------
    def _collect_candidate_hrefs(self) -> List[str]:
        """
        Collect hrefs from the page. We gather broadly then canonicalize/filter.

        Improvement:
        - gather from anchors, plus elements with onclick navigation patterns
          are too site-specific; we stay simple and robust.
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
        max_rounds: int = 12,
        sleep_range_s: Tuple[float, float] = (0.20, 0.55),
        stable_rounds: int = 2,
    ) -> List[str]:
        assert self.page is not None

        seen: set[str] = set()
        out: List[str] = []
        stable = 0

        for _ in range(max(1, int(max_rounds))):
            before = len(seen)

            hrefs = self._collect_candidate_hrefs()
            for href in hrefs:
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
                self.page.mouse.wheel(0, 1700)
            except Exception:
                pass

            _sleep(*sleep_range_s)
            # allow any lazy loaders to attach new anchors
            self._tiny_settle()

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
        Harvest product links from Loaded category/search pages.

        Improvements:
        - Better canonicalization to prevent under-harvesting.
        - Slightly more robust scrolling behavior.
        - Avoids relying on "p=" already being present in URL.
        """
        assert self.page is not None

        pages = max(1, int(pages))
        max_links = max(1, int(max_links))

        def page_url(base: str, n: int) -> str:
            if n <= 1:
                return base
            joiner = "&" if "?" in base else "?"
            # Loaded uses ?p=2 for pagination on some listings
            if re.search(r"[?&]p=\d+", base):
                return base
            return f"{base}{joiner}p={n}"

        out: List[str] = []
        seen: set[str] = set()

        for n in range(1, pages + 1):
            url = page_url(source_url, n)
            log.info("PW harvest (Loaded): goto %s", url)

            if not self._goto(url, wait="domcontentloaded"):
                continue

            _sleep(*sleep_range_s)
            self._try_accept_cookies()
            self._try_set_currency_gbp()

            links = self._scroll_until_stable(
                max_rounds=12,
                sleep_range_s=sleep_range_s,
                stable_rounds=2,
            )
            random.shuffle(links)

            for u in links:
                u = _strip_query_and_fragment(_force_https(u))
                if u not in seen:
                    seen.add(u)
                    out.append(u)
                    if len(out) >= max_links:
                        break

            if len(out) >= max_links:
                break

        return out

    # ---------------------------
    # Title + GBP price extraction
    # ---------------------------
    def _extract_price_from_dom(self) -> Optional[float]:
        """
        Try to extract a GBP price from likely price containers.
        More context-aware than scanning whole body first.
        """
        assert self.page is not None

        # Prefer structured / semantic locations first
        selectors = (
            # explicit testids
            "[data-testid*='price' i]",
            "[data-test*='price' i]",
            # common classes/ids
            "[class*='price' i]",
            "[id*='price' i]",
            # buy box-ish regions
            "main [class*='buy' i] [class*='price' i]",
            "main form [class*='price' i]",
            "main",
        )

        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if not loc.is_visible(timeout=450):
                    continue
                txt = loc.inner_text(timeout=1200)
                p = _first_gbp_price_in_text(txt or "")
                if p is not None:
                    return p
            except Exception:
                continue

        return None

    def read_title_and_price_gbp(self, url: str) -> Tuple[Optional[str], Optional[float], str]:
        """
        Return (title, price_gbp, notes).

        Improvements:
        - Stronger URL canonicalization
        - JSON-LD is the first-class source and doesn't depend on currency UI
        - DOM extraction is more context-aware
        - BODY scan is last resort and conservative
        - Clearer notes for diagnostics without noisy logs
        """
        assert self.page is not None

        canon = _canonicalize_product_url(url)
        if canon:
            url = canon

        if not self._goto(url, wait="domcontentloaded"):
            return None, None, "failed (PW timeout)"

        self._tiny_settle()
        self._try_accept_cookies()

        # Title (usually h1)
        title: Optional[str] = None
        try:
            title = _clean_title(self.page.locator("h1").first.inner_text(timeout=1600))
        except Exception:
            # fallback: document title
            try:
                title = _clean_title(self.page.title())
            except Exception:
                title = None

        # 1) JSON-LD is best and does not require UI currency switching
        html: Optional[str] = None
        try:
            html = self.page.content()
            prices = _extract_jsonld_gbp_prices(html)
            best = _pick_best_price(prices)
            if best is not None:
                return title, best, "ok (PW JSON-LD GBP)"
        except Exception:
            pass

        # 2) Attempt currency switch, then retry DOM/JSON-LD once
        switched = self._try_set_currency_gbp()
        self._tiny_settle()

        if switched:
            # retry JSON-LD after switch (sometimes rerenders)
            try:
                html2 = self.page.content()
                prices2 = _extract_jsonld_gbp_prices(html2)
                best2 = _pick_best_price(prices2)
                if best2 is not None:
                    return title, best2, "ok (PW JSON-LD GBP after switch)"
            except Exception:
                pass

        # 3) DOM extraction (context-aware)
        try:
            p = self._extract_price_from_dom()
            if p is not None:
                return title, p, "ok (PW DOM GBP)"
        except Exception:
            pass

        # 4) Last resort: small body scan, but require it to be near common commerce keywords
        # to avoid picking unrelated "£" values.
        try:
            body = self.page.inner_text("body") or ""
            # If body is enormous, keep the first chunk (performance + fewer false positives)
            snippet = body[:12000]
            # require purchase-ish keywords nearby to reduce false positives
            if re.search(r"\b(add to cart|buy now|checkout|price|save|deal|offer)\b", snippet, re.I):
                p = _first_gbp_price_in_text(snippet)
                if p is not None:
                    return title, p, "ok (PW BODY GBP)"
        except Exception:
            pass

        # Helpful failure notes
        try:
            if self.page.locator("text=PLN").first.is_visible(timeout=300):
                return title, None, "failed (site shows PLN; GBP not available via JSON-LD/DOM)"
        except Exception:
            pass

        note = "failed (no GBP price found PW)"
        if switched:
            note = "failed (switched currency but no GBP price found PW)"
        return title, None, note


# -----------------------------------------------------------------------------
# Backwards-compatible wrappers
# -----------------------------------------------------------------------------
def harvest_game_links(source_url: str, pages: int) -> List[str]:
    with FanaticalPWClient() as c:
        return c.harvest_game_links(source_url, pages)


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    with FanaticalPWClient() as c:
        return c.read_title_and_price_gbp(url)
