from __future__ import annotations

# IMPORTANT: set Playwright browser install dir BEFORE importing Playwright anywhere
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw-browsers")

import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import streamlit as st

from keyflip.config import RunConfig
from keyflip.core import build_watchlist, scan_watchlist
from keyflip.cache import PriceCache

# ------------------------------------------------------------
# Paths (relative to this app file)
# ------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.resolve()
WATCHLIST_CSV = ROOT_DIR / "watchlist.csv"
SCANS_CSV = ROOT_DIR / "scans.csv"
PASSES_CSV = ROOT_DIR / "passes.csv"
DB_PATH = ROOT_DIR / "price_cache.sqlite"
DEFAULT_CACHE_FAIL_TTL = 1200  # seconds (20 minutes)


# ------------------------------------------------------------
# Small utilities
# ------------------------------------------------------------
def _open_cache(db_path: Path) -> PriceCache:
    """
    Support multiple PriceCache constructor signatures without changing cache.py.
    Mirrors the compatibility approach used in core.py.
    """
    db_path = Path(db_path)
    for ctor in (
        lambda: PriceCache(db_path),
        lambda: PriceCache(path=db_path),
        lambda: PriceCache(str(db_path)),
        lambda: PriceCache(path=str(db_path)),
        lambda: PriceCache(db_path=str(db_path)),
    ):
        try:
            return ctor()
        except TypeError:
            continue
    raise TypeError(
        "Unsupported PriceCache constructor. Tried: PriceCache(path/str), PriceCache(path=...), PriceCache(db_path=...)."
    )


def safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        df = pd.read_csv(path)
        obj_cols = df.select_dtypes(include=["object"]).columns
        if len(obj_cols) > 0:
            df[obj_cols] = df[obj_cols].fillna("")
        return df
    except Exception as e:
        st.error(f"Failed to read `{path.name}`: {type(e).__name__}: {e}")
        return None


def safe_read_bytes(path: Path) -> Optional[bytes]:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return path.read_bytes()
    except Exception:
        return None


def latest_timestamp_from_scans(df: pd.DataFrame) -> Tuple[Optional[pd.Timestamp], pd.DataFrame]:
    if df.empty:
        return None, df
    if "timestamp" not in df.columns:
        return None, df.tail(50)
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().all():
        return None, df.tail(50)
    latest_ts = ts.max()
    latest_rows = df[ts == latest_ts]
    # Keep it readable
    return latest_ts, latest_rows.head(200)


