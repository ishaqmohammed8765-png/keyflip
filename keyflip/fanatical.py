from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import UA  # keep this
# Support BOTH config styles:
# - HTTP_TIMEOUT_S = 20
# - HTTP_CONNECT_TIMEOUT_S / HTTP_READ_TIMEOUT_S
try:
    from .config import HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S  # type: ignore
    _TIMEOUT: object = (HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S)
except Exception:
    from .config import HTTP_TIMEOUT_S  # type: ignore
    _TIMEOUT = HTTP_TIMEOUT_S

log = logging.getLogger("keyflip.fanatical")

_BASE = "https://www.fanatical.com"
_GAME_PATH_RE = re.compile(r"^/en/game/[^/?#]+/?$")


@dataclass(frozen=True)
class FanaticalItem:
    title: str
    url: str


def _timeout() -> object:
    """Requests timeout. Either float seconds or (connect, read)."""
    return _TIMEOUT


def _make_session() -> requests.Session:
    """
    One pooled session + polite headers.
    Adds retry/backoff for transient HTTP issues (429/5xx).
    """
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )

    # Lightweight retries with backoff.
    # (requests doesn't do this natively; urllib3 Retry is exposed via adapters)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    except Exception:
        # If urllib3 Retry isn't available for some reason, keep the session as-is.
        pass

    return s


# Reuse one session for connection pooling + retry policy
_SESS = _make_session()


def _canonicalize_game_url(base_url: str, href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None

    full = urljoin(base_url, href)
    p = urlparse(full)

    # Require Fanatical host
    if "fanatical.com" not in (p.netloc or ""):
        return None

    # Strong path validation (game pages only)
    path = p.path or ""
    if not _GAME_PATH_RE.match(path.rstrip("/") + "/"):
        # Normalize for matching: ensure trailing slash for regex
        if not _GAME_PATH_RE.match(path.rstrip("/") + "/"):
            return None

    # Canonicalize: drop query/fragment, drop trailing slash
    canon = f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    return canon


def harvest_game_links(
    source_url: str,
    pages: int,
    *,
    max_links: int = 500,
    sleep_range_s: Tuple[float, float] = (0.2, 0.8),
) -> List[str]:
    """
    Best-effort harvest:
    - scans anchors but with strong validation
    - retry/backoff via session adapters
    - returns deduped canonical URLs (no querystrings)
    - optional per-page jitter sleep to reduce 429s
    """
    pages = max(1, int(pages))
    max_links = max(1, int(max_links))

    out: List[str] = []
    seen: set[str] = set()

    for page in range(1, pages + 1):
        url = source_url
        if page > 1:
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}page={page}"

        try:
            r = _SESS.get(url, timeout=_timeout())
        except requests.RequestException as e:
            log.warning("Fanatical harvest HTTP error page=%s url=%s err=%s", page, url, type(e).__name__)
            continue

        if r.status_code in (403, 429):
            log.warning("Fanatical harvest blocked/rate-limited (HTTP %s) url=%s", r.status_code, url)
            break

        if r.status_code >= 400:
            log.warning("Fanatical harvest non-OK (HTTP %s) url=%s", r.status_code, url)
            continue

        soup = BeautifulSoup(r.text, "lxml")

        # Some pages are heavy; scanning all anchors is okay, but keep it bounded.
        for a in soup.select("a[href]"):
            canon = _canonicalize_game_url(url, a.get("href") or "")
            if not canon:
                continue
            if canon in seen:
                continue
            seen.add(canon)
            out.append(canon)
            if len(out) >= max_links:
                return out

        # jitter sleep (polite + reduces 429 chance)
        lo, hi = sleep_range_s
        if hi > 0:
            time.sleep(random.uniform(max(0.0, lo), max(0.0, hi)))

    return out


def _clean_title(raw: str) -> str:
    t = (raw or "").strip()
    t = re.sub(r"\s*\|\s*Fanatical\s*$", "", t).strip()
    t = re.sub(r"\s*-\s*Fanatical\s*$", "", t).strip()
    return t


def _parse_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    # remove commas and currency symbols
    s = s.replace(",", "")
    s = re.sub(r"[^\d.]+", "", s)
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _find_title(soup: BeautifulSoup) -> Optional[str]:
    # Prefer on-page h1
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(strip=True)
        if t:
            return _clean_title(t)

    # OpenGraph / Twitter
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return _clean_title(str(og["content"]))
    tw = soup.select_one('meta[name="twitter:title"]')
    if tw and tw.get("content"):
        return _clean_title(str(tw["content"]))

    # Fallback <title>
    if soup.title:
        t = soup.title.get_text(strip=True)
        if t:
            return _clean_title(t)

    return None


