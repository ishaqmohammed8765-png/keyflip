from __future__ import annotations

# Set up Playwright browser installation directory before any Playwright imports
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw-browsers")

import subprocess
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import streamlit as st

from keyflip.core import RunConfig, build_watchlist, scan_watchlist
from keyflip.cache import PriceCache

# Define paths for output files relative to this app's directory
ROOT_DIR = Path(__file__).parent.resolve()
WATCHLIST_CSV = ROOT_DIR / "watchlist.csv"
SCANS_CSV = ROOT_DIR / "scans.csv"
PASSES_CSV = ROOT_DIR / "passes.csv"
DB_PATH = ROOT_DIR / "price_cache.sqlite"
DEFAULT_CACHE_FAIL_TTL = 1200  # seconds (20 minutes)

# Configure Streamlit page
st.set_page_config(page_title="Keyflip Scanner", layout="wide")
st.title("Keyflip — Fanatical → Eneba Scanner")
st.caption("Use this tool to build a watchlist of games from Fanatical (up to your max price), then scan Eneba for profitable resale deals.")

# Utility functions for safe file handling and data loading
def safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
    """Read a CSV into a DataFrame, returning None if file missing/empty or on error."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        df = pd.read_csv(path)
        # Fill NA/NaN only for object (string) columns to avoid losing numeric data
        obj_cols = df.select_dtypes(include=["object"]).columns
        if len(obj_cols) > 0:
            df[obj_cols] = df[obj_cols].fillna("")
        return df
    except Exception as e:
        st.error(f"Failed to read `{path.name}`: {type(e).__name__}: {e}")
        return None

def safe_read_bytes(path: Path) -> Optional[bytes]:
    """Read a file's bytes safely, returning None if file missing or empty."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return path.read_bytes()
    except Exception:
        return None

def latest_timestamp_from_scans(df: pd.DataFrame) -> Tuple[Optional[pd.Timestamp], pd.DataFrame]:
    """
    Given a DataFrame of scans, return the latest timestamp (if any) and 
    the subset of rows corresponding to the latest scan batch.
    If no valid timestamp column, return the last 50 rows.
    """
    if df.empty:
        return None, df
    if "timestamp" not in df.columns:
        # If no timestamp column, show last 50 rows
        return None, df.tail(50)
    # Convert timestamp column to datetime
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().all():
        # Unparseable timestamps, show last 50 rows
        return None, df.tail(50)
    latest_ts = ts.max()
    latest_rows = df[ts == latest_ts]
    return latest_ts, latest_rows

