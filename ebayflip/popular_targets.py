from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PopularTarget:
    category: str
    name: str
    query: str
    max_buy_gbp: float | None = None


POPULAR_TARGETS: tuple[PopularTarget, ...] = (
    PopularTarget("smartphones", "Apple iPhone 14 Pro", "apple iphone 14 pro", 460.0),
    PopularTarget("smartphones", "Apple iPhone 15 Pro", "apple iphone 15 pro", 650.0),
    PopularTarget("smartphones", "Samsung Galaxy S23 Ultra", "samsung galaxy s23 ultra", 520.0),
    PopularTarget("smartphones", "Google Pixel 8 Pro", "google pixel 8 pro", 430.0),
    PopularTarget("gaming", "Nintendo Switch OLED", "nintendo switch oled", 180.0),
    PopularTarget("gaming", "PlayStation 5 Slim", "playstation 5 slim", 360.0),
    PopularTarget("gaming", "Xbox Series X", "xbox series x", 310.0),
    PopularTarget("gaming", "Steam Deck OLED", "steam deck oled", 360.0),
    PopularTarget("audio", "AirPods Pro 2", "airpods pro 2", 130.0),
    PopularTarget("audio", "Sony WH-1000XM5", "sony wh-1000xm5", 190.0),
    PopularTarget("audio", "Bose QuietComfort Ultra", "bose quietcomfort ultra", 220.0),
    PopularTarget("wearables", "Apple Watch Series 9", "apple watch series 9", 230.0),
    PopularTarget("wearables", "Garmin Fenix 7", "garmin fenix 7", 340.0),
    PopularTarget("wearables", "Galaxy Watch 6 Classic", "galaxy watch 6 classic", 190.0),
    PopularTarget("computing", "MacBook Air M2", "macbook air m2", 740.0),
    PopularTarget("computing", "iPad Pro 11", "ipad pro 11", 520.0),
    PopularTarget("computing", "RTX 4070", "rtx 4070 graphics card", 440.0),
    PopularTarget("cameras", "Sony A7 III", "sony a7 iii", 900.0),
    PopularTarget("cameras", "Canon EOS R6", "canon eos r6", 1100.0),
    PopularTarget("cameras", "Fujifilm X-T4", "fujifilm x-t4", 820.0),
)


def get_popular_targets(*, per_category: int = 3) -> list[PopularTarget]:
    per_category = max(1, int(per_category))
    counts: dict[str, int] = {}
    selected: list[PopularTarget] = []
    for target in POPULAR_TARGETS:
        count = counts.get(target.category, 0)
        if count >= per_category:
            continue
        counts[target.category] = count + 1
        selected.append(target)
    return selected

