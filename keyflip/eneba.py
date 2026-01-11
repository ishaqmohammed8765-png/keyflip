from __future__ import annotations

import json
import re
from typing import Optional, Tuple
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from .config import HTTP_TIMEOUT_S, UA

_BASE = "https://www.eneba.com"

# Reject obvious non-game / not-a-key / wrong product types
_BAD_SLUG_WORDS = {
    "gift-card", "wallet", "prepaid", "card",
    "dlc", "season-pass", "seasonpass", "add-on", "addon",
    "bundle", "soundtrack", "artbook", "expansion",
}

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return s

# one shared session for connection pooling
_SESS = _session()


def make_store_search_url(title: str) -> str:
    q = quote_plus(f"{title} steam key pc")
    return f"{_BASE}/gb/store?text={q}"


def _score_candidate(href: str, title: str) -> int:
    """
    Heuristic scoring: higher is better.
    """
    h = href.lower()
    score = 0

    # must be gb product page
    if not h.startswith("/gb/"):
        return -10

    # strong positives
    if "steam-key" in h:
        score += 10
    if "-pc-" in h or "pc" in h:
        score += 6
    if "steam" in h and "key" in h:
        score += 3

    # prefer global (optional – keep if you only want global)
    if "global" in h:
        score += 2

    # penalize bad types
    for w in _BAD_SLUG_WORDS:
        if w in h:
            score -= 50

    # soft title match: more shared “words” in slug = better
    # (very rough, but helps pick the exact game vs similarly named items)
    slug = h.rsplit("/", 1)[-1]
    title_words = [w for w in re.split(r"[^a-z0-9]+", title.lower()) if len(w) >= 3]
    overlap = sum(1 for w in title_words if w in slug)
    score += min(overlap, 6)

    return score


def resolve_product_url_from_store(store_url: str, title: str) -> Tuple[Optional[str], str]:
    """
    Picks the best matching product URL from Eneba store search results.
    """
    try:
        r = _SESS.get(store_url, timeout=HTTP_TIMEOUT_S)
        if r.status_code in (403, 429):
            return None, f"failed (blocked HTTP {r.status_code})"
        r.raise_for_status()
    except Exception as e:
        return None, f"failed (http error: {type(e).__name__})"

    soup = BeautifulSoup(r.text, "lxml")

    best_url: Optional[str] = None
    best_score = -10**9

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href.startswith("/gb/"):
            continue
        if "steam" not in href or "key" not in href:
            continue

        sc = _score_candidate(href, title)
        if sc > best_score:
            best_score = sc
            best_url = urljoin(_BASE, href)

    if best_url and best_score >= 5:
        return best_url.split("?")[0], f"ok (store->product scored={best_score})"

    # fallback: find product-ish urls in html, then score
    found = re.findall(r'https://www\.eneba\.com/gb/[a-z0-9\-]+', r.text.lower())
    for full in found:
        href = full.replace(_BASE, "")
        sc = _score_candidate(href, title)
        if sc > best_score:
            best_score = sc
            best_url = full

    if best_url and best_score >= 5:
        return best_url.split("?")[0], f"ok (regex fallback scored={best_score})"

    return None, "failed (no suitable product link found)"


def read_price_gbp(url: str) -> Tuple[Optional[float], str]:
    """
    Safer price extraction:
    1) JSON-LD offers with priceCurrency GBP
    2) meta[itemprop=price] + meta[itemprop=priceCurrency]==GBP
    3) Visible '£' pattern (last resort)
    """
    try:
        r = _SESS.get(url, timeout=HTTP_TIMEOUT_S)
        if r.status_code in (403, 429):
            return None, f"failed (blocked HTTP {r.status_code})"
        r.raise_for_status()
    except Exception as e:
        return None, f"failed (http error: {type(e).__name__})"

    soup = BeautifulSoup(r.text, "lxml")

    # (1) JSON-LD
    for script in soup.select('script[type="application/ld+json"]'):
        txt = script.get_text(strip=True) or ""
        if not txt:
            continue
        # Some pages have multiple JSON-LD blobs; parse cautiously
        try:
            data = json.loads(txt)
        except Exception:
            continue

        # data can be dict or list
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
                p = off.get("price")
                if cur == "GBP" and p is not None:
                    try:
                        return float(p), "ok (JSON-LD offers GBP)"
                    except Exception:
                        pass

    # (2) meta itemprop price + currency
    meta_price = soup.select_one('meta[itemprop="price"]')
    meta_cur = soup.select_one('meta[itemprop="priceCurrency"]')
    if meta_price and meta_price.get("content") and meta_cur and meta_cur.get("content"):
        if str(meta_cur["content"]).upper() == "GBP":
            try:
                return float(meta_price["content"]), "ok (META price+currency GBP)"
            except Exception:
                pass

    # (3) last resort: visible £xx.xx
    m = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", r.text)
    if m:
        try:
            return float(m.group(1)), "ok (REGEX £ price fallback)"
        except Exception:
            pass

    return None, "failed (no GBP price found)"
