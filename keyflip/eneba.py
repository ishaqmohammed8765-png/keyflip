ffrom __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from bs4.builder import FeatureNotFound

from .config import HTTP_TIMEOUT_S, UA

log = logging.getLogger("keyflip.eneba")

_BASE = "https://www.eneba.com"

# Reject obvious non-key / wrong product types.
# NOTE: we check these as *tokens* in the slug to reduce false rejects.
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
    # intentionally NOT including plain "card" because it causes false negatives
}

# Eneba product pages under /gb/ are usually a single slug segment:
#   /gb/game-title-pc-steam-key-global
_PRODUCT_SLUG_RE = re.compile(r"^/gb/[a-z0-9-]{8,}$", re.I)

# Regex fallback for extracting candidate product URLs from HTML
_PRODUCT_URL_RE = re.compile(r"https?://(?:www\.)?eneba\.com/gb/[a-z0-9\-]{8,}", re.I)

# Regex last-resort for visible GBP
_GBP_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")


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

    # Retry/backoff on transient issues (optional)
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
        # If urllib3 Retry API changes or missing, we still want the module to work.
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
def _timeout_value() -> Any:
    """
    Support both:
      - HTTP_TIMEOUT_S = 20
      - HTTP_TIMEOUT_S = (connect_timeout, read_timeout)
    """
    t = HTTP_TIMEOUT_S
    if isinstance(t, tuple) and len(t) == 2:
        return t
    return float(t)


def _soup(html: str) -> BeautifulSoup:
    """
    Prefer lxml if available, otherwise fallback to html.parser.
    """
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


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
    host = (netloc or "").lower()
    # strip port if present
    host = host.split(":")[0]
    return host == "eneba.com" or host.endswith(".eneba.com")


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

    if not _is_eneba_host(p.netloc):
        return None

    path = (p.path or "").strip()
    if not path.startswith("/gb/"):
        return None

    path_norm = path.lower()

    # Strict-ish: only /gb/<single-slug>
    if not _PRODUCT_SLUG_RE.match(path_norm):
        return None

    # Canonicalize host to www + https, strip query/fragment
    return urlunparse(("https", "www.eneba.com", path_norm, "", "", ""))


def _slug_tokens(canon_url: str) -> list[str]:
    p = urlparse(canon_url)
    slug = (p.path or "").lower().rsplit("/", 1)[-1]
    # tokens but also keep "gift-card" style by joining pairs is unnecessary; slug contains hyphens already
    return [t for t in slug.split("-") if t]


def _score_candidate(canon_url: str, title: str) -> int:
    """
    Heuristic scoring: higher is better.
    Uses URL slug only (fast + stable).
    """
    p = urlparse(canon_url)
    path = (p.path or "").lower()

    if not _PRODUCT_SLUG_RE.match(path):
        return -10

    slug = path.rsplit("/", 1)[-1]
    tokens = _slug_tokens(canon_url)

    # Reject bad product types (token-based, lower false-negative risk)
    slug_joined = "-".join(tokens)
    for bad in _BAD_SLUG_TOKENS:
        if bad in slug_joined:
            return -50

    score = 0

    # positives
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

    # title overlap
    title_words = [
        w
        for w in re.split(r"[^a-z0-9]+", (title or "").lower())
        if len(w) >= 3
    ]
    overlap = sum(1 for w in title_words if w in slug)
    score += min(overlap, 8)

    if overlap <= 1:
        score -= 2

    return score


def _http_get(url: str) -> Tuple[Optional[requests.Response], Optional[str]]:
    try:
        r = _SESS.get(url, timeout=_timeout_value())
        return r, None
    except requests.RequestException as e:
        return None, f"failed (http error: {type(e).__name__})"


# -----------------------------------------------------------------------------
# Store resolver
# -----------------------------------------------------------------------------
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
    soup = _soup(html)

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
    """
    Eneba (Next.js) often embeds prices in __NEXT_DATA__.
    Look for nodes that indicate GBP and contain a plausible numeric value.
    """
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

    # Common numeric keys that show up in Next payloads
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
    """
    Last resort regex. We prefer searching near purchase UI terms to avoid
    grabbing unrelated £ amounts (like shipping, bundle savings, etc.).
    """
    html_text = html_text or ""
    lower = html_text.lower()

    needles = (
        "buy now",
        "add to cart",
        "add to basket",
        "checkout",
        "purchase",
        "buy",
    )

    idxs = [lower.find(n) for n in needles if lower.find(n) != -1]

    windows: list[str] = []
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