def ensure_playwright_chromium_installed() -> None:
    """
    Best-effort runtime install. On Streamlit Cloud, you should still prefer
    installing browsers during build/deploy. This is a fallback.
    """
    browsers_dir = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    try:
        if browsers_dir.exists() and any(browsers_dir.iterdir()):
            return
    except Exception:
        # if listing fails, still try install
        pass

    with st.spinner("Installing Playwright Chromium browser (first-time setup)..."):
        try:
            subprocess.run(
                ["playwright", "install", "chromium"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            st.success("Chromium installed successfully.")
        except Exception as e:
            st.warning(
                "Automatic Chromium install did not succeed in this environment. "
                "If scans fail, ensure Playwright browsers are installed in your deploy step.\n\n"
                f"Details: {type(e).__name__}: {e}"
            )


def render_file_links() -> None:
    st.caption("Output files (saved next to this app):")
    st.code(
        "\n".join(
            [
                f"watchlist.csv: {WATCHLIST_CSV}",
                f"scans.csv:     {SCANS_CSV}",
                f"passes.csv:    {PASSES_CSV}",
                f"cache db:      {DB_PATH}",
            ]
        )
    )


# ------------------------------------------------------------
# Streamlit page
# ------------------------------------------------------------
st.set_page_config(page_title="Keyflip Scanner", layout="wide")
st.title("Keyflip — CDKeys/Loaded → Eneba Scanner")
st.caption(
    "Build a watchlist of CDKeys/Loaded items under your max buy price, then scan Eneba to find profitable resale deals."
)

ensure_playwright_chromium_installed()

# ------------------------------------------------------------
# Sidebar settings
# ------------------------------------------------------------
with st.sidebar:
    st.header("Settings")

    max_buy = st.number_input("Max buy price (£)", min_value=1.0, max_value=200.0, value=15.0, step=0.5)
    watchlist_target = st.number_input("Watchlist size target", min_value=1, max_value=100, value=15, step=1)

    verify_candidates = st.number_input("Verify candidates", min_value=0, max_value=5000, value=300, step=25)
    pages_per_source = st.number_input("Pages per source", min_value=1, max_value=25, value=5, step=1)

    verify_limit = st.number_input("Verify limit (0 = use safety cap)", min_value=0, max_value=500, value=0, step=1)
    safety_cap = st.number_input("Verify safety cap", min_value=1, max_value=500, value=20, step=1)

    scan_limit = st.number_input("Scan limit (0 = no limit)", min_value=0, max_value=2000, value=10, step=1)
    avoid_recent_days = st.number_input("Avoid recent days", min_value=0, max_value=30, value=0, step=1)

    allow_eur = st.checkbox("Allow EUR prices (convert to GBP)", value=False)
    eur_to_gbp = st.number_input("EUR→GBP conversion rate", min_value=0.1, max_value=2.0, value=0.86, step=0.01)

    item_budget = st.number_input("Per-item time budget (sec)", min_value=5.0, max_value=180.0, value=55.0, step=5.0)
    run_budget = st.number_input("Overall run time budget (sec, 0 = none)", min_value=0.0, max_value=3600.0, value=0.0, step=10.0)

    st.divider()
    clear_cache = st.button("Clear price cache (DB)", width="stretch")
    clear_recent = st.button("Clear recent flags (avoid_recent_days)", width="stretch")
    delete_outputs = st.button("Delete CSV outputs", width="stretch")

# ------------------------------------------------------------
# Actions row
# ------------------------------------------------------------
col1, col2, col3 = st.columns([1, 1, 1])
build_clicked = col1.button("🔨 Build Watchlist", width="stretch")
scan_clicked = col2.button("🔍 Scan Watchlist", width="stretch")
play_clicked = col3.button("▶️ Play All (Build + Scan)", width="stretch")

# ------------------------------------------------------------
# Maintenance actions
# ------------------------------------------------------------
if clear_cache:
    try:
        cache = _open_cache(DB_PATH)
        cache.clear_all()
        st.success("Cache cleared.")
    except Exception as e:
        st.error(f"Failed to clear cache: {type(e).__name__}: {e}")

if clear_recent:
    # "recent flags" live in the DB (cache.mark_recent / cache.is_recent), not in watchlist.csv
    try:
        cache = _open_cache(DB_PATH)

        # Prefer a method if cache.py provides it
        if hasattr(cache, "clear_recent"):
            cache.clear_recent()  # type: ignore[attr-defined]
            st.success("Recent flags cleared.")
        else:
            # Fallback: safest behavior is to clear the cache entirely.
            cache.clear_all()
            st.success("Recent flags cleared (by clearing cache).")
    except Exception as e:
        st.error(f"Failed to clear recent flags: {type(e).__name__}: {e}")

if delete_outputs:
    deleted = 0
    for p in (WATCHLIST_CSV, SCANS_CSV, PASSES_CSV):
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except Exception:
            pass
    st.success(f"Deleted {deleted} output file(s).")

# ------------------------------------------------------------
# Build / Scan
# ------------------------------------------------------------
def make_config(*, include_scan_fields: bool) -> RunConfig:
    kwargs = dict(
        max_buy=max_buy,
        target=int(watchlist_target),
        verify_candidates=int(verify_candidates),
        pages_per_source=int(pages_per_source),
        verify_limit=int(verify_limit),
        safety_cap=int(safety_cap),
        avoid_recent_days=int(avoid_recent_days),
        allow_eur=allow_eur,
        eur_to_gbp=float(eur_to_gbp),
        item_budget=float(item_budget),
        run_budget=float(run_budget),
    )
    if include_scan_fields:
        kwargs["scan_limit"] = int(scan_limit)
    return RunConfig(**kwargs)


if build_clicked or play_clicked:
    cfg = make_config(include_scan_fields=False)
    with st.spinner("Building watchlist from CDKeys/Loaded..."):
        try:
            added = build_watchlist(cfg, WATCHLIST_CSV)
            if added:
                st.success(f"Watchlist built with {added} item(s).")
            else:
                st.warning("Watchlist built, but no items were added (no matches found).")
        except Exception as e:
            st.error(f"Build failed: {type(e).__name__}: {e}")

if scan_clicked or play_clicked:
    cfg = make_config(include_scan_fields=True)
    with st.spinner("Scanning watchlist on Eneba..."):
        try:
            scan_watchlist(cfg, WATCHLIST_CSV, SCANS_CSV, PASSES_CSV, DB_PATH, DEFAULT_CACHE_FAIL_TTL)
            st.success("Scan complete. Results saved.")
        except Exception as e:
            st.error(f"Scan failed: {type(e).__name__}: {e}")

# ------------------------------------------------------------
# Display outputs
# ------------------------------------------------------------
st.divider()
render_file_links()

watch_df = safe_read_csv(WATCHLIST_CSV)
scans_df = safe_read_csv(SCANS_CSV)
passes_df = safe_read_csv(PASSES_CSV)

tabs = st.tabs(["📋 Watchlist", "✅ Good Deals", "📈 Latest Scan", "⬇️ Downloads"])

with tabs[0]:
    if watch_df is not None:
        st.dataframe(watch_df, width="stretch")
    else:
        st.info("No watchlist available yet. Build one first.")

with tabs[1]:
    if passes_df is not None and not passes_df.empty:
        st.dataframe(passes_df, width="stretch")
    else:
        st.info("No good deals found yet (passes.csv is empty).")

with tabs[2]:
    if scans_df is not None and not scans_df.empty:
        ts, recent = latest_timestamp_from_scans(scans_df)
        if ts is not None:
            st.caption(f"Most recent scan at: {ts}")
        st.dataframe(recent, width="stretch")
    else:
        st.info("No scan results found yet.")

with tabs[3]:
    b = safe_read_bytes(WATCHLIST_CSV)
    st.download_button(
        "Download watchlist.csv",
        data=b if b is not None else b"",
        file_name="watchlist.csv",
        width="stretch",
        disabled=b is None,
    )

    b = safe_read_bytes(SCANS_CSV)
    st.download_button(
        "Download scans.csv",
        data=b if b is not None else b"",
        file_name="scans.csv",
        width="stretch",
        disabled=b is None,
    )

    b = safe_read_bytes(PASSES_CSV)
    st.download_button(
        "Download passes.csv",
        data=b if b is not None else b"",
        file_name="passes.csv",
        width="stretch",
        disabled=b is None,
    )

st.caption(
    "Tip: If Playwright fails on Streamlit Cloud, install browsers during deploy. "
    "Runtime install is best-effort only."
)
