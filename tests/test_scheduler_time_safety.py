from __future__ import annotations

from ebayflip.scheduler import _comps_stale


def test_comps_stale_handles_naive_timestamp() -> None:
    assert _comps_stale("2026-01-01T00:00:00", 12) is True

