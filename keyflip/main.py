from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

from .cache import PriceCache
from .core import RunConfig, build_watchlist, scan_watchlist

log = logging.getLogger("keyflip")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Keyflip (no Playwright) — build & scan")

    mode = p.add_mutually_exclusive_group(required=False)
    mode.add_argument("--play", action="store_true", help="Build watchlist then scan (default)")
    mode.add_argument("--build", action="store_true", help="Build watchlist only")
    mode.add_argument("--scan", action="store_true", help="Scan existing watchlist only")

    p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root directory (defaults to current working directory)",
    )

    # --- Core targets / limits ---
    p.add_argument("--max-buy", type=float, default=10.0, help="Max buy price in GBP")
    p.add_argument("--watchlist-target", type=int, default=10, help="Target rows to write to watchlist.csv")
    p.add_argument("--verify-candidates", type=int, default=220, help="How many candidates to consider before verifying")
    p.add_argument("--pages-per-source", type=int, default=2, help="Pages to harvest per source")

    p.add_argument("--verify-limit", type=int, default=10, help="How many candidates to verify (0 = use safety cap)")
    p.add_argument("--verify-safety-cap", type=int, default=14, help="Hard upper cap on verifies per run")
    p.add_argument("--scan-limit", type=int, default=10, help="How many watchlist rows to scan (0 = unlimited)")

    p.add_argument("--avoid-recent-days", type=int, default=2, help="Avoid re-scanning items seen in the last N days")

    # --- Currency ---
    p.add_argument("--allow-eur", action="store_true", help="Allow EUR prices (converted to GBP)")
    p.add_argument("--eur-to-gbp", type=float, default=0.86, help="EUR→GBP rate (used only with --allow-eur)")

    # --- Budgets / timeouts ---
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    p.add_argument("--item-budget", type=float, default=55.0, help="Best-effort per-item time budget (seconds)")
    p.add_argument("--run-budget", type=float, default=0.0, help="Total run budget (seconds); 0 disables")

    # --- Cache ---
    p.add_argument("--cache-fail-ttl", type=int, default=1200, help="TTL seconds for failed cache entries")
    p.add_argument("--clear-cache", action="store_true", help="Clear price cache and exit")
    p.add_argument("--clear-recent", action="store_true", help="Clear recent-scans tracking and exit")

    return p


def _resolve_root(args_root: Optional[Path]) -> Path:
    # Most reliable default across local, GitHub, streamlit, etc.
    root = args_root if args_root is not None else Path.cwd()
    return root.resolve()


def _watchlist_is_usable(path: Path) -> bool:
    # "Usable" means: file exists, not empty, and has at least a header line.
    try:
        if not path.exists() or not path.is_file():
            return False
        if path.stat().st_size < 10:
            return False
        # Lightweight header check (avoid pandas here)
        with path.open("r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        return bool(first) and ("," in first)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,  # make sure our config wins even if something configured logging earlier
    )

    root = _resolve_root(args.root)

    watchlist_csv = root / "watchlist.csv"
    scans_csv = root / "scans.csv"
    passes_csv = root / "passes.csv"
    db_path = root / "price_cache.sqlite"

    cache = PriceCache(db_path)

    # --- Cache maintenance exits ---
    if args.clear_cache:
        cache.clear_cache()
        log.info("Cache cleared: %s", db_path.resolve())
        return 0

    if args.clear_recent:
        cache.clear_recent()
        log.info("Recent cleared.")
        return 0

    # --- Normalize verify limits ---
    verify_limit = int(args.verify_limit)
    verify_safety_cap = int(args.verify_safety_cap)

    if verify_limit < 0:
        log.warning("--verify-limit < 0 is invalid. Using default 10.")
        verify_limit = 10

    if verify_safety_cap <= 0:
        log.warning("--verify-safety-cap <= 0 is invalid. Using default 14.")
        verify_safety_cap = 14

    # If user says "0 = unlimited", we still keep safety behavior predictable:
    # Treat 0 as "use safety cap" to avoid accidental huge runs.
    if verify_limit == 0:
        log.info("verify_limit=0 -> using safety cap=%d", verify_safety_cap)
        verify_limit = verify_safety_cap

    cfg = RunConfig(
        root=root,
        max_buy_gbp=float(args.max_buy),
        watchlist_target=int(args.watchlist_target),
        verify_candidates=int(args.verify_candidates),
        pages_per_source=int(args.pages_per_source),
        verify_limit=verify_limit,
        verify_safety_cap=verify_safety_cap,
        scan_limit=int(args.scan_limit),
        avoid_recent_days=int(args.avoid_recent_days),
        allow_eur=bool(args.allow_eur),
        eur_to_gbp=float(args.eur_to_gbp),
        item_budget_s=float(args.item_budget),
        run_budget_s=float(args.run_budget),
        cache_fail_ttl_s=int(args.cache_fail_ttl),
    )

    # Default mode = play
    do_play = bool(args.play) or (not args.build and not args.scan)
    do_build = bool(args.build) or do_play
    do_scan = bool(args.scan) or do_play

    dfw = None

    if do_build:
        log.info("Building watchlist...")
        dfw = build_watchlist(cfg, cache, watchlist_csv)

        # dfw might be None if builder chose to return nothing; handle safely
        built_rows = len(dfw) if dfw is not None else 0
        log.info("Wrote %s rows to %s", built_rows, watchlist_csv.resolve())

        # If build produced empty (or None), don't scan in the same run.
        if do_scan and (dfw is None or getattr(dfw, "empty", True)):
            log.warning("Watchlist is empty (this run). Skipping scan.")
            return 0

    if do_scan:
        # Guard scan-only runs too
        if not _watchlist_is_usable(watchlist_csv):
            log.warning(
                "watchlist.csv missing/empty/unusable at %s. Run with --build (or --play) first.",
                watchlist_csv.resolve(),
            )
            return 0

        log.info("Scanning watchlist...")
        batch = scan_watchlist(cfg, cache, watchlist_csv, scans_csv, passes_csv)

        try:
            batch_rows = len(batch)
        except Exception:
            batch_rows = 0

        log.info("Scan batch rows: %s", batch_rows)
        log.info("Wrote scans to %s and passes to %s", scans_csv.resolve(), passes_csv.resolve())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
