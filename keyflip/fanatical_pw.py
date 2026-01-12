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
    """
    s = (s or "").lower()
    noise_tokens = ("save", "was", "rrp", "off", "discount", "you save", "coupon")
    return any(t in s for t in noise_tokens)


def _extract_jsonld_prices(html: str) -> List[float]:
    """
    Parse JSON-LD <script type="application/ld+json"> blocks and return GBP prices found in Offer(s).
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
            if "priceCurrency" in node and str(node.get("priceCurrency", "")).upper() == "GBP":
                for k in ("price", "lowPrice", "highPrice"):
                    if k in node:
                        v = _parse_float(node.get(k))
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


# -----------------------------------------------------------------------------
# Playwright client (reusable browser/context/page)
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

    # =========================
    # ONLY MODIFIED SECTION
    # =========================
    def __enter__(self) -> "FanaticalPWClient":
        try:
            self._pw = sync_playwright().start()
            self.browser = self._pw.chromium.launch(headless=self.headless)
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" not in msg and "executable doesn't exist" not in msg:
                raise

            log.error(
                "Browser binaries not found. Attempting to install via "
                "`python -m playwright install chromium`."
            )

            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None

            try:
                import subprocess, sys
                subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium"],
                    check=True,
                )
            except Exception as install_err:
                raise RuntimeError(
                    "Missing Playwright browser binaries. "
                    "Please run `python -m playwright install chromium`."
                ) from install_err

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
    # =========================
    # END MODIFIED SECTION
    # =========================

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
        assert self.page is not None
        for sel in (
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
        ):
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
        assert self.page is not None

        seen: set[str] = set()
        out: List[str] = []
        stable = 0

        for _ in range(max(1, int(max_rounds))):
            before = len(seen)

            for href in self._collect_anchors():
                canon = _canonicalize_game_url(href)
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
            return f"{base}{joiner}page={n}"

        out: List[str] = []
        seen: set[str] = set()

        for n in range(1, pages + 1):
            url = page_url(source_url, n)
            log.info("PW harvest: goto %s", url)

            if not self._goto(url):
                continue

            _sleep(*sleep_range_s)
            self._try_accept_cookies()

            links = self._scroll_until_stable(
                max_rounds=12,
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
        assert self.page is not None

        url = _canonicalize_game_url(url) or url

        if not self._goto(url):
            return None, None, "failed (PW timeout)"

        _sleep(0.25, 0.70)
        self._try_accept_cookies()

        title = None
        try:
            title = _clean_title(self.page.locator("h1").first.inner_text())
        except Exception:
            pass

        try:
            prices = _extract_jsonld_prices(self.page.content())
            if prices:
                return title, prices[0], "ok (PW JSON-LD offers GBP)"
        except Exception:
            pass

        return title, None, "failed (no GBP price found PW)"


# -----------------------------------------------------------------------------
# Backwards-compatible wrappers
# -----------------------------------------------------------------------------

def harvest_game_links(source_url: str, pages: int) -> List[str]:
    with FanaticalPWClient() as c:
        return c.harvest_game_links(source_url, pages)


def read_title_and_price_gbp(
    url: str,
) -> Tuple[Optional[str], Optional[float], str]:
    with FanaticalPWClient() as c:
        return c.read_title_and_price_gbp(url)
