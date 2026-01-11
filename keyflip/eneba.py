from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from .config import HTTP_TIMEOUT_S, UA


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    return s


def make_store_search_url(title: str) -> str:
    # matches your old pattern
    q = quote_plus(f"{title} steam key pc")
    return f"https://www.eneba.com/gb/store?text={q}"


def resolve_product_url_from_store(store_url: str) -> Tuple[Optional[str], str]:
    """
    Eneba store search often links to product pages:
    https://www.eneba.com/gb/steam-...-pc-steam-key-global
    We pick the first plausible product URL.
    """
    s = _session()
    r = s.get(store_url, timeout=HTTP_TIMEOUT_S)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    candidates = []
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        # prefer steam product pages with /gb/ and "-pc-" and "steam-key"
        if href.startswith("/gb/") and "steam" in href and "key" in href:
            full = urljoin("https://www.eneba.com", href)
            candidates.append(full)

    if candidates:
        return candidates[0].split("?")[0], "ok (store->product link)"

    # fallback: find full URL in html
    m = re.search(r"https://www\.eneba\.com/gb/[a-z0-9\-]+", r.text)
    if m:
        return m.group(0).split("?")[0], "ok (regex fallback)"

    return None, "failed (no product link found)"


def read_price_gbp(url: str) -> Tuple[Optional[float], str]:
    """
    Best-effort price extraction from product page.
    Tries:
    1) meta[itemprop=price]
    2) JSON-LD offers price
    3) __NEXT_DATA__ or raw regex '"price":"12.34"'
    4) visible currency pattern "£12.34"
    """
    s = _session()
    r = s.get(url, timeout=HTTP_TIMEOUT_S)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    meta_price = soup.select_one('meta[itemprop="price"]')
    if meta_price and meta_price.get("content"):
        try:
            return float(meta_price["content"]), "ok (META itemprop=price)"
        except Exception:
            pass

    for script in soup.select('script[type="application/ld+json"]'):
        txt = script.get_text(strip=True) or ""
        if "offers" in txt and "price" in txt:
            m = re.search(r'"price"\s*:\s*"?(?P<p>\d+(\.\d+)?)"?', txt)
            if m:
                try:
                    return float(m.group("p")), "ok (JSON-LD offers)"
                except Exception:
                    pass

    # NEXT_DATA / general JSON price fields
    m = re.search(r'"price"\s*:\s*"?(?P<p>\d+(\.\d+)?)"?', r.text)
    if m:
        try:
            return float(m.group("p")), "ok (regex JSON price)"
        except Exception:
            pass

    # £ fallback
    m = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", r.text)
    if m:
        try:
            return float(m.group(1)), "ok (REGEX £ price)"
        except Exception:
            pass

    return None, "failed (no price found)"

