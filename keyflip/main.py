from __future__ import annotations

import argparse
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .cache import PriceCache
from .config import FANATICAL_SOURCES
from .core import RunConfig, build_watchlist, scan_watchlist
from .fanatical_pw import harvest_game_links, read_title_and_price_gbp

log = logging.getLogger("keyflip")


# =========================
# CLI
# =========================
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Keyflip — build & scan (Playwright Fanatical)")

    mode = p.add_mutually_exclusive_group(required=False)
    mode.add_argument("--play", action="store_true", help="Build then scan (default)")
    mode.add_argument("--build", action="store_true", help="Build watchlist only")
    mode.add_argument("--scan", action="store_true", help="Scan watchlist only")

    p.add_argument("--root", type=Path, default=None, help="Project root (default: cwd)")

    # Build
    p.add_argument("--max-buy", type=float, default=10.0)
    p.add_argument("--watchlist-target", type=int, default=10)
    p.add_argument("--verify-candidates", type=int, default=220)
    p.add_argument("--pages-per-source", type=int, default=2)

    p.add_argument("--verify-limit", type=int, default=10, help="0 = use safety cap")
    p.add_argument("--verify-safety-cap", type=int, default=14)

    # Scan
    p.add_argument("--scan-limit", type=int, default=10)
    p.add_argument("--avoid-recent-days", type=int, default=2)

    # Currency
    p.add_argument("--allow-eur", action="store_true")
    p.add_argument("--eur-to-gbp", type=float, default=0.86)

    # Budgets
    p.add_argument("--item-budget", type=float, default=55.0)
    p.add_argument("--run-budget", type=float, default=0.0)

    # Cache
    p.add_argument("--cache-fail-ttl", type=int, default=1200)
    p.add_argument("--clear-cache", action="store_true")
    p.add_argument("--clear-recent", action="store_true")

    # Debug / diag
    p.add_argument("--debug", action="store_true")
    p.add_argument("--diag-harvest", action="store_true")
    p.add_argument("--diag-price", type=int, default=0)
    p.add_argument("--diag-seed", type=int, default=1337)
    p.add_argument("--diag-shuffle", action="store_true")

    return p


# =========================
# Helpers
# =========================
def _resolve_root(p: Optional[Path]) -> Path:
    return (p if p else Path.cwd()).resolve()


def _log_paths(root: Path) -> None:
    log.info("ROOT: %s", root)
    log.info("watchlist.csv: %s", root / "watchlist.csv")
    log.info("scans.csv: %s", root / "scans.csv")
    log.info("passes.csv: %s", root / "passes.csv")
    log.info("cache db: %s", root / "price_cache.sqlite")


def _watchlist_ok(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 20:
            return False
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return bool(f.readline().strip() and f.readline().strip())
    except Exception:
        return False


# =========================
# Diagnostics
# =========================
def _run_diag_harvest(pages: int) -> int:
    for name, src in FANATICAL_SOURCES.items():
        links = harvest_game_links(src, pages=pages)
        uniq = list(dict.fromkeys(links))
        log.info("source=%s raw=%d unique=%d sample=%s", name, len(links), len(uniq), uniq[:5])
    return 0


def _run_diag_price(pages: int, n: int, *, seed: int, shuffle: bool) -> int:
    pool: list[str] = []
    for src in FANATICAL_SOURCES.values():
        pool.extend(harvest_game_links(src, pages=pages))

    uniq = list(dict.fromkeys(pool))
    if shuffle:
        random.Random(seed).shuffle(uniq)

    for url in uniq[:n]:
        title, price, notes = read_title_and_price_gbp(url)
        log.info("url=%s title=%r price=%r notes=%s", url, title, price, notes)

    return 0


# =========================
# Verify normalization
# =========================
@dataclass(frozen=True)
class Normalized:
    verify_limit: int
    safety_cap: int


def _normalize_verify(v: int, cap: int) -> Normalized:
    cap = cap if cap > 0 else 14
    if v <= 0:
        v = cap
    return Normalized(v, cap)


# =========================
# Main
# =========================
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    root = _resolve_root(args.root)
    _log_paths(root)

    watchlist_csv = root / "watchlist.csv"
    scans_csv = root / "scans.csv"
    passes_csv = root / "passes.csv"
    cache = PriceCache(root / "price_cache.sqlite")

    if args.clear_cache:
        cache.clear_cache()
        return 0
    if args.clear_recent:
        cache.clear_recent()
        return 0

    if args.diag_harvest:
        return _run_diag_harvest(args.pages_per_source)

    if args.diag_price > 0:
        return _run_diag_price(
            args.pages_per_source,
            args.diag_price,
            seed=args.diag_seed,
            shuffle=args.diag_shuffle,
        )

    v = _normalize_verify(args.verify_limit, args.verify_safety_cap)

    cfg = RunConfig(
        root=root,
        max_buy_gbp=args.max_buy,
        watchlist_target=args.watchlist_target,
        verify_candidates=args.verify_candidates,
        pages_per_source=args.pages_per_source,
        verify_limit=v.verify_limit,
        verify_safety_cap=v.safety_cap,
        scan_limit=args.scan_limit,
        avoid_recent_days=args.avoid_recent_days,
        allow_eur=args.allow_eur,
        eur_to_gbp=args.eur_to_gbp,
        item_budget_s=args.item_budget,
        run_budget_s=args.run_budget,
        cache_fail_ttl_s=args.cache_fail_ttl,
    )

    do_play = args.play or (not args.build and not args.scan)
    do_build = args.build or do_play
    do_scan = args.scan or do_play

    if do_build:
        build_watchlist(cfg, cache, watchlist_csv)

    if do_scan:
        if not _watchlist_ok(watchlist_csv):
            log.warning("watchlist.csv missing or empty — run --build first")
            return 0
        scan_watchlist(cfg, cache, watchlist_csv, scans_csv, passes_csv)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

