from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Iterable, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import HTTP_TIMEOUT_S, UA

log = logging.getLogger("keyflip.eneba")

_BASE = "https://www.eneba.com"

# Reject obvious non-game / not-a-key / wrong product types
_BAD_SLUG_WORDS = {
    "gift-card", "wallet", "prepaid", "card",
    "dlc", "season-pass", "seasonpass", "add-on", "addon",
    "bundle", "soundtrack", "artbook", "expansion",
}

# Must look like an actual product page path on /gb/
# Eneba product URLs commonly look like:
#   /gb/steam-xxx-pc-key-global
#   /gb/game-title-pc-steam-key-global
_PRODUCT_SLUG_RE = re.compile(r"^/gb/[a-z0-9-]{8,}$")


def _make_session() -> requests.Session:
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

    # Retry/backoff on transient issues
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


def make_store_search_url(title: str) -> str:
    q = quote_plus(f"{title} steam key pc")
    return f"{_BASE}/gb/store?text={q}"


def _parse_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "")
    s = re.sub(r"[^\d.]+", "", s)
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _canonicalize(href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None
    # Keep only /gb/<slug> without query/fragment
    p = urlparse(urljoin(_BASE, href))
    if "eneba.com" not in (p.netloc or ""):
        return None
    if not p.path.startswith("/gb/"):
        return None
    if not _PRODUCT_SLUG_RE.match(p.path):
        return None
    return f"{p.scheme}://{p.netloc}{p.path}".split("?")[0]


def _score_candidate(canon_url: str, title: str) -> int:
    """
    Heuristic scoring: higher is better.
    Uses URL slug only (fast + stable).
    """
    p = urlparse(canon_url)
    h = p.path.lower()

    # must be /gb/ product slug-like
    if not _PRODUCT_SLUG_RE.match(h):
        return -10

    slug = h.rsplit("/", 1)[-1]
    score = 0

    # hard rejects
    for w in _BAD_SLUG_WORDS:
        if w in slug:
            return -50

    # strong positives
    if "steam" in slug:
        score += 8
    if "key" in slug:
        score += 8
    if "pc" in slug:
        score += 6

    # prefer explicit "steam-key" patterns
    if "steam-key" in slug or ("steam" in slug and "key" in slug):
        score += 6

    # prefer global (small bonus)
    if "global" in slug:
        score += 2

    # title overlap (rough)
    title_words = [w for w in re.split(r"[^a-z0-9]+", title.lower()) if len(w) >= 3]
    overlap = sum(1 for w in title_words if w in slug)
    score += min(overlap, 8)

    # penalize “too generic” slugs
    if overlap <= 1:
        score -= 2

    return score


def resolve_product_url_from_store(store_url: str, title: str) -> Tuple[Optional[str], str]:
    """
    Picks the best matching product URL from Eneba store search results.
    """
    try:
        r = _SESS.get(store_url, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return None, f"failed (http error: {type(e).__name__})"

    if r.status_code in (403, 429):
        return None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, f"failed (http {r.status_code})"

    soup = BeautifulSoup(r.text, "lxml")

    best_url: Optional[str] = None
    best_score = -10**9

    # (A) normal anchor scan
    for a in soup.select("a[href]"):
        canon = _canonicalize(a.get("href") or "")
        if not canon:
            continue
        sc = _score_candidate(canon, title)
        if sc > best_score:
            best_score, best_url = sc, canon

    # (B) regex fallback for hard-to-find anchors
    if not best_url or best_score < 6:
        found = re.findall(r'https://www\.eneba\.com/gb/[a-z0-9\-]{8,}', r.text.lower())
        for full in found:
            canon = full.split("?")[0]
            sc = _score_candidate(canon, title)
            if sc > best_score:
                best_score, best_url = sc, canon

    if best_url and best_score >= 6:
        return best_url, f"ok (store->product scored={best_score})"

    return None, "failed (no suitable product link found)"


def _walk(obj: Any) -> Iterable[Any]:
    stack = [obj]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


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
    if not meta_price or not meta_cur:
        return None
    if str(meta_cur.get("content") or "").upper() != "GBP":
        return None
    return _parse_float(meta_price.get("content"))


def _extract_price_from_next_data(html_text: str) -> Optional[float]:
    """
    Eneba is often Next.js; pricing is frequently embedded in __NEXT_DATA__ JSON.
    We look for dicts that contain GBP + a plausible numeric price.
    """
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return None

    raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return None

    # Heuristic: find nodes with currency GBP and a numeric value, or price nodes tagged as GBP
    for node in _walk(data):
        if not isinstance(node, dict):
            continue

        cur = node.get("currency") or node.get("priceCurrency") or node.get("currencyCode")
        if str(cur).upper() != "GBP":
            continue

        for k in ("amount", "value", "price", "currentPrice", "finalPrice", "lowestPrice"):
            p = _parse_float(node.get(k))
            if p is not None:
                return p

    return None


def _extract_price_from_visible_gbp(html_text: str) -> Optional[float]:
    """
    Last resort regex, but try to avoid grabbing random '£' values.
    Search near purchase UI terms first.
    """
    lower = html_text.lower()
    needles = ("buy now", "add to cart", "add to basket", "checkout", "purchase")
    idxs = [lower.find(n) for n in needles if lower.find(n) != -1]

    windows = []
    if idxs:
        i = min(idxs)
        windows.append(html_text[max(0, i - 3000) : min(len(html_text), i + 3000)])
    else:
        windows.append(html_text)

    for w in windows:
        m = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", w)
        if m:
            return _parse_float(m.group(1))
    return None


def read_price_gbp(url: str) -> Tuple[Optional[float], str]:
    """
    Safer price extraction order:
    1) JSON-LD offers with priceCurrency GBP
    2) meta[itemprop=price] + meta[itemprop=priceCurrency]==GBP
    3) __NEXT_DATA__ (Next.js JSON) GBP (big reliability win)
    4) visible '£' near purchase UI (last resort)
    """
    try:
        r = _SESS.get(url, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return None, f"failed (http error: {type(e).__name__})"

    if r.status_code in (403, 429):
        return None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, f"failed (http {r.status_code})"

    soup = BeautifulSoup(r.text, "lxml")

    p = _extract_price_from_ldjson(soup)
    if p is not None:
        return p, "ok (JSON-LD offers GBP)"

    p = _extract_price_from_meta(soup)
    if p is not None:
        return p, "ok (META price+currency GBP)"

    p = _extract_price_from_next_data(r.text)
    if p is not None:
        return p, "ok (__NEXT_DATA__ GBP)"

    p = _extract_price_from_visible_gbp(r.text)
    if p is not None:
        return p, "ok (REGEX £ price, UI-window)"

    return None, "failed (no GBP price found)"
