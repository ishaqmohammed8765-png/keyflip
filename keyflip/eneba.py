# ==========================
# keyflip/eneba.py (rewrite)
# ==========================
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Iterable, Optional, Tuple, List
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, FeatureNotFound

from .config import HTTP_TIMEOUT_S, UA

log = logging.getLogger("keyflip.eneba")

_BASE = "https://www.eneba.com"

_BAD_SLUG_TOKENS = {
    "gift-card",
    "wallet",
    "prepaid",
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

# Accept /gb/<slug> AND /gb/<category>/<slug>
_PRODUCT_PATH_RE = re.compile(r"^/gb/(?:[a-z0-9-]+/)?[a-z0-9-]{8,}$", re.I)

_PRODUCT_URL_RE = re.compile(
    r"https?://(?:www\.)?eneba\.com/gb/(?:[a-z0-9-]+/)?[a-z0-9\-]{8,}",
    re.I,
)

_GBP_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")


# -----------------------------------------------------------------------------
# Session
# -----------------------------------------------------------------------------
def _timeout_value() -> Any:
    t = HTTP_TIMEOUT_S
    if isinstance(t, tuple) and len(t) == 2:
        return t
    return float(t)


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

    # Keep urllib3 retry (nice to have), but throttling is the real protection.
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


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


# -----------------------------------------------------------------------------
# Public
# -----------------------------------------------------------------------------
def make_store_search_url(title: str) -> str:
    q = quote_plus(f"{title or ''} steam key pc")
    return f"{_BASE}/gb/store?text={q}"


# -----------------------------------------------------------------------------
# Helpers
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


def _is_eneba_host(netloc: str) -> bool:
    host = (netloc or "").lower().split(":")[0]
    return host == "eneba.com" or host.endswith(".eneba.com")


def _canonicalize(href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None

    full = urljoin(_BASE, href)
    try:
        p = urlparse(full)
    except Exception:
        return None

    if not _is_eneba_host(p.netloc):
        return None

    path = (p.path or "").strip()
    if not path.startswith("/gb/"):
        return None

    path_norm = path.lower()
    if not _PRODUCT_PATH_RE.match(path_norm):
        return None

    return urlunparse(("https", "www.eneba.com", path_norm, "", "", ""))


def _slug_tokens(canon_url: str) -> List[str]:
    p = urlparse(canon_url)
    slug = (p.path or "").lower().rsplit("/", 1)[-1]
    return [t for t in slug.split("-") if t]


def _score_candidate(canon_url: str, title: str) -> int:
    p = urlparse(canon_url)
    path = (p.path or "").lower()

    if not _PRODUCT_PATH_RE.match(path):
        return -10

    slug = path.rsplit("/", 1)[-1]
    tokens = _slug_tokens(canon_url)

    slug_joined = "-".join(tokens)
    for bad in _BAD_SLUG_TOKENS:
        if bad in slug_joined:
            return -50

    score = 0
    if "steam" in tokens or "steam" in slug:
        score += 8
    if "key" in tokens or "key" in slug:
        score += 8
    if "pc" in tokens or "pc" in slug:
        score += 6
    if ("steam" in slug and "key" in slug) or "steam-key" in slug:
        score += 6
    if "global" in tokens or "global" in slug:
        score += 2

    title_words = [w for w in re.split(r"[^a-z0-9]+", (title or "").lower()) if len(w) >= 3]
    overlap = 0
    for w in title_words:
        if w in slug:
            overlap += 1
    score += min(overlap, 8)
    if overlap <= 1:
        score -= 2

    return score


# -----------------------------------------------------------------------------
# Throttling + 429 backoff + invalid URL guard
# -----------------------------------------------------------------------------
_THROTTLE_NEXT_OK: float = 0.0
_BACKOFF_S: float = 2.0


def _looks_like_block_page(html: str) -> bool:
    t = (html or "").lower()
    signals = (
        "captcha",
        "access denied",
        "verify you are",
        "cloudflare",
        "rate limit",
        "too many requests",
        "unusual traffic",
        "blocked",
    )
    return any(s in t for s in signals)


def _http_get(url: str) -> Tuple[Optional[requests.Response], Optional[str]]:
    global _THROTTLE_NEXT_OK, _BACKOFF_S

    u = (url or "").strip()
    if not u or u.lower() == "nan" or not (u.startswith("http://") or u.startswith("https://")):
        return None, "failed (invalid url)"

    # Steady pacing to avoid burst 429s
    now = time.time()
    if now < _THROTTLE_NEXT_OK:
        time.sleep(_THROTTLE_NEXT_OK - now)
    _THROTTLE_NEXT_OK = time.time() + (2.2 + random.random() * 1.6)  # ~2.2–3.8s

    try:
        r = _SESS.get(u, timeout=_timeout_value())
    except requests.RequestException as e:
        return None, f"failed (http error: {type(e).__name__})"

    if r.status_code == 429:
        ra = r.headers.get("Retry-After")
        if ra:
            try:
                sleep_s = float(ra)
            except Exception:
                sleep_s = _BACKOFF_S
        else:
            sleep_s = _BACKOFF_S

        sleep_s = sleep_s + random.random() * 1.0
        time.sleep(sleep_s)
        _BACKOFF_S = min(_BACKOFF_S * 1.8, 30.0)
        return r, "failed (blocked HTTP 429)"

    if r.status_code < 400:
        _BACKOFF_S = 2.0

    ct = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ct and _looks_like_block_page(r.text or ""):
        return r, "failed (blocked challenge page)"

    return r, None


# -----------------------------------------------------------------------------
# Store resolver
# -----------------------------------------------------------------------------
def resolve_product_url_from_store(store_url: str, title: str) -> Tuple[Optional[str], str]:
    r, err = _http_get(store_url)
    if err:
        return None, err
    assert r is not None

    if r.status_code in (403, 429):
        return None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, f"failed (http {r.status_code})"

    html = r.text or ""
    soup = _soup(html)

    best_url: Optional[str] = None
    best_score = -10**9

    for a in soup.select("a[href]"):
        canon = _canonicalize(a.get("href") or "")
        if not canon:
            continue
        sc = _score_candidate(canon, title)
        if sc > best_score:
            best_score, best_url = sc, canon

    if not best_url or best_score < 6:
        for full in _PRODUCT_URL_RE.findall(html):
            canon = _canonicalize(full)
            if not canon:
                continue
            sc = _score_candidate(canon, title)
            if sc > best_score:
                best_score, best_url = sc, canon

    if best_url and best_score >= 6:
        return best_url, f"ok (store->product scored={best_score})"

    return None, "failed (no suitable product link found)"


# -----------------------------------------------------------------------------
# Price extraction
# -----------------------------------------------------------------------------
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
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html_text or "",
        re.S | re.I,
    )
    if not m:
        return None

    raw = (m.group(1) or "").strip()
    try:
        data = json.loads(raw)
    except Exception:
        return None

    candidate_keys = (
        "amount",
        "value",
        "price",
        "currentPrice",
        "finalPrice",
        "lowestPrice",
        "basePrice",
        "discountedPrice",
        "priceAmount",
    )

    for node in _walk(data):
        if not isinstance(node, dict):
            continue

        cur = node.get("currency") or node.get("priceCurrency") or node.get("currencyCode")
        if str(cur).upper() != "GBP":
            continue

        for k in candidate_keys:
            p = _parse_float(node.get(k))
            if p is not None:
                return p

    return None


def _extract_price_from_visible_gbp(html_text: str) -> Optional[float]:
    html_text = html_text or ""
    lower = html_text.lower()

    needles = ("buy now", "add to cart", "add to basket", "checkout", "purchase", "buy")
    idxs = [lower.find(n) for n in needles if lower.find(n) != -1]

    windows: List[str] = []
    if idxs:
        i = min(idxs)
        windows.append(html_text[max(0, i - 3500) : min(len(html_text), i + 3500)])
    else:
        windows.append(html_text)

    for w in windows:
        m = _GBP_RE.search(w)
        if m:
            return _parse_float(m.group(1))
    return None


def read_price_gbp(url: str) -> Tuple[Optional[float], str]:
    r, err = _http_get(url)
    if err:
        return None, err
    assert r is not None

    if r.status_code in (403, 429):
        return None, f"failed (blocked HTTP {r.status_code})"
    if r.status_code >= 400:
        return None, f"failed (http {r.status_code})"

    html = r.text or ""
    soup = _soup(html)

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
