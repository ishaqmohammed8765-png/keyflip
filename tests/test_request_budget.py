from __future__ import annotations

import pytest

from ebayflip.config import RunSettings
from ebayflip.ebay_client import EbayClient, RequestBudget, RequestLimitError


class _DummyResponse:
    status_code = 200
    headers = {}
    text = "<html></html>"
    url = "https://example.test/search"

    def raise_for_status(self) -> None:
        return None


def test_shared_request_budget_limits_across_clients(monkeypatch) -> None:
    budget = RequestBudget(2)
    settings = RunSettings(request_cap=99)
    client_a = EbayClient(settings, request_budget=budget)
    client_b = EbayClient(settings, request_budget=budget)

    monkeypatch.setattr(client_a.session, "get", lambda *args, **kwargs: _DummyResponse())
    monkeypatch.setattr(client_b.session, "get", lambda *args, **kwargs: _DummyResponse())

    client_a._request("https://example.test/a", use_cache=False, store_cache=False, max_attempts=1)
    client_b._request("https://example.test/b", use_cache=False, store_cache=False, max_attempts=1)
    with pytest.raises(RequestLimitError):
        client_a._request("https://example.test/c", use_cache=False, store_cache=False, max_attempts=1)

    assert budget.used == 2
    assert client_a.cap_reached() is True

