from ebayflip.ebay_client import _detect_blocked_detail, _safe_close_playwright


def test_detect_blocked_from_url() -> None:
    url = "https://www.ebay.co.uk/splashui/challenge?foo=bar"
    assert _detect_blocked_detail(url, "", listing_container_present=True) is not None


def test_detect_blocked_from_html_keywords() -> None:
    html = "<html><title>Pardon our interruption</title></html>"
    assert _detect_blocked_detail("https://www.ebay.co.uk/", html, listing_container_present=True)


def test_detect_blocked_from_missing_container() -> None:
    html = "<html><body><div>no listings here</div></body></html>"
    detail = _detect_blocked_detail("https://www.ebay.co.uk/", html, listing_container_present=False)
    assert detail == "missing_listing_container"


class DummyCloser:
    def __init__(self, *, closed: bool = False, raise_on_close: bool = False) -> None:
        self._closed = closed
        self._raise_on_close = raise_on_close

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._raise_on_close:
            raise RuntimeError("already closed")
        self._closed = True


class DummyResource:
    def __init__(self, *, raise_on_close: bool = False) -> None:
        self._raise_on_close = raise_on_close

    def close(self) -> None:
        if self._raise_on_close:
            raise RuntimeError("already closed")


def test_safe_close_playwright_handles_closed_resources() -> None:
    page = DummyCloser(closed=True, raise_on_close=True)
    context = DummyResource(raise_on_close=True)
    browser = DummyResource(raise_on_close=True)
    _safe_close_playwright(page, context, browser)