# Attempt to ensure Playwright Chromium is installed (first-run setup)
browsers_dir = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
if not browsers_dir.exists() or not any(browsers_dir.iterdir()):
    with st.spinner("Installing Chromium browser for Playwright (first-time setup)..."):
        try:
            subprocess.run(["playwright", "install", "chromium"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            st.success("Chromium has been installed successfully.")
        except Exception as e:
            st.error(f"Automatic Chromium install failed: {e}")

# Sidebar: User-configurable settings
with st.sidebar:
    st.header("Settings")
    max_buy = st.number_input("Max buy price (£)", min_value=1.0, max_value=200.0, value=10.0, step=0.5,
                              help="Maximum price you're willing to pay for a game on Fanatical.")
    watchlist_target = st.number_input("Watchlist size target", min_value=1, max_value=50, value=10, step=1,
                                       help="Desired number of games to include in the watchlist.")
    verify_candidates = st.number_input("Verify candidates", min_value=20, max_value=2000, value=220, step=10,
                                        help="Number of cheapest games to fetch as candidates for verification.")
    pages_per_source = st.number_input("Pages per source", min_value=1, max_value=10, value=2, step=1,
                                       help="Number of pages to scan on each Fanatical source (for watchlist building).")
    verify_limit = st.number_input("Verify limit (0 = use safety cap)", min_value=0, max_value=200, value=10, step=1,
                                   help="Max games to fully verify when building watchlist (0 uses safety cap).")
    safety_cap = st.number_input("Verify safety cap", min_value=1, max_value=200, value=14, step=1,
                                 help="Absolute cap on games to verify if verify limit is 0 (acts as a safety net).")
    scan_limit = st.number_input("Scan limit (0 = no limit)", min_value=0, max_value=500, value=10, step=1,
                                 help="Maximum number of watchlist items to scan for deals (0 = no limit).")
    avoid_recent_days = st.number_input("Avoid recent days", min_value=0, max_value=30, value=2, step=1,
                                        help="Skip games released in the last N days (both for building and scanning).")
    allow_eur = st.checkbox("Allow EUR prices (convert to GBP)", value=False,
                             help="Include Euro-denominated deals (will be converted to GBP).")
    eur_to_gbp = st.number_input("EUR→GBP conversion rate", min_value=0.1, max_value=2.0, value=0.86, step=0.01,
                                 help="Conversion rate from EUR to GBP, if EUR prices are allowed.")
    item_budget = st.number_input("Per-item time budget (sec)", min_value=5.0, max_value=180.0, value=55.0, step=5.0,
                                  help="Time budget for processing each item (game) during build/scan.")
    run_budget = st.number_input("Overall run time budget (sec, 0 = none)", min_value=0.0, max_value=3600.0, value=0.0, step=10.0,
                                 help="Time budget for the entire operation (0 means no limit).")
    st.divider()
    # Maintenance actions
    clear_cache = st.button("Clear price cache", use_container_width=True,
                             help="Delete all cached price entries (forces fresh price fetches).")
    clear_recent = st.button("Clear recent flags", use_container_width=True,
                              help="Forget 'recently seen' items (so they can be considered again).")

# Handle maintenance actions (cache clearing)
if clear_cache:
    # Clear all cached price data
    try:
        with PriceCache(DB_PATH) as cache:
            cache.clear_cache()
        st.success("Price cache cleared.")
    except Exception as e:
        st.error(f"Failed to clear cache: {e}")
    # Stop further processing to avoid running an action simultaneously
    st.stop()

if clear_recent:
    # Clear 'recently seen' records
    try:
        with PriceCache(DB_PATH) as cache:
            cache.clear_recent()
        st.success("Recently-seen items cleared.")
    except Exception as e:
        st.error(f"Failed to clear recent items: {e}")
    st.stop()

# Prepare configuration for build/scan actions using current settings
cfg = RunConfig(
    root=ROOT_DIR,
    max_buy_gbp=float(max_buy),
    watchlist_target=int(watchlist_target),
    verify_candidates=int(verify_candidates),
    pages_per_source=int(pages_per_source),
    verify_limit=int(verify_limit),
    verify_safety_cap=int(safety_cap),
    scan_limit=int(scan_limit),
    avoid_recent_days=int(avoid_recent_days),
    allow_eur=bool(allow_eur),
    eur_to_gbp=float(eur_to_gbp),
    item_budget_s=float(item_budget),
    run_budget_s=float(run_budget),
    cache_fail_ttl_s=int(DEFAULT_CACHE_FAIL_TTL),
)

# Main action buttons
col1, col2 = st.columns(2)
# Disable Scan button if no watchlist data is available yet
watchlist_ready = safe_read_csv(WATCHLIST_CSV)
scan_disabled = (watchlist_ready is None or watchlist_ready.empty)
build_clicked = col1.button("🔨 Build Watchlist", help="Step 1: Build a watchlist of games from Fanatical below the max buy price.", disabled=False, type="primary")
scan_clicked = col2.button("🔍 Scan Watchlist", help="Step 2: Scan Eneba for deals on games in the watchlist.", disabled=scan_disabled, type="primary")

# Execute actions based on button clicks
if build_clicked:
    st.write("**Building watchlist...**")
    # Use a spinner to indicate work in progress
    with st.spinner("Building watchlist, please wait..."):
        try:
            with PriceCache(DB_PATH) as cache:
                # Perform the watchlist build using core logic
                df_watch = build_watchlist(cfg, cache, WATCHLIST_CSV)
            # Provide feedback to user
            num_items = len(df_watch) if df_watch is not None else 0
            if num_items > 0:
                st.success(f"Watchlist built successfully with **{num_items}** items.")
            else:
                st.warning("Watchlist built, but no items were added (no games met the criteria).")
        except Exception as e:
            st.error("Watchlist build failed!")
            # Show technical details in an expandable section for troubleshooting
            with st.expander("Error details"):
                st.write(f"*{type(e).__name__}:* {e}")

if scan_clicked:
    # Prevent scanning if watchlist is not ready
    watch_df = safe_read_csv(WATCHLIST_CSV)
    if watch_df is None or watch_df.empty:
        st.warning("Watchlist is empty or not available. Please build the watchlist first.")
    else:
        st.write("**Scanning watchlist for deals...**")
        with st.spinner("Scanning watchlist on Eneba, please wait..."):
            try:
                with PriceCache(DB_PATH) as cache:
                    # Perform the scan using core logic
                    df_batch = scan_watchlist(cfg, cache, WATCHLIST_CSV, SCANS_CSV, PASSES_CSV)
                # Summarize results for the user
                if df_batch is not None and not df_batch.empty:
                    deals_found = int((df_batch["passes"] == True).sum())
                else:
                    deals_found = 0
                if deals_found > 0:
                    st.success(f"Scan complete! Found **{deals_found}** profitable deal(s) in this run.")
                else:
                    st.info("Scan complete. No profitable deals found this time.")
            except Exception as e:
                st.error("Scan failed!")
                with st.expander("Error details"):
                    st.write(f"*{type(e).__name__}:* {e}")

# Display summary metrics after actions (or initial state)
wl_count = None if (watchlist_ready is None) else len(watchlist_ready)
scans_df = safe_read_csv(SCANS_CSV)
passes_df = safe_read_csv(PASSES_CSV)
sc_count = None if (scans_df is None) else len(scans_df)
pa_count = None if (passes_df is None) else len(passes_df)
# Determine last scan timestamp if available
last_scan_time = "—"
if scans_df is not None and not scans_df.empty:
    ts, _latest_rows = latest_timestamp_from_scans(scans_df)
    if ts:
        last_scan_time = str(ts)

# Only show metrics if any data exists (to avoid confusion on first run)
if any([wl_count, sc_count, pa_count]):
    metrics = st.columns(4)
    metrics[0].metric("Watchlist items", "—" if wl_count is None else f"{wl_count:,}")
    metrics[1].metric("Scanned items", "—" if sc_count is None else f"{sc_count:,}")
    metrics[2].metric("Good deals", "—" if pa_count is None else f"{pa_count:,}")
    metrics[3].metric("Last scan time", last_scan_time)

# Offer download buttons for output files
dl_wl = safe_read_bytes(WATCHLIST_CSV)
dl_sc = safe_read_bytes(SCANS_CSV)
dl_pa = safe_read_bytes(PASSES_CSV)
dl_db = safe_read_bytes(DB_PATH)
d1, d2, d3, d4 = st.columns(4)
d1.download_button("⬇️ Download watchlist.csv", data=dl_wl or b"", file_name="watchlist.csv",
                   mime="text/csv", disabled=(dl_wl is None))
d2.download_button("⬇️ Download scans.csv", data=dl_sc or b"", file_name="scans.csv",
                   mime="text/csv", disabled=(dl_sc is None))
d3.download_button("⬇️ Download passes.csv", data=dl_pa or b"", file_name="passes.csv",
                   mime="text/csv", disabled=(dl_pa is None))
d4.download_button("⬇️ Download price_cache.sqlite", data=dl_db or b"", file_name="price_cache.sqlite",
                   mime="application/octet-stream", disabled=(dl_db is None))

# Tabs to display data tables
tab_watch, tab_scans, tab_passes = st.tabs(["Watchlist", "Scans", "Passes"])

with tab_watch:
    df_watch = safe_read_csv(WATCHLIST_CSV)
    if df_watch is None or df_watch.empty:
        st.info("No watchlist yet. Click **Build Watchlist** to generate one.")
    else:
        st.dataframe(df_watch, use_container_width=True)

with tab_scans:
    df_scans = safe_read_csv(SCANS_CSV)
    if df_scans is None or df_scans.empty:
        st.info("No scan results yet. Run **Scan Watchlist** to see results here.")
    else:
        latest_ts, latest_scan_df = latest_timestamp_from_scans(df_scans)
        if latest_ts:
            st.caption(f"*Latest scan timestamp:* {latest_ts}")
            st.dataframe(latest_scan_df, use_container_width=True)
        else:
            st.caption("*Showing last 50 scan entries (no timestamp data).*")
            st.dataframe(df_scans.tail(50), use_container_width=True)

with tab_passes:
    df_passes = safe_read_csv(PASSES_CSV)
    if df_passes is None or df_passes.empty:
        st.info("No good deals (passes) found yet.")
    else:
        st.dataframe(df_passes, use_container_width=True)
