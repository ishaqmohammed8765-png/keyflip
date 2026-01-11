from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import HTTP_TIMEOUT_S, UA


@dataclass
class FanaticalItem:
    title: str
    url: str


_BASE = "https://www.fanatical.com"


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


# Reuse one session for connection pooling
_SESS = _session()


def harvest_game_links(source_url: str, pages: int) -> List[str]:
    """
    Best-effort harvest:
    - prefers product-card-ish links when possible
    - falls back to scanning all <a href> but validates path
    - returns deduped canonical URLs (no querystrings)
    """
    links: List[str] = []

    for page in range(1, max(1, pages) + 1):
        url = source_url
        if page > 1:
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}page={page}"

        try:
            r = _SESS.get(url, timeout=HTTP_TIMEOUT_S)
            if r.status_code in (403, 429):
                # blocked/rate-limited; stop early rather than crashing
                break
            r.raise_for_status()
        except Exception:
            # fail softly for this page
            continue

        soup = BeautifulSoup(r.text, "lxml")

        # If Fanatical changes markup, this might be empty; we keep the generic fallback too.
        # Generic fallback: scan all anchors but validate the path strongly.
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue

            full = urljoin(url, href)
            p = urlparse(full)

            # Strong path validation
            if not p.path.startswith("/en/game/"):
                continue

            # Canonicalize: drop query/fragment
            canon = f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
            links.append(canon)

    # dedupe while keeping order
    seen = set()
    out: List[str] = []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    """
    Attempts (GBP):
    1) meta[itemprop=price] + meta[itemprop=priceCurrency]==GBP (best)
    2) JSON-LD offers with priceCurrency GBP (safe)
    3) regex fallback '£12.34' (last resort)
    Returns: (title, price, notes)
    """
    try:
        r = _SESS.get(url, timeout=HTTP_TIMEOUT_S)
        if r.status_code in (403, 429):
            return None, None, f"failed (blocked HTTP {r.status_code})"
        r.raise_for_status()
    except Exception as e:
        return None, None, f"failed (http error: {type(e).__name__})"

    soup = BeautifulSoup(r.text, "lxml")

    # Title: prefer an on-page heading if present, else <title>
    title = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    elif soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
        title = re.sub(r"\s*\|\s*Fanatical\s*$", "", title).strip()

    # Meta price + currency (strongest)
    meta_price = soup.select_one('meta[itemprop="price"]')
    meta_cur = soup.select_one('meta[itemprop="priceCurrency"]')
    if meta_price and meta_price.get("content") and meta_cur and meta_cur.get("content"):
        if str(meta_cur["content"]).upper() == "GBP":
            try:
                return title, float(meta_price["content"]), "ok (META price+currency GBP)"
            except Exception:
                pass

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
                p = off.get("price")
                if cur == "GBP" and p is not None:
                    try:
                        return title, float(p), "ok (JSON-LD offers GBP)"
                    except Exception:
                        pass

    # Visible £ fallback
    m = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", r.text)
    if m:
        try:
            return title, float(m.group(1)), "ok (REGEX £ price)"
        except Exception:
            pass

    return title, None, "failed (no GBP price found)"
