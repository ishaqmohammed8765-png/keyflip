from __future__ import annotations

from types import SimpleNamespace

import serve


def _reset_scan_trigger_state() -> None:
    serve._LAST_SCAN_TRIGGER_MONO = 0.0


def test_scan_run_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SCAN_RUN_ENABLED", raising=False)
    monkeypatch.delenv("SCAN_RUN_TOKEN", raising=False)
    _reset_scan_trigger_state()
    client = serve.app.test_client()
    response = client.post("/api/scan/run")
    assert response.status_code == 403


def test_scan_run_requires_token(monkeypatch) -> None:
    monkeypatch.setenv("SCAN_RUN_ENABLED", "1")
    monkeypatch.setenv("SCAN_RUN_TOKEN", "secret-token")
    _reset_scan_trigger_state()
    client = serve.app.test_client()
    response = client.post("/api/scan/run")
    assert response.status_code == 401


def test_scan_run_rate_limited(monkeypatch) -> None:
    monkeypatch.setenv("SCAN_RUN_ENABLED", "1")
    monkeypatch.setenv("SCAN_RUN_TOKEN", "secret-token")
    monkeypatch.setenv("SCAN_RUN_MIN_INTERVAL_SECONDS", "3600")
    monkeypatch.setattr(
        serve.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    _reset_scan_trigger_state()
    client = serve.app.test_client()
    headers = {"X-Scan-Token": "secret-token"}
    first = client.post("/api/scan/run", headers=headers)
    second = client.post("/api/scan/run", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 429

