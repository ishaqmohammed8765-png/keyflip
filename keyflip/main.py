from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .config import RunConfig
from .core import build_watchlist, scan_watchlist

log = logging.getLogger("keyflip")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Keyflip — eBay mispricing radar")

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build-watchlist", action="store_true", help="Create watchlist template")
    mode.add_argument("--scan", action="store_true", help="Run a single scan")
    mode.add_argument("--play", action="store_true", help="Run continuous scans")

    p.add_argument("--root", type=Path, default=None, help="Project root (default: cwd)")
    p.add_argument("--scan-interval", type=int, default=300, help="Seconds between scans in play mode")
    p.add_argument("--scan-limit", type=int, default=50, help="Queries to scan per run (0 = all)")
    p.add_argument("--allow-non-gbp", action="store_true", help="Convert non-GBP currencies")

    p.add_argument("--ebay-app-id", type=str, default=None, help="eBay App ID (or set EBAY_APP_ID)")
    p.add_argument("--rate-limit-per-min", type=int, default=60)
    p.add_argument("--max-retries", type=int, default=3)

    p.add_argument("--debug", action="store_true")

    return p


def _resolve_root(p: Optional[Path]) -> Path:
    return (p if p else Path.cwd()).resolve()


def _log_paths(root: Path) -> None:
    log.info("ROOT: %s", root)
    log.info("watchlist.csv: %s", root / "watchlist.csv")
    log.info("scans.csv: %s", root / "scans.csv")
    log.info("passes.csv: %s", root / "passes.csv")
    log.info("cache db: %s", root / "price_cache.sqlite")


def _build_config(args: argparse.Namespace, root: Path) -> RunConfig:
    return RunConfig.from_kwargs(
        root=root,
        ebay_app_id=args.ebay_app_id or os.getenv("EBAY_APP_ID"),
        rate_limit_per_min=args.rate_limit_per_min,
        max_retries=args.max_retries,
        allow_non_gbp=args.allow_non_gbp,
        scan_limit=args.scan_limit,
    )


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
    cache_db = root / "price_cache.sqlite"

    cfg = _build_config(args, root)

    if args.build_watchlist:
        build_watchlist(cfg, watchlist_csv, overwrite=True)
        return 0

    if args.scan:
        scan_watchlist(cfg, watchlist_csv, scans_csv, passes_csv, cache_db)
        return 0

    if args.play:
        interval = max(30, int(args.scan_interval))
        while True:
            log.info("Running scan cycle...")
            scan_watchlist(cfg, watchlist_csv, scans_csv, passes_csv, cache_db)
            log.info("Sleeping for %d seconds", interval)
            time.sleep(interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
