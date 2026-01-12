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
    return latest_ts, latest_rows

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
    max_buy = st.number_input("Max buy price (£)", min_value=1.0, max_value=200.0, value=15.0, step=0.5)
    watchlist_target = st.number_input("Watchlist size target", min_value=1, max_value=50, value=15, step=1)
    verify_candidates = st.number_input("Verify candidates", min_value=20, max_value=2000, value=300, step=10)
    pages_per_source = st.number_input("Pages per source", min_value=1, max_value=10, value=5, step=1)
    verify_limit = st.number_input("Verify limit (0 = use safety cap)", min_value=0, max_value=200, value=0, step=1)
    safety_cap = st.number_input("Verify safety cap", min_value=1, max_value=200, value=20, step=1)
    scan_limit = st.number_input("Scan limit (0 = no limit)", min_value=0, max_value=500, value=10, step=1)
    avoid_recent_days = st.number_input("Avoid recent days", min_value=0, max_value=30, value=0, step=1)
    allow_eur = st.checkbox("Allow EUR prices (convert to GBP)", value=False)
    eur_to_gbp = st.number_input("EUR→GBP conversion rate", min_value=0.1, max_value=2.0, value=0.86, step=0.01)
    item_budget = st.number_input("Per-item time budget (sec)", min_value=5.0, max_value=180.0, value=55.0, step=5.0)
    run_budget = st.number_input("Overall run time budget (sec, 0 = none)", min_value=0.0, max_value=3600.0, value=0.0, step=10.0)
    st.divider()
    clear_cache = st.button("Clear price cache", use_container_width=True)
    clear_recent = st.button("Clear recent flags", use_container_width=True)

# [The rest of app.py continues below unchanged...]
# Your existing scan logic, buttons, tabs, and file displays remain intact.
