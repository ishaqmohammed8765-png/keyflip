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
from .fanatical import harvest_game_links, read_title_and_price_gbp

log = logging.getLogger("keyflip")


# =========================
# CLI
# =========================
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Keyflip (no Playwright) — build & scan")

    mode = p.add_mutually_exclusive_group(required=False)
    mode.add_argument("--play", action="store_true", help="Build then scan (default)")
    mode.add_argument("--build", action="store_true", help="Build watchlist only")
    mode.add_argument("--scan", action="store_true", help="Scan watchlist only")

    p.add_argument("--root", type=Path, default=None, help="Project root (default: current working directory)")

    # Build settings
    p.add_argument("--max-buy", type=float, default=10.0, help="Max buy price in GBP")
    p.add_argument("--watchlist-target", type=int, default=10, help="Target watchlist size")
    p.add_argument("--verify-candidates", type=int, default=220, help="Candidate pool size before verify")
    p.add_argument("--pages-per-source", type=int, default=2, help="Pages to harvest per source")

    p.add_argument("--verify-limit", type=int, default=10, help="Verifies per run (0 = use safety cap)")
    p.add_argument("--verify-safety-cap", type=int, default=14, help="Hard upper cap on verifies")

    # Scan settings
    p.add_argument("--scan-limit", type=int, default=10, help="Rows scanned per run (0 = unlimited)")
    p.add_argument("--avoid-recent-days", type=int, default=2, help="Avoid items scanned in last N days")

    # Currency
    p.add_argument("--allow-eur", action="store_true")
    p.add_argument("--eur-to-gbp", type=float, default=0.86)

    # Budgets / timeouts
    p.add_argument("--item-budget", type=float, default=55.0, help="Best-effort per-item time budget seconds")
    p.add_argument("--run-budget", type=float, default=0.0, help="Total run budget seconds (0 disables)")

    # Cache
    p.add_argument("--cache-fail-ttl", type=int, default=1200, help="TTL for failed cache entries (seconds)")
    p.add_argument("--clear-cache", action="store_true", help="Clear price cache and exit")
    p.add_argument("--clear-recent", action="store_true", help="Clear recent-items tracking and exit")

    # Debug
    p.add_argument("--debug", action="store_true", help="Enable debug logging")

    # Diagnostics
    p.add_argument("--diag-harvest", action="store_true", help="Print harvested link counts per source and exit")
    p.add_argument("--diag-price", type=int, default=0, help="Test read_title_and_price_gbp on N sampled URLs and exit")
    p.add_argument(
        "--diag-seed",
        type=int,
        default=1337,
        help="Seed for diagnostic sampling/shuffle (only used by diag tools)",
    )
    p.add_argument(
        "--diag-shuffle",
        action="store_true",
        help="Shuffle diagnostic URL pool (reproducible with --diag-seed)",
    )

    return p


def _resolve_root(args_root: Optional[Path]) -> Path:
    return (args_root if args_root is not None else Path.cwd()).resolve()


def _log_paths(root: Path) -> None:
    log.info("ROOT: %s", root)
    log.info("watchlist.csv: %s", (root / "watchlist.csv").resolve())
    log.info("scans.csv: %s", (root / "scans.csv").resolve())
    log.info("passes.csv: %s", (root / "passes.csv").resolve())
    log.info("cache db: %s", (root / "price_cache.sqlite").resolve())


def _watchlist_is_usable(path: Path) -> bool:
    """
    Quick sanity check:
    - exists, non-trivial size
    - has a header line with commas
    - has at least one data row (>= 2 lines total)
    """
    try:
        if not path.exists() or not path.is_file():
            return False
        if path.stat().st_size < 20:
            return False

        with path.open("r", encoding="utf-8", errors="replace") as f:
            header = f.readline().strip()
            row1 = f.readline().strip()

        if not header or "," not in header:
            return False
        if not row1:
            return False

        return True
    except Exception:
        return False


# =========================
# Diagnostics
# =========================
def _run_diag_harvest(pages_per_source: int) -> int:
    log.info("DIAG: Harvesting Fanatical sources (pages_per_source=%d)", pages_per_source)
    total_unique_per_source = 0

    for name, src_url in FANATICAL_SOURCES.items():
        try:
            links = harvest_game_links(src_url, pages=pages_per_source)
            uniq = list(dict.fromkeys(links))
            total_unique_per_source += len(uniq)
            log.info(
                "source=%s raw=%d unique=%d sample=%s",
                name,
                len(links),
                len(uniq),
                uniq[:5],
            )
        except Exception:
            log.exception("DIAG harvest failed for source=%s url=%s", name, src_url)

    log.info("DIAG: total unique (sum across sources, not cross-deduped): %d", total_unique_per_source)
    return 0


def _build_diag_pool(pages_per_source: int) -> list[str]:
    pool: list[str] = []
    for _, src_url in FANATICAL_SOURCES.items():
        try:
            pool.extend(harvest_game_links(src_url, pages=pages_per_source))
        except Exception:
            log.exception("DIAG: harvest failed while building pool: %s", src_url)

    # Dedup, preserve order
    seen = set()
    uniq: list[str] = []
    for u in pool:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _run_diag_price(pages_per_source: int, n: int, *, seed: int, shuffle: bool) -> int:
    if n <= 0:
        return 0

    log.info("DIAG: Price-reading %d sampled Fanatical URLs", n)
    uniq = _build_diag_pool(pages_per_source=pages_per_source)

    if not uniq:
        log.warning("DIAG: no harvested URLs found, cannot test price reader.")
        log.warning("Try: --diag-harvest --pages-per-source 2 (or increase pages)")
        return 0

    if shuffle:
        r = random.Random(seed)
        r.shuffle(uniq)
        log.info("DIAG: shuffled pool with seed=%d", seed)

    test_urls = uniq[:n]
    for i, url in enumerate(test_urls, 1):
        try:
            title, price, notes = read_title_and_price_gbp(url)
            log.info("DIAG %d/%d url=%s title=%r price=%r notes=%s", i, n, url, title, price, notes)
        except Exception:
            log.exception("DIAG: read_title_and_price_gbp crashed for url=%s", url)

    return 0


