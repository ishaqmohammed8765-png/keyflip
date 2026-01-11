from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .cache import PriceCache
from .core import RunConfig, build_watchlist, scan_watchlist

log = logging.getLogger("keyflip")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Keyflip (no Playwright) — build & scan")
    mode = p.add_mutually_exclusive_group(required=False)
    mode.add_argument("--play", action="store_true", help="Build watchlist then scan")
    mode.add_argument("--build", action="store_true", help="Build watchlist only")
    mode.add_argument("--scan", action="store_true", help="Scan watchlist only")

    p.add_argument("--max-buy", type=float, default=10.0)
    p.add_argument("--watchlist-target", type=int, default=10)
    p.add_argument("--verify-candidates", type=int, default=220)
    p.add_argument("--pages-per-source", type=int, default=2)

    p.add_argument("--verify-limit", type=int, default=10, help="0 = unlimited")
    p.add_argument("--verify-safety-cap", type=int, default=14)
    p.add_argument("--scan-limit", type=int, default=10, help="0 = unlimited")

    p.add_argument("--avoid-recent-days", type=int, default=2)

    p.add_argument("--allow-eur", action="store_true")
    p.add_argument("--eur-to-gbp", type=float, default=0.86)

    p.add_argument("--debug", action="store_true")

    p.add_argument("--item-budget", type=float, default=55.0, help="Best-effort (no Playwright)")
    p.add_argument("--run-budget", type=float, default=0.0, help="0 disables")

    p.add_argument("--cache-fail-ttl", type=int, default=1200)

    p.add_argument("--clear-cache", action="store_true")
    p.add_argument("--clear-recent", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # repo root (../.. from keyflip/main.py)
    root = Path(__file__).resolve().parent.parent
    watchlist_csv = root / "watchlist.csv"
    scans_csv = root / "scans.csv"
    passes_csv = root / "passes.csv"
    db_path = root / "price_cache.sqlite"

    cache = PriceCache(db_path)

    if args.clear_cache:
        cache.clear_cache()
        log.info("Cache cleared.")
        return 0

    if args.clear_recent:
        cache.clear_recent()
        log.info("Recent cleared.")
        return 0

    cfg = RunConfig(
        root=root,
        max_buy_gbp=float(args.max_buy),
        watchlist_target=int(args.watchlist_target),
        verify_candidates=int(args.verify_candidates),
        pages_per_source=int(args.pages_per_source),
        verify_limit=int(args.verify_limit),
        verify_safety_cap=int(args.verify_safety_cap),
        scan_limit=int(args.scan_limit),
        avoid_recent_days=int(args.avoid_recent_days),
        allow_eur=bool(args.allow_eur),
        eur_to_gbp=float(args.eur_to_gbp),
        item_budget_s=float(args.item_budget),
        run_budget_s=float(args.run_budget),
        cache_fail_ttl_s=int(args.cache_fail_ttl),
    )

    do_play = args.play or (not args.build and not args.scan)
    do_build = args.build or do_play
    do_scan = args.scan or do_play

    dfw = None
    if do_build:
        log.info("Building watchlist...")
        dfw = build_watchlist(cfg, cache, watchlist_csv)
        log.info("Wrote %s rows to %s", len(dfw), watchlist_csv.resolve())

    # IMPORTANT: If play/build produced an empty watchlist, skip scan (prevents pandas crash)
    if do_scan and dfw is not None and dfw.empty:
        log.warning("Watchlist is empty. Skipping scan.")
        return 0

    if do_scan:
        log.info("Scanning watchlist...")
        batch = scan_watchlist(cfg, cache, watchlist_csv, scans_csv, passes_csv)
        log.info("Scan batch rows: %s", len(batch))
        log.info("Wrote scans to %s and passes to %s", scans_csv.resolve(), passes_csv.resolve())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
