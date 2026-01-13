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
# Loaded / CDKeys Playwright client (legacy name kept for compatibility)
# =============================================================================
#
# Public API intentionally stays the same:
#   - FanaticalPWClient
#   - harvest_game_links()
#   - read_title_and_price_gbp()
#
# Internally this targets Loaded/CDKeys pages.
# =============================================================================

log = logging.getLogger("keyflip.loaded_pw")

_BASE = "https://www.loaded.com"
_ALLOWED_HOSTS = {
    "www.loaded.com",
    "loaded.com",
    "www.cdkeys.com",
    "cdkeys.com",
}

_TITLE_CLEAN_RE = re.compile(r"\s*(\|\s*(?:Loaded|CDKeys)\s*|-\s*(?:Loaded|CDKeys)\s*)$", re.I)

_PRICE_GBP_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
_PRICE_GBP_WORD_RE = re.compile(r"\bGBP\s*(\d+(?:\.\d{1,2})?)\b", re.I)

_MIN_PRICE = 0.01
_MAX_PRICE = 9999.0


# ---------------------------------------------------------------------------
# Compatibility dataclass (kept for imports)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FanaticalItem:
    title: str
    url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sleep(lo: float, hi: float) -> None:
    lo = max(0.0, float(lo))
    hi = max(lo, float(hi))
    time.sleep(random.uniform(lo, hi))


def _clean_title(t: str) -> str:
    t = (t or "").strip()
    return _TITLE_CLEAN_RE.sub("", t).strip()


def _parse_float(x: object) -> Optional[float]:
    try:
        v = float(str(x).replace(",", "").strip())
        if _MIN_PRICE <= v <= _MAX_PRICE:
            return v
    except Exception:
        return None
    return None


