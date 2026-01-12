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

# CheapShark
_CHEAPSHARK_BASE = "https://www.cheapshark.com/api/1.0"
_CHEAPSHARK_STORE_FANATICAL = "15"
_CHEAPSHARK_REDIRECT = "https://www.cheapshark.com/redirect?dealID="

# Cache so core calling harvest_game_links multiple times doesn't refetch repeatedly
_HARVEST_CACHE: Dict[str, Tuple[float, List[str]]] = {}
_HARVEST_CACHE_TTL_S = 60.0

# Locale handling:
# Accept /en/... and /en-gb/... etc.
_LOCALE_EN_RE = re.compile(r"^/en(?:-[a-z]{2})?/", re.I)

# Some redirects land on /game/... without /en/ prefix (rare but real)
_GAME_OR_EN_RE = re.compile(r"^/(?:en(?:-[a-z]{2})?/)?game/", re.I)

# Filter to games (recommended default)
_GAME_PATH_RE = re.compile(r"^/en(?:-[a-z]{2})?/game/", re.I)


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


def _timeout_sleep(lo_hi: Tuple[float, float]) -> None:
    lo, hi = lo_hi
    if hi <= 0:
        return
    time.sleep(random.uniform(max(0.0, lo), max(0.0, hi)))


# ---------------------------
# URL canonicalization
# ---------------------------
def _is_fanatical_host(url: str) -> bool:
    try:
        return (urlparse(url).netloc or "").lower() in _ALLOWED_HOSTS
    except Exception:
        return False


