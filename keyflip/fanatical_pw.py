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
    if not text:
        return None
    vals: List[float] = []
    for m in _PRICE_GBP_RE.findall(text):
        v = _parse_float(m)
        if v is not None:
            vals.append(v)
    return min(vals) if vals else None


def _looks_like_noise_price_context(s: str) -> bool:
    s = (s or "").lower()
    noise_tokens = ("save", "was", "rrp", "off", "discount", "you save", "coupon")
    return any(t in s for t in noise_tokens)


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
            if str(node.get("priceCurrency", "")).upper() == "GBP":
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
        try:
            walk(json.loads(raw))
        except Exception:
            continue

    return sorted({p for p in prices if p is not None})


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
        try:
            self._pw = sync_playwright().start()
            self.browser = self._pw.chromium.launch(headless=self.headless)
        except Exception as e:
            msg = str(e).lower()
            missing = "executable doesn't exist" in msg or "executable does not exist" in msg

            if not missing:
                raise

            log.error(
                "Playwright Chromium binary missing. "
                "Attempting auto-install: python -m playwright install chromium"
            )

            if self._pw is not None:
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
                    "Run: python -m playwright install chromium"
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

    def __exit__(self, exc_type, exc, tb) -> None:
        for obj in (self.page, self.ctx, self.browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Harvesting
    # -----------------------------------------------------------------

    def _goto(self, url: str) -> bool:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception:
            return False

    def _try_accept_cookies(self) -> None:
        for sel in (
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=600):
                    loc.click(timeout=600)
                    return
            except Exception:
                continue

    def harvest_game_links(self, source_url: str, pages: int) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()

        for n in range(1, pages + 1):
            url = source_url if n == 1 else f"{source_url}&page={n}"
            if not self._goto(url):
                continue

            _sleep(0.3, 0.7)
            self._try_accept_cookies()

            hrefs = self.page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href'))",
            )

            for h in hrefs:
                u = _canonicalize_game_url(h)
                if u and u not in seen:
                    seen.add(u)
                    out.append(u)

        return out

    # -----------------------------------------------------------------
    # Title + price
    # -----------------------------------------------------------------

    def read_title_and_price_gbp(
        self, url: str
    ) -> Tuple[Optional[str], Optional[float], str]:
        if not self._goto(url):
            return None, None, "failed (timeout)"

        _sleep(0.3, 0.6)
        self._try_accept_cookies()

        title = None
        try:
            title = _clean_title(self.page.locator("h1").first.inner_text())
        except Exception:
            pass

        try:
            html = self.page.content()
            prices = _extract_jsonld_prices(html)
            if prices:
                return title, prices[0], "ok (json-ld)"
        except Exception:
            pass

        return title, None, "failed (no price)"


# -----------------------------------------------------------------------------
# Backwards-compatible helpers
# -----------------------------------------------------------------------------

def harvest_game_links(source_url: str, pages: int) -> List[str]:
    with FanaticalPWClient() as c:
        return c.harvest_game_links(source_url, pages)


def read_title_and_price_gbp(
    url: str,
) -> Tuple[Optional[str], Optional[float], str]:
    with FanaticalPWClient() as c:
        return c.read_title_and_price_gbp(url)
