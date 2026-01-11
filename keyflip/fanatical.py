from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple
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
_ALLOWED_HOSTS = {"www.fanatical.com", "fanatical.com"}

# Block/bot pages often look like this even with HTTP 200
_BLOCK_PATTERNS = (
    "attention required",
    "cloudflare",
    "access denied",
    "are you a robot",
    "verify you are human",
    "checking your browser",
    "ddos-guard",
    "incapsula",
)

# Strictly match "/en/game/<slug>" with no extra segments.
# Accept optional trailing slash.
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
            "Referer": _BASE + "/",  # mild help sometimes
            "Connection": "keep-alive",
        }
    )

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
        pass

    return s


_SESS = _make_session()


def _bs(html: str) -> BeautifulSoup:
    """Prefer lxml if available; fall back safely."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _looks_blocked(html: str) -> bool:
    h = (html or "").lower()
    return any(p in h for p in _BLOCK_PATTERNS)


def _canonicalize_game_url(base_url: str, href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None

    full = urljoin(base_url, href)
    p = urlparse(full)

    host = (p.netloc or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None

    path = p.path or ""
    # Strict game path check
    if not _GAME_PATH_RE.match(path):
        return None

    # Canonicalize: drop query/fragment, drop trailing slash
    return f"{p.scheme}://{host}{path}".rstrip("/")


def harvest_game_links(
    source_url: str,
    pages: int,
    *,
    max_links: int = 500,
    sleep_range_s: Tuple[float, float] = (0.2, 0.8),
) -> List[str]:
    """
    Best-effort harvest:
    - scans anchors with strict validation
    - retry/backoff via session adapters
    - returns deduped canonical URLs (no querystrings)
    - optional per-page jitter sleep to reduce 429s
    - logs enough to debug "0 rows"
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

        ct = (r.headers.get("Content-Type") or "").lower()
        log.info("Fanatical fetch page=%d status=%d bytes=%d ct=%s url=%s", page, r.status_code, len(r.content), ct, url)

        if r.status_code in (403, 429):
            log.warning("Fanatical harvest blocked/rate-limited (HTTP %s) url=%s", r.status_code, url)
            break

        if r.status_code >= 400:
            log.warning("Fanatical harvest non-OK (HTTP %s) url=%s", r.status_code, url)
            continue

        html = r.text or ""
        if _looks_blocked(html):
            # Common issue on hosted runners
            title = ""
            try:
                soup0 = _bs(html)
                title = soup0.title.get_text(strip=True) if soup0.title else ""
            except Exception:
                pass
            snippet = html[:250].replace("\n", " ")
            log.error("Fanatical appears BLOCKED (200 OK). title=%r snippet=%r url=%s", title, snippet, url)
            break

        soup = _bs(html)

        anchors = soup.select("a[href]")
        accepted = 0
        for a in anchors:
            canon = _canonicalize_game_url(url, a.get("href") or "")
            if not canon or canon in seen:
                continue
            seen.add(canon)
            out.append(canon)
            accepted += 1
            if len(out) >= max_links:
                log.info("Fanatical harvest hit max_links=%d", max_links)
                return out

        log.info("Fanatical parsed page=%d anchors=%d accepted_game_links=%d", page, len(anchors), accepted)

        lo, hi = sleep_range_s
        if hi > 0:
            time.sleep(random.uniform(max(0.0, lo), max(0.0, hi)))

    log.info("Fanatical total harvested from %s: %d", source_url, len(out))
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
    s = s.replace(",", "")
    s = re.sub(r"[^\d.]+", "", s)
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _find_title(soup: BeautifulSoup) -> Optional[str]:
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(strip=True)
        if t:
            return _clean_title(t)

    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return _clean_title(str(og["content"]))

    tw = soup.select_one('meta[name="twitter:title"]')
    if tw and tw.get("content"):
        return _clean_title(str(tw["content"]))

    if soup.title:
        t = soup.title.get_text(strip=True)
        if t:
            return _clean_title(t)

    return None


def _extract_price_from_ldjson(soup: BeautifulSoup) -> Optional[float]:
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
    stack = [obj]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _extract_price_from_next_data(html_text: str) -> Optional[float]:
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return None

    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        cur = (node.get("currency") or node.get("priceCurrency") or node.get("currencyCode"))
        if str(cur).upper() != "GBP":
            continue
        for k in ("amount", "value", "price", "lowPrice", "currentPrice"):
            p = _parse_float(node.get(k))
            if p is not None:
                return p
    return None


def _extract_price_from_visible_gbp(html_text: str) -> Optional[float]:
    needles = ("add to basket", "add to cart", "checkout", "buy now")
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

    # Consider multiple matches; pick the lowest plausible (safer than first match)
    prices: List[float] = []
    for w in windows:
        for m in re.finditer(r"£\s*(\d+(?:\.\d{1,2})?)", w):
            p = _parse_float(m.group(1))
            if p is not None and 0.01 <= p <= 999.0:
                prices.append(p)

    return min(prices) if prices else None


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    """
    Safest first:
    1) meta itemprop price+currency
    2) JSON-LD offers
    3) __NEXT_DATA__
    4) visible £ near purchase UI (last resort)
    """
    try:
        r = _SESS.get(url, timeout=_timeout())
    except requests.RequestException as e:
        return None, None, f"failed (http error: {type(e).__name__})"

    if r.status_code in (403, 429):
        return None, None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, None, f"failed (http {r.status_code})"

    html = r.text or ""
    if _looks_blocked(html):
        return None, None, "failed (blocked page content)"

    soup = _bs(html)
    title = _find_title(soup)

    p = _extract_price_from_meta(soup)
    if p is not None:
        return title, p, "ok (META price+currency GBP)"

    p = _extract_price_from_ldjson(soup)
    if p is not None:
        return title, p, "ok (JSON-LD offers GBP)"

    p = _extract_price_from_next_data(html)
    if p is not None:
        return title, p, "ok (__NEXT_DATA__ GBP)"

    p = _extract_price_from_visible_gbp(html)
    if p is not None:
        return title, p, "ok (REGEX £ price, UI-window)"

    return title, None, "failed (no GBP price found)"
