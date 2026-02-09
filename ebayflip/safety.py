from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


def safe_external_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    value = str(url).strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return value
