from __future__ import annotations

from ebayflip.safety import safe_external_url


def test_safe_external_url_allows_http_https() -> None:
    assert safe_external_url("https://example.com/x") == "https://example.com/x"
    assert safe_external_url("http://example.com") == "http://example.com"


def test_safe_external_url_rejects_unsafe_schemes_and_auth() -> None:
    assert safe_external_url("javascript:alert(1)") is None
    assert safe_external_url("data:text/html,abc") is None
    assert safe_external_url("https://user:pass@example.com") is None