def _extract_price_from_ldjson(soup: BeautifulSoup) -> Optional[float]:
    # JSON-LD offers (parse JSON, don't regex random "price")
    for script in soup.select('script[type="application/ld+json"]'):
        txt = script.get_text(strip=True) or ""
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue

        blobs = data if isinstance(data, list) else [data]
        for blob in blobs:
            if not isinstance(blob, dict):
                continue
            offers = blob.get("offers")
            if not offers:
                continue
            offer_list = offers if isinstance(offers, list) else [offers]
            for off in offer_list:
                if not isinstance(off, dict):
                    continue
                cur = str(off.get("priceCurrency") or "").upper()
                if cur != "GBP":
                    continue

                # Prefer explicit "price", otherwise consider "lowPrice"
                p = _parse_float(off.get("price"))
                if p is not None:
                    return p
                lp = _parse_float(off.get("lowPrice"))
                if lp is not None:
                    return lp

    return None


def _extract_price_from_meta(soup: BeautifulSoup) -> Optional[float]:
    meta_price = soup.select_one('meta[itemprop="price"]')
    meta_cur = soup.select_one('meta[itemprop="priceCurrency"]')
    if not (meta_price and meta_cur):
        return None
    cur = str(meta_cur.get("content") or "").upper()
    if cur != "GBP":
        return None
    return _parse_float(meta_price.get("content"))


def _walk(obj: Any) -> Iterable[Any]:
    """Yield nested values (dict/list)."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _extract_price_from_next_data(html_text: str) -> Optional[float]:
    """
    Next.js fallback:
    Fanatical sometimes embeds product/price data inside <script id="__NEXT_DATA__"> JSON.
    We parse it and look for GBP-ish price dicts.
    """
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return None

    # Heuristic: find dicts that look like {"currency":"GBP","amount":...} or {"price":...,"currency":"GBP"}
    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        # Common key patterns
        cur = (node.get("currency") or node.get("priceCurrency") or node.get("currencyCode"))
        if str(cur).upper() != "GBP":
            continue

        for k in ("amount", "value", "price", "lowPrice", "currentPrice"):
            p = _parse_float(node.get(k))
            if p is not None:
                return p

    return None


def _extract_price_from_visible_gbp(html_text: str) -> Optional[float]:
    """
    LAST resort: visible '£12.34' pattern.
    To reduce false hits, prefer '£' values that are near 'Add to basket/cart' keywords.
    """
    # Narrow search window around common purchase UI words
    # (still heuristic, but safer than scanning whole HTML blindly)
    needles = ("add to basket", "add to cart", "buy", "checkout")
    lower = html_text.lower()

    idxs = [lower.find(n) for n in needles if lower.find(n) != -1]
    windows: List[str] = []
    if idxs:
        i = min(idxs)
        start = max(0, i - 2500)
        end = min(len(html_text), i + 2500)
        windows.append(html_text[start:end])
    else:
        windows.append(html_text)

    for w in windows:
        m = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", w)
        if m:
            return _parse_float(m.group(1))
    return None


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    """
    Attempts (GBP), safest first:
    1) meta[itemprop=price] + meta[itemprop=priceCurrency]==GBP (strong)
    2) JSON-LD offers with priceCurrency GBP (strong)
    3) __NEXT_DATA__ JSON (modern Fanatical fallback)
    4) visible £ fallback in purchase-UI window (last resort)

    Returns: (title, price_gbp, notes)
    """
    try:
        r = _SESS.get(url, timeout=_timeout())
    except requests.RequestException as e:
        return None, None, f"failed (http error: {type(e).__name__})"

    if r.status_code in (403, 429):
        return None, None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, None, f"failed (http {r.status_code})"

    soup = BeautifulSoup(r.text, "lxml")
    title = _find_title(soup)

    # 1) Meta
    p = _extract_price_from_meta(soup)
    if p is not None:
        return title, p, "ok (META price+currency GBP)"

    # 2) JSON-LD
    p = _extract_price_from_ldjson(soup)
    if p is not None:
        return title, p, "ok (JSON-LD offers GBP)"

    # 3) __NEXT_DATA__
    p = _extract_price_from_next_data(r.text)
    if p is not None:
        return title, p, "ok (__NEXT_DATA__ GBP)"

    # 4) Visible £ (last resort)
    p = _extract_price_from_visible_gbp(r.text)
    if p is not None:
        return title, p, "ok (REGEX £ price, UI-window)"

    return title, None, "failed (no GBP price found)"
