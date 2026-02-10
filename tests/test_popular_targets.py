from __future__ import annotations

from ebayflip.popular_targets import get_popular_targets


def test_get_popular_targets_limits_per_category() -> None:
    targets = get_popular_targets(per_category=2)
    counts: dict[str, int] = {}
    for target in targets:
        counts[target.category] = counts.get(target.category, 0) + 1
    assert counts
    assert all(value <= 2 for value in counts.values())

