from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .config import HTTP_TIMEOUT_S, UA


@dataclass(frozen=True)
class FanaticalItem:
    title: str
    url: str


_BASE = "https://www.fanatical.com"
_ALLOWED_HOSTS = {"www.fanatical.com", "fanatical.com"}

# Retry policy (lightweight, safe defaults)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.7  # exponential backoff base


def _session() -> requests.Session:
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
    return s


# Reuse one session for connection pooling
_SESS = _session()


def _sleep_backoff(attempt: int) -> None:
    # exponential backoff with jitter
    delay = (_BACKOFF_BASE_S * (2 ** max(0, attempt - 1))) + random.uniform(0.0, 0.25)
    time.sleep(delay)


def _get(url: str) -> Optional[requests.Response]:
    """
    GET with small retry/backoff for transient errors.
    Returns Response or None on hard failure.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            r = _SESS.get(url, timeout=HTTP_TIMEOUT_S)
        except requests.RequestException:
            if attempt >= _MAX_RETRIES:
                return None
            _sleep_backoff(attempt)
            continue

        if r.status_code in (403,):
            # likely blocked; don't hammer
            return r

        if r.status_code in _RETRY_STATUSES:
            if attempt >= _MAX_RETRIES:
                return r
            _sleep_backoff(attempt)
            continue

        return r

    return None


def _canonicalize(url: str) -> Optional[str]:
    """
    Canonicalize URL:
    - enforce https
    - strip query/fragment
    - strip trailing slash
