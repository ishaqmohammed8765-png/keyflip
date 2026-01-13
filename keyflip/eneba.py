from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .config import HTTP_TIMEOUT_S, UA

log = logging.getLogger("keyflip.eneba")

_BASE = "https://www.eneba.com"

# Reject obvious non-key / wrong product types
_BAD_SLUG_WORDS = {
    "gift-card",
    "wallet",
    "prepaid",
    "card",
    "dlc",
    "season-pass",
    "seasonpass",
    "add-on",
    "addon",
    "bundle",
    "soundtrack",
    "artbook",
    "expansion",
}

# Eneba product pages under /gb/ are usually a single slug segment:
#   /gb/game-title-pc-steam-key-global
# Use case-insensitive match and allow longer slugs.
_PRODUCT_SLUG_RE = re.compile(r"^/gb/[a-z0-9-]{8,}$", re.I)


# -----------------------------------------------------------------------------
# Session
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Public helpers
# -----------------------------------------------------------------------------
def make_store_search_url(title: str) -> str:
    q = quote_plus(f"{title} steam key pc")
    return f"{_BASE}/gb/store?text={q}"


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------
def _parse_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if v > 0 else None
    s = str(x).strip().replace(",", "")
    s = re.sub(r"[^\d.]+", "", s)
    if not s:
        return None
    try:
        v = float(s)
        return v if v > 0 else None
    except Exception:
        return None


def _canonicalize(href: str) -> Optional[str]:
    """
    Canonicalize to https://www.eneba.com/gb/<slug> with no query/fragment,
    and only accept paths that look like a product slug.
    """
    href = (href or "").strip()
    if not href:
        return None

    full = urljoin(_BASE, href)
    try:
        p = urlparse(full)
    except Exception:
        return None

    host = (p.netloc or "").lower()
    if "eneba.com" not in host:
        return None

    path = (p.path or "").strip()
    if not path.startswith("/gb/"):
        return None

    # normalize path for matching/scoring (Eneba slugs are effectively case-insensitive)
    path_norm = path.lower()

    # Strict-ish: only /gb/<single-slug> forms
    if not _PRODUCT_SLUG_RE.match(path_norm):
        return None

    # Always canonicalize host to www + https, strip query/fragment
    return urlunparse(("https", "www.eneba.com", path_norm, "", "", ""))


def _score_candidate(canon_url: str, title: str) -> int:
    """
    Heuristic scoring: higher is better.
    Uses URL slug only (fast + stable).
    """
    p = urlparse(canon_url)
    h = (p.path or "").lower()

    if not _PRODUCT_SLUG_RE.match(h):
        return -10

    slug = h.rsplit("/", 1)[-1]
    score = 0

    for w in _BAD_SLUG_WORDS:
        if w in slug:
            return -50

    # positives
    if "steam" in slug:
        score += 8
    if "key" in slug:
        score += 8
    if "pc" in slug:
        score += 6

    if "steam-key" in slug or ("steam" in slug and "key" in slug):
        score += 6

    if "global" in slug:
        score += 2

    title_words = [w for w in re.split(r"[^a-z0-9]+", (title or "").lower()) if len(w) >= 3]
    overlap = sum(1 for w in title_words if w in slug)
    score += min(overlap, 8)

    if overlap <= 1:
        score -= 2

    return score


def _http_get(url: str) -> Tuple[Optional[requests.Response], Optional[str]]:
    try:
        r = _SESS.get(url, timeout=HTTP_TIMEOUT_S)
        return r, None
    except requests.RequestException as e:
        return None, f"failed (http error: {type(e).__name__})"


def resolve_product_url_from_store(store_url: str, title: str) -> Tuple[Optional[str], str]:
    """
    Pick the best matching product URL from Eneba store search results.
    """
    r, err = _http_get(store_url)
    if err:
        return None, err
    assert r is not None

    if r.status_code in (403, 429):
        return None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, f"failed (http {r.status_code})"

    html = r.text or ""
    soup = BeautifulSoup(html, "lxml")

    best_url: Optional[str] = None
    best_score = -10**9

    # (A) anchor scan
    for a in soup.select("a[href]"):
        canon = _canonicalize(a.get("href") or "")
        if not canon:
            continue
        sc = _score_candidate(canon, title)
        if sc > best_score:
            best_score, best_url = sc, canon

    # (B) regex fallback (wider net: www + non-www)
    if not best_url or best_score < 6:
        found = re.findall(r"https?://(?:www\.)?eneba\.com/gb/[a-z0-9\-]{8,}", html, flags=re.I)
        for full in found:
            canon = _canonicalize(full)
            if not canon:
                continue
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

                for k in ("price", "lowPrice", "highPrice"):
                    p = _parse_float(off.get(k))
                    if p is not None:
                        return p
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
    Eneba (Next.js) often embeds prices in __NEXT_DATA__.
    Look for nodes that indicate GBP and contain a plausible numeric value.
    """
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text or "", re.S | re.I)
    if not m:
        return None

    raw = (m.group(1) or "").strip()
    try:
        data = json.loads(raw)
    except Exception:
        return None

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
    html_text = html_text or ""
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
    Price extraction order:
      1) JSON-LD offers with GBP
      2) META itemprop price + currency GBP
      3) __NEXT_DATA__ (Next.js JSON) GBP
      4) visible '£' near purchase UI terms
    """
    r, err = _http_get(url)
    if err:
        return None, err
    assert r is not None

    if r.status_code in (403, 429):
        return None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, f"failed (http {r.status_code})"

    html = r.text or ""
    soup = BeautifulSoup(html, "lxml")

    p = _extract_price_from_ldjson(soup)
    if p is not None:
        return p, "ok (JSON-LD offers GBP)"

    p = _extract_price_from_meta(soup)
    if p is not None:
        return p, "ok (META price+currency GBP)"

    p = _extract_price_from_next_data(html)
    if p is not None:
        return p, "ok (__NEXT_DATA__ GBP)"

    p = _extract_price_from_visible_gbp(html)
    if p is not None:
        return p, "ok (REGEX £ price, UI-window)"

    return None, "failed (no GBP price found)"