# =========================
# Config normalization
# =========================
@dataclass(frozen=True)
class NormalizedLimits:
    verify_limit: int
    verify_safety_cap: int


def _normalize_verify_limits(verify_limit: int, verify_safety_cap: int) -> NormalizedLimits:
    cap = int(verify_safety_cap) if int(verify_safety_cap) > 0 else 14
    v = int(verify_limit)

    if v < 0:
        log.warning("--verify-limit < 0 is invalid; using 10")
        v = 10

    # Make 0 predictable (avoid accidental “verify hundreds”)
    if v == 0:
        log.info("verify_limit=0 -> using safety cap=%d", cap)
        v = cap

    if v > cap:
        # You can still allow it if your core uses safety cap separately,
        # but at least be explicit.
        log.info("verify_limit=%d requested; safety cap=%d will still apply in core.", v, cap)

    return NormalizedLimits(verify_limit=v, verify_safety_cap=cap)


def _log_run_plan(do_build: bool, do_scan: bool, args: argparse.Namespace) -> None:
    mode = "PLAY" if (do_build and do_scan) else ("BUILD" if do_build else "SCAN")
    log.info("Mode: %s", mode)
    log.info(
        "Limits: max_buy=£%.2f watchlist_target=%d verify_candidates=%d pages_per_source=%d",
        float(args.max_buy),
        int(args.watchlist_target),
        int(args.verify_candidates),
        int(args.pages_per_source),
    )
    log.info(
        "Scan: scan_limit=%s avoid_recent_days=%d",
        ("unlimited" if int(args.scan_limit) == 0 else str(int(args.scan_limit))),
        int(args.avoid_recent_days),
    )
    log.info(
        "Budgets: item_budget=%.1fs run_budget=%s",
        float(args.item_budget),
        ("disabled" if float(args.run_budget) <= 0 else f"{float(args.run_budget):.1f}s"),
    )


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
    db_path = root / "price_cache.sqlite"

    cache = PriceCache(db_path)

    # Maintenance exits
    if args.clear_cache:
        try:
            cache.clear_cache()
            log.info("Cache cleared.")
            return 0
        except Exception:
            log.exception("Failed to clear cache.")
            return 2

    if args.clear_recent:
        try:
            cache.clear_recent()
            log.info("Recent cleared.")
            return 0
        except Exception:
            log.exception("Failed to clear recent.")
            return 2

    # Diagnostics exits
    if args.diag_harvest:
        return _run_diag_harvest(pages_per_source=int(args.pages_per_source))

    if int(args.diag_price) > 0:
        return _run_diag_price(
            pages_per_source=int(args.pages_per_source),
            n=int(args.diag_price),
            seed=int(args.diag_seed),
            shuffle=bool(args.diag_shuffle),
        )

    limits = _normalize_verify_limits(int(args.verify_limit), int(args.verify_safety_cap))

    cfg = RunConfig(
        root=root,
        max_buy_gbp=float(args.max_buy),
        watchlist_target=int(args.watchlist_target),
        verify_candidates=int(args.verify_candidates),
        pages_per_source=int(args.pages_per_source),
        verify_limit=int(limits.verify_limit),
        verify_safety_cap=int(limits.verify_safety_cap),
        scan_limit=int(args.scan_limit),
        avoid_recent_days=int(args.avoid_recent_days),
        allow_eur=bool(args.allow_eur),
        eur_to_gbp=float(args.eur_to_gbp),
        item_budget_s=float(args.item_budget),
        run_budget_s=float(args.run_budget),
        cache_fail_ttl_s=int(args.cache_fail_ttl),
    )

    do_play = bool(args.play) or (not args.build and not args.scan)
    do_build = bool(args.build) or do_play
    do_scan = bool(args.scan) or do_play

    _log_run_plan(do_build, do_scan, args)

    # -------------------------
    # BUILD
    # -------------------------
    dfw = None
    if do_build:
        log.info("Building watchlist...")
        try:
            dfw = build_watchlist(cfg, cache, watchlist_csv)
        except Exception:
            log.exception("BUILD failed.")
            return 2

        built_rows = len(dfw) if dfw is not None else 0
        log.info("Built watchlist rows: %d", built_rows)
        log.info("Wrote watchlist to: %s", watchlist_csv.resolve())

        # If we built this run and got an empty watchlist, skip scan
        if do_scan and (dfw is None or getattr(dfw, "empty", True)):
            log.warning("Watchlist is empty. Skipping scan.")
            log.warning("Next steps:")
            log.warning("  1) Run: --diag-harvest")
            log.warning("  2) If harvest has links, run: --diag-price 5 --diag-shuffle")
            return 0

    # -------------------------
    # SCAN
    # -------------------------
    if do_scan:
        if not _watchlist_is_usable(watchlist_csv):
            log.warning("watchlist.csv missing/empty/unusable at %s", watchlist_csv.resolve())
            log.warning("Run with --build or --play first (or debug with --diag-harvest).")
            return 0

        log.info("Scanning watchlist...")
        try:
            batch = scan_watchlist(cfg, cache, watchlist_csv, scans_csv, passes_csv)
        except Exception:
            log.exception("SCAN failed.")
            return 2

        log.info("Scan batch rows: %d", len(batch))
        log.info("Wrote scans to: %s", scans_csv.resolve())
        log.info("Wrote passes to: %s", passes_csv.resolve())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
