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

# CheapShark (Fanatical is storeID=15)  :contentReference[oaicite:1]{index=1}
_CHEAPSHARK_BASE = "https://www.cheapshark.com/api/1.0"
_CHEAPSHARK_STORE_FANATICAL = "15"
_CHEAPSHARK_REDIRECT = "https://www.cheapshark.com/redirect?dealID="


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
            "Referer": _BASE + "/",
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


# Reuse one session for pooling + retry policy
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
    if not _GAME_PATH_RE.match(path):
        return None

    return f"{p.scheme}://{host}{path}".rstrip("/")


# -------------------------------------------------------------------
# NEW: CheapShark-based harvesting (works when Fanatical listings are JS)
# -------------------------------------------------------------------
def _cheapshark_fetch_deals(page_number: int, page_size: int) -> List[dict]:
    """
    Fetch CheapShark deals for Fanatical. Returns list of deal dicts.
    Note: CheapShark prices are USD; we only use dealID -> redirect -> Fanatical URL.
    """
    url = f"{_CHEAPSHARK_BASE}/deals"
    params = {
        "storeID": _CHEAPSHARK_STORE_FANATICAL,
        "pageNumber": max(0, int(page_number)),
        "pageSize": max(1, int(page_size)),
    }
    r = _SESS.get(url, params=params, timeout=_timeout())
    if r.status_code >= 400:
        log.warning("CheapShark deals non-OK (HTTP %s) url=%s", r.status_code, r.url)
        return []
    try:
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        log.warning("CheapShark deals JSON parse failed url=%s", r.url)
        return []


def _resolve_cheapshark_dealid_to_fanatical_url(deal_id: str) -> Optional[str]:
    """
    CheapShark gives dealID. Their /redirect?dealID=... 302s to the store product page.
    We follow redirects and then canonicalize to Fanatical /en/game/<slug>.
    """
    deal_id = (deal_id or "").strip()
    if not deal_id:
        return None

    redir = _CHEAPSHARK_REDIRECT + deal_id
    try:
        # follow redirects to final store URL
        r = _SESS.get(redir, timeout=_timeout(), allow_redirects=True)
    except requests.RequestException:
        return None

    final = r.url or ""
    # Some redirects may fail or go elsewhere; canonicalize will validate host + /en/game/
    return _canonicalize_game_url(final, final)


def harvest_game_links(
    source_url: str,
    pages: int,
    *,
    max_links: int = 500,
    sleep_range_s: Tuple[float, float] = (0.2, 0.6),
) -> List[str]:
    """
    Fanatical category pages are JS-rendered in many environments (your logs confirm this),
    so we harvest links via CheapShark -> redirect -> Fanatical game pages.

    `source_url` is ignored for harvesting (kept for API compatibility with your core),
    `pages` maps to CheapShark pageNumber iterations.
    """
    pages = max(1, int(pages))
    max_links = max(1, int(max_links))

    out: List[str] = []
    seen: set[str] = set()

    # CheapShark returns up to 60 deals/page by default; we can keep this modest.
    page_size = 60

    for page in range(0, pages):
        deals = _cheapshark_fetch_deals(page_number=page, page_size=page_size)
        log.info("CheapShark fetched page=%d deals=%d (Fanatical storeID=%s)", page, len(deals), _CHEAPSHARK_STORE_FANATICAL)

        # Randomize so each run builds a different pool (helps “fresh watchlist”)
        random.shuffle(deals)

        for d in deals:
            deal_id = str(d.get("dealID") or "").strip()
            if not deal_id:
                continue

            fan_url = _resolve_cheapshark_dealid_to_fanatical_url(deal_id)
            if not fan_url or fan_url in seen:
                continue

            seen.add(fan_url)
            out.append(fan_url)

            if len(out) >= max_links:
                log.info("Harvest hit max_links=%d", max_links)
                return out

        lo, hi = sleep_range_s
        if hi > 0:
            time.sleep(random.uniform(max(0.0, lo), max(0.0, hi)))

    log.info("Total harvested Fanatical game URLs via CheapShark: %d", len(out))
    return out


# -------------------------------------------------------------------
# Title + price extraction (your existing logic)
# -------------------------------------------------------------------
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
