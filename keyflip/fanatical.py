from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import HTTP_TIMEOUT_S, UA


@dataclass
class FanaticalItem:
    title: str
    url: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    return s


def harvest_game_links(source_url: str, pages: int) -> List[str]:
    """
    Best-effort harvest:
    - grabs links that look like /en/game/...
    - dedupes
    """
    s = _session()
    links: List[str] = []
    for page in range(1, pages + 1):
        url = source_url
        # Fanatical often uses ?page=2
        if page > 1:
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}page={page}"

        r = s.get(url, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if "/en/game/" in href:
                full = urljoin(url, href)
                links.append(full)

    # dedupe while keeping order
    seen = set()
    out = []
    for u in links:
        u = u.split("?")[0]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def read_title_and_price_gbp(url: str) -> Tuple[Optional[str], Optional[float], str]:
    """
    Attempts:
    1) meta[itemprop=price]
    2) JSON-LD offer price
    3) regex fallback
    Returns: (title, price, notes)
    """
    s = _session()
    r = s.get(url, timeout=HTTP_TIMEOUT_S)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # title
    title = None
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
        title = re.sub(r"\s*\|\s*Fanatical\s*$", "", title).strip()

    # meta price
    meta_price = soup.select_one('meta[itemprop="price"]')
    if meta_price and meta_price.get("content"):
        try:
            return title, float(meta_price["content"]), "ok (META itemprop=price)"
        except Exception:
            pass

    # JSON-LD offers
    for script in soup.select('script[type="application/ld+json"]'):
        txt = script.get_text(strip=True) or ""
        if "offers" in txt and "price" in txt:
            m = re.search(r'"price"\s*:\s*"?(?P<p>\d+(\.\d+)?)"?', txt)
            if m:
                try:
                    return title, float(m.group("p")), "ok (JSON-LD offers)"
                except Exception:
                    pass

    # regex fallback: look for £12.34
    m = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", r.text)
    if m:
        try:
            return title, float(m.group(1)), "ok (REGEX £ price)"
        except Exception:
            pass

    return title, None, "failed (no price found)"