def _canonicalize_any_fanatical(url: str) -> Optional[str]:
    """
    Canonicalize Fanatical URL and accept:
      - /en/... or /en-gb/... etc
      - OR /game/... (some redirects land here)
    Strips query/fragment and trailing slash.
    """
    try:
        p = urlparse(url)
    except Exception:
        return None

    host = (p.netloc or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None

    path = (p.path or "").rstrip("/")
    if not path:
        return None

    ok = bool(_LOCALE_EN_RE.match(path + "/") or _GAME_OR_EN_RE.match(path + "/"))
    if not ok:
        return None

    scheme = p.scheme or "https"
    return f"{scheme}://{host}{path}"


def _path_is_game(url: str) -> bool:
    path = urlparse(url).path or ""
    return bool(_GAME_PATH_RE.match(path + "/"))


# ---------------------------
# CheapShark harvesting
# ---------------------------
def _cheapshark_fetch_deals(page_number: int, page_size: int) -> List[dict]:
    url = f"{_CHEAPSHARK_BASE}/deals"
    params = {
        "storeID": _CHEAPSHARK_STORE_FANATICAL,
        "pageNumber": max(0, int(page_number)),
        "pageSize": max(1, int(page_size)),
    }

    try:
        r = _SESS.get(url, params=params, timeout=_timeout())
    except requests.RequestException as e:
        log.warning("CheapShark deals request failed (%s)", type(e).__name__)
        return []

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
    Robust CheapShark redirect resolution.

    Your log: no_fan_url=120 means *every* redirect resolution returned None.
    This function:
      - tries HEAD (fast)
      - then manually walks redirects (allow_redirects=False) so we can see failures
      - logs useful reasons on failure
      - canonicalizes Fanatical locale paths (/en-gb/ etc)
    """
    deal_id = (deal_id or "").strip()
    if not deal_id:
        return None

    redir = _CHEAPSHARK_REDIRECT + deal_id

    # 1) Try HEAD + follow redirects
    try:
        r = _SESS.head(redir, timeout=_timeout(), allow_redirects=True)
        final = (r.url or "").strip()
        if final and _is_fanatical_host(final):
            canon = _canonicalize_any_fanatical(final)
            if canon:
                return canon
    except requests.RequestException:
        pass

    # 2) Manual redirect walk
    url = redir
    for hop in range(8):
        try:
            r = _SESS.get(url, timeout=_timeout(), allow_redirects=False)
        except requests.RequestException as e:
            log.warning(
                "CheapShark redirect GET failed deal_id=%s hop=%d err=%s",
                deal_id,
                hop,
                type(e).__name__,
            )
            return None

        status = int(r.status_code)

        # If we landed (2xx)
        if 200 <= status < 300:
            final = (r.url or "").strip()

            # Fanatical page reached
            if final and _is_fanatical_host(final):
                canon = _canonicalize_any_fanatical(final)
                if canon:
                    return canon

                log.warning(
                    "Fanatical URL reached but rejected by canonicalizer deal_id=%s final=%s",
                    deal_id,
                    final,
                )
                return None

            # Still on cheapshark (no redirect happened)
            host = (urlparse(final).netloc or "").lower()
            if "cheapshark.com" in host:
                log.warning(
                    "CheapShark redirect did not redirect deal_id=%s (HTTP %s) final=%s",
                    deal_id,
                    status,
                    final,
                )
                return None

            # Ended on some other domain
            log.warning("Redirect ended non-fanatical deal_id=%s final=%s", deal_id, final)
            return None

        # 3xx: follow Location
        if 300 <= status < 400:
            loc = r.headers.get("Location") or r.headers.get("location")
            if not loc:
                log.warning("Redirect missing Location deal_id=%s hop=%d url=%s", deal_id, hop, url)
                return None
            url = urljoin(url, loc)
            continue

        # 4xx/5xx: fail with info
        log.warning(
            "CheapShark redirect bad status deal_id=%s hop=%d HTTP=%s url=%s",
            deal_id,
            hop,
            status,
            url,
        )
        return None

    log.warning("Redirect exceeded hops deal_id=%s", deal_id)
    return None


def harvest_game_links(
    source_url: str,
    pages: int,
    *,
    max_links: int = 500,
    sleep_range_s: Tuple[float, float] = (0.15, 0.45),
    games_only: bool = True,
) -> List[str]:
    """
    Harvest Fanatical links via CheapShark.

    Key behaviors:
    - Fanatical listing pages can be JS-rendered -> CheapShark is more reliable.
    - Accept /en-gb/ and other locale paths.
    - games_only=True keeps your pipeline from dying on bundles/DLC pages.
    - Adds reject-reason counters so you can debug empty watchlists fast.
    - IMPORTANT: does NOT cache empty results (so failures don't stick for 60s).
    """
    pages = max(1, int(pages))
    max_links = max(1, int(max_links))

    # source_url is ignored (kept for interface compatibility with old pipeline)
    cache_key = f"cheapshark:{pages}:{max_links}:{int(games_only)}"
    now = time.time()
    cached = _HARVEST_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _HARVEST_CACHE_TTL_S:
        log.info(
            "Harvest cache hit (%ds old) returning %d urls",
            int(now - cached[0]),
            len(cached[1]),
        )
        return list(cached[1])

    out: List[str] = []
    seen: set[str] = set()

    page_size = 60

    # Debug counters
    cat_counts: Dict[str, int] = {}
    reasons = {
        "no_deal_id": 0,
        "no_fan_url": 0,
        "deduped": 0,
        "not_game": 0,
    }

    for page in range(0, pages):
        deals = _cheapshark_fetch_deals(page_number=page, page_size=page_size)
        log.info(
            "CheapShark fetched page=%d deals=%d (Fanatical storeID=%s)",
            page,
            len(deals),
            _CHEAPSHARK_STORE_FANATICAL,
        )

        random.shuffle(deals)

        for d in deals:
            deal_id = str(d.get("dealID") or "").strip()
            if not deal_id:
                reasons["no_deal_id"] += 1
                continue

            fan_url = _resolve_cheapshark_dealid_to_fanatical_url(deal_id)
            if not fan_url:
                reasons["no_fan_url"] += 1
                continue

            if fan_url in seen:
                reasons["deduped"] += 1
                continue

            if games_only and not _path_is_game(fan_url):
                reasons["not_game"] += 1
                continue

            seen.add(fan_url)
            out.append(fan_url)

            # category debug
            path = urlparse(fan_url).path or ""
            parts = [p for p in path.split("/") if p]
            cat = parts[1] if len(parts) >= 2 else "en"
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

            if len(out) >= max_links:
                log.info("Harvest hit max_links=%d", max_links)
                break

        if len(out) >= max_links:
            break

        _timeout_sleep(sleep_range_s)

    log.info("Total harvested Fanatical URLs via CheapShark: %d", len(out))
    log.info("Harvest reject reasons: %s", reasons)
    if cat_counts:
        log.info(
            "Harvest categories: %s",
            dict(sorted(cat_counts.items(), key=lambda kv: kv[1], reverse=True)),
        )

    # IMPORTANT: don't cache empties (helps debugging; avoids 0-url cache hits)
    if out:
        _HARVEST_CACHE[cache_key] = (time.time(), list(out))

    return out


# ---------------------------
# Title + price extraction (GBP-only)
# ---------------------------
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
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html_text,
        re.S,
    )
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
        cur = node.get("currency") or node.get("priceCurrency") or node.get("currencyCode")
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