def _force_https(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.netloc:
            return url
        scheme = "https"
        return urlunparse((scheme, p.netloc, p.path, "", p.query, ""))
    except Exception:
        return url


def _strip_query_and_fragment(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url.rstrip("/")


def _is_probably_product_path(path: str) -> bool:
    """
    Accept product pages for Loaded/CDKeys.

    We MUST allow platform-prefixed product URLs, otherwise you harvest 0:
      - /pc/<slug>
      - /xbox/<slug>
      - /playstation/<slug>
      - /nintendo/<slug>
      - sometimes: /pc/steam/<slug>

    Reject obvious non-product sections:
      - /explore, /deals, /blog, /cart, /checkout, /search, etc.
    """
    if not path:
        return False

    p = path.rstrip("/")
    if not p or p == "/":
        return False

    segs = [s for s in p.split("/") if s]
    if not segs:
        return False

    # Hard reject obvious non-product top-level sections
    hard_reject_first = {
        "explore",
        "deals",
        "blog",
        "faqs",
        "privacy-policy",
        "terms",
        "customer",
        "checkout",
        "cart",
        "search",
        "account",
    }
    if segs[0].lower() in hard_reject_first:
        return False

    platform_prefixes = {"pc", "playstation", "xbox", "nintendo", "gift-cards"}

    slug = ""

    if len(segs) == 1:
        # /<slug>
        slug = segs[0]

    elif len(segs) == 2:
        # /product/<slug> OR /pc/<slug> etc.
        if segs[0].lower() in {"product", "products"} or segs[0].lower() in platform_prefixes:
            slug = segs[1]
        else:
            return False

    elif len(segs) == 3:
        # /pc/steam/<slug> (or similar)
        if segs[0].lower() in platform_prefixes and len(segs[1]) <= 24:
            slug = segs[2]
        else:
            return False

    else:
        return False

    if len(slug) < 4:
        return False

    # Keep a light heuristic to avoid nonsense:
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

    return f"https://{host}{path}"


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

            ps = node.get("priceSpecification")
            if isinstance(ps, dict):
                cur2 = str(ps.get("priceCurrency", "")).upper()
                if cur2 == "GBP":
                    v = _parse_float(ps.get("price"))
                    if v is not None:
                        prices.append(v)

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
    clean = [p for p in prices if p is not None and _MIN_PRICE <= p <= _MAX_PRICE]
    return min(clean) if clean else None


# ---------------------------------------------------------------------------
# Playwright client (reusable browser/context/page)
# ---------------------------------------------------------------------------
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
            if "executable doesn't exist" in msg or ("browser" in msg and "not found" in msg):
                try:
                    if self._pw:
                        self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                raise RuntimeError(
                    "Playwright browser binaries not found. "
                    "On Streamlit Cloud, ensure Playwright is installed and browsers are available, then restart."
                ) from e
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None
            raise

        self.ctx = self.browser.new_context(user_agent=UA, locale=self.locale, viewport=self.viewport)
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
        assert self.page is not None
        url = _force_https(url)
        try:
            self.page.goto(url, wait_until=wait)
            return True
        except (PWTimeoutError, PWError):
            return False
        except Exception:
            return False

    def _tiny_settle(self) -> None:
        _sleep(0.12, 0.28)

    def _try_accept_cookies(self) -> None:
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
        Returns True if we believe we clicked a GBP option.
        """
        assert self.page is not None

        try:
            if self.page.locator("text=GBP").first.is_visible(timeout=350):
                return True
        except Exception:
            pass

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

        if click_gbp():
            return True

        triggers = (
            "[data-testid*='currency' i]",
            "[data-test*='currency' i]",
            "[class*='currency' i]",
            "button[aria-haspopup='listbox']",
            "button[aria-haspopup='menu']",
            "[role='button'][aria-haspopup='listbox']",
            "[role='button'][aria-haspopup='menu']",
            "text=PLN",
            "text=EUR",
            "text=USD",
        )

        for trg in triggers:
            try:
                t = self.page.locator(trg).first
                if not t.is_visible(timeout=350):
                    continue
                t.click(timeout=900)
                self._tiny_settle()
                if click_gbp():
                    return True
            except Exception:
                continue

        return False

    # ---------------------------
    # Harvesting
    # ---------------------------
    def _collect_candidate_hrefs(self) -> List[str]:
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

            stable = stable + 1 if len(seen) == before else 0
            if stable >= max(1, int(stable_rounds)):
                break

            try:
                self.page.mouse.wheel(0, 1700)
            except Exception:
                pass

            _sleep(*sleep_range_s)
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
        """
        assert self.page is not None

        pages = max(1, int(pages))
        max_links = max(1, int(max_links))

        def page_url(base: str, n: int) -> str:
            if n <= 1:
                return base
            joiner = "&" if "?" in base else "?"
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
        assert self.page is not None

        selectors = (
            "[data-testid*='price' i]",
            "[data-test*='price' i]",
            "[class*='price' i]",
            "[id*='price' i]",
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
        """
        assert self.page is not None

        canon = _canonicalize_product_url(url)
        if canon:
            url = canon

        if not self._goto(url, wait="domcontentloaded"):
            return None, None, "failed (PW timeout)"

        self._tiny_settle()
        self._try_accept_cookies()

        title: Optional[str] = None
        try:
            title = _clean_title(self.page.locator("h1").first.inner_text(timeout=1600))
        except Exception:
            try:
                title = _clean_title(self.page.title())
            except Exception:
                title = None

        # 1) JSON-LD (best)
        try:
            html = self.page.content()
            prices = _extract_jsonld_gbp_prices(html)
            best = _pick_best_price(prices)
            if best is not None:
                return title, best, "ok (PW JSON-LD GBP)"
        except Exception:
            pass

        # 2) Try currency switch and retry JSON-LD once
        switched = self._try_set_currency_gbp()
        self._tiny_settle()

        if switched:
            try:
                html2 = self.page.content()
                prices2 = _extract_jsonld_gbp_prices(html2)
                best2 = _pick_best_price(prices2)
                if best2 is not None:
                    return title, best2, "ok (PW JSON-LD GBP after switch)"
            except Exception:
                pass

        # 3) DOM extraction
        try:
            p = self._extract_price_from_dom()
            if p is not None:
                return title, p, "ok (PW DOM GBP)"
        except Exception:
            pass

        # 4) Last resort: body snippet with commerce keywords
        try:
            body = self.page.inner_text("body") or ""
            snippet = body[:12000]
            if re.search(r"\b(add to cart|buy now|checkout|price|save|deal|offer)\b", snippet, re.I):
                p = _first_gbp_price_in_text(snippet)
                if p is not None:
                    return title, p, "ok (PW BODY GBP)"
        except Exception:
            pass

        try:
            if self.page.locator("text=PLN").first.is_visible(timeout=300):
                return title, None, "failed (site shows PLN; GBP not available via JSON-LD/DOM)"
        except Exception:
            pass

        return title, None, "failed (no GBP price found PW)" if not switched else "failed (switched but no GBP price found PW)"


# ---------------------------------------------------------------------------
# Backwards-compatible wrappers
# ---------------------------------------------------------------------------
def harvest_game_links(source_url: str, pages: int) -> List[str]:
    with FanaticalPWClient() as c:
        return c.harvest_game_links(source_url, pages)


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    with FanaticalPWClient() as c:
        return c.read_title_and_price_gbp(url)
