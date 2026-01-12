from __future__ import annotations

# MUST be set before importing anything that may import playwright
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

import io
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import streamlit as st

from keyflip.main import main as keyflip_main

# =========================
# Paths / constants
# =========================
ROOT = Path(__file__).parent.resolve()
WATCHLIST = ROOT / "watchlist.csv"
SCANS = ROOT / "scans.csv"
PASSES = ROOT / "passes.csv"
DB = ROOT / "price_cache.sqlite"

DEFAULT_CACHE_FAIL_TTL = 1200

# =========================
# Streamlit setup
# =========================
st.set_page_config(page_title="Keyflip Scanner", layout="wide")

st.title("Keyflip — Fanatical → Eneba Scanner (Playwright Fanatical)")
st.caption(
    "Builds a fresh watchlist (<= max buy) then scans Eneba for edge. "
    "Fanatical is fetched via Playwright; Eneba is fetched via requests."
)

# =========================
# Session state
# =========================
if "running" not in st.session_state:
    st.session_state.running = False
if "last_output" not in st.session_state:
    st.session_state.last_output = ""
if "last_exit_code" not in st.session_state:
    st.session_state.last_exit_code = None
if "last_label" not in st.session_state:
    st.session_state.last_label = ""
if "last_elapsed_s" not in st.session_state:
    st.session_state.last_elapsed_s = None

# =========================
# Helpers
# =========================
def run_keyflip(argv: list[str]) -> tuple[int, str]:
    """
    Run keyflip.main.main(argv) and capture stdout/stderr into a string.
    Returns (exit_code, combined output).
    """
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rv = keyflip_main(argv)
        code = int(rv) if rv is not None else 0
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        code = 1
        buf.write(f"\nERROR: {type(e).__name__}: {e}\n")
    return code, buf.getvalue()

def safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
    """
    Safe CSV loader that avoids EmptyDataError and doesn't blindly coerce all NaNs to "".
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        df = pd.read_csv(path)
        # Only fill NaNs in object columns (keeps numeric cols numeric)
        obj_cols = df.select_dtypes(include=["object"]).columns
        if len(obj_cols) > 0:
            df[obj_cols] = df[obj_cols].fillna("")
        return df
    except Exception as e:
        st.error(f"Failed to read {path.name}: {type(e).__name__}: {e}")
        return None

def safe_read_bytes(path: Path) -> Optional[bytes]:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return path.read_bytes()
    except Exception:
        return None

def file_status_line(p: Path) -> str:
    try:
        if not p.exists():
            return f"- {p.name}: —  ({p.resolve()})"
        return f"- {p.name}: ✅  ({p.resolve()})  • {p.stat().st_size:,} bytes"
    except Exception:
        return f"- {p.name}: ✅  ({p})"

def rows_count(path: Path) -> Optional[int]:
    df = safe_read_csv(path)
    if df is None:
        return None
    return int(len(df))

def latest_timestamp_from_scans(df: pd.DataFrame) -> Tuple[Optional[pd.Timestamp], pd.DataFrame]:
    """
    Returns (latest_ts, filtered_df_for_latest_ts).
    If timestamp column missing or can't parse, returns (None, df.tail(50)).
    """
    if df.empty:
        return None, df
    if "timestamp" not in df.columns:
        return None, df.tail(50)
    ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=False)
    if ts.isna().all():
        return None, df.tail(50)
    latest = ts.max()
    mask = ts == latest
    return latest, df.loc[mask]

@dataclass(frozen=True)
class Settings:
    max_buy: float
    watchlist_target: int
    verify_candidates: int
    pages_per_source: int
    verify_limit: int
    safety_cap: int
    scan_limit: int
    avoid_recent_days: int
    allow_eur: bool
    eur_to_gbp: float
    debug: bool
    item_budget: float
    run_budget: float

def build_args_common(s: Settings) -> list[str]:
    # IMPORTANT: always pass --root so CLI writes CSVs to the same folder as Streamlit
    args = [
        "--root", str(ROOT),
        "--max-buy", str(float(s.max_buy)),
        "--watchlist-target", str(int(s.watchlist_target)),
        "--verify-candidates", str(int(s.verify_candidates)),
        "--pages-per-source", str(int(s.pages_per_source)),
        "--verify-limit", str(int(s.verify_limit)),
        "--verify-safety-cap", str(int(s.safety_cap)),
        "--scan-limit", str(int(s.scan_limit)),
        "--avoid-recent-days", str(int(s.avoid_recent_days)),
        "--item-budget", str(float(s.item_budget)),
        "--run-budget", str(float(s.run_budget)),
        "--cache-fail-ttl", str(int(DEFAULT_CACHE_FAIL_TTL)),
    ]
    if s.allow_eur:
        args += ["--allow-eur", "--eur-to-gbp", str(float(s.eur_to_gbp))]
    if s.debug:
        args += ["--debug"]
    return args

def run_action(label: str, argv: list[str]) -> None:
    st.session_state.running = True
    st.session_state.last_label = label
    st.session_state.last_output = ""
    st.session_state.last_exit_code = None
    st.session_state.last_elapsed_s = None
    t0 = time.perf_counter()
    try:
        with st.spinner(f"Running {label}…"):
            code, out = run_keyflip(argv)
        elapsed = time.perf_counter() - t0
        st.session_state.last_elapsed_s = elapsed
        st.session_state.last_exit_code = code
        st.session_state.last_output = out
    finally:
        st.session_state.running = False

def show_run_summary_banner() -> None:
    wl_n = rows_count(WATCHLIST)
    sc_n = rows_count(SCANS)
    pa_n = rows_count(PASSES)
    cols = st.columns(4)
    cols[0].metric("Watchlist items", "—" if wl_n is None else f"{wl_n:,}")
    cols[1].metric("Scanned items", "—" if sc_n is None else f"{sc_n:,}")
    cols[2].metric("Good deals", "—" if pa_n is None else f"{pa_n:,}")
    latest_str = "—"
    df_sc = safe_read_csv(SCANS)
    if df_sc is not None and not df_sc.empty and "timestamp" in df_sc.columns:
        latest_ts, _ = latest_timestamp_from_scans(df_sc)
        if latest_ts is not None:
            latest_str = str(latest_ts)
    cols[3].metric("Last scan time", latest_str)

def show_last_run() -> None:
    if st.session_state.last_exit_code is None:
        return
    st.subheader("Last run")
    meta_cols = st.columns(3)
    meta_cols[0].write(f"**Action:** {st.session_state.last_label or '—'}")
    elapsed = st.session_state.last_elapsed_s
    meta_cols[1].write(f"**Elapsed:** {('—' if elapsed is None else f'{elapsed:.2f} s')}")
    code = st.session_state.last_exit_code
    meta_cols[2].write(f"**Exit code:** {code}")
    if code != 0:
        st.error("Run failed. Check output below.")
    else:
        st.success("Done")
    show_run_summary_banner()
    with st.expander("Run output (logs)", expanded=(st.session_state.last_exit_code != 0)):
        st.code(st.session_state.last_output or "(no output)")

def show_downloads_row() -> None:
    c1, c2, c3, c4 = st.columns(4)
    wl = safe_read_bytes(WATCHLIST)
    sc = safe_read_bytes(SCANS)
    pa = safe_read_bytes(PASSES)
    db = safe_read_bytes(DB)
    c1.download_button(
        "⬇️ Download watchlist.csv",
        data=wl if wl is not None else b"",
        file_name="watchlist.csv",
        mime="text/csv",
        disabled=(wl is None),
        use_container_width=True,
    )
    c2.download_button(
        "⬇️ Download scans.csv",
        data=sc if sc is not None else b"",
        file_name="scans.csv",
        mime="text/csv",
        disabled=(sc is None),
        use_container_width=True,
    )
    c3.download_button(
        "⬇️ Download passes.csv",
        data=pa if pa is not None else b"",
        file_name="passes.csv",
        mime="text/csv",
        disabled=(pa is None),
        use_container_width=True,
    )
    c4.download_button(
        "⬇️ Download price_cache.sqlite",
        data=db if db is not None else b"",
        file_name="price_cache.sqlite",
        mime="application/octet-stream",
        disabled=(db is None),
        use_container_width=True,
    )

# =========================
# Sidebar settings
# =========================
with st.sidebar:
    st.header("Settings")
    max_buy = st.number_input("Max buy (£)", min_value=1.0, max_value=200.0, value=10.0, step=0.5, help="Maximum price (£) to pay for games")
    watchlist_target = st.number_input("Watchlist target", min_value=1, max_value=50, value=10, step=1, help="Target number of games to build in watchlist")
    verify_candidates = st.number_input("Verify candidates", min_value=20, max_value=2000, value=220, step=20, help="Number of cheapest games to consider for verification")
    pages_per_source = st.number_input("Pages per source", min_value=1, max_value=10, value=2, step=1, help="Number of pages to fetch from each source")
    verify_limit = st.number_input("Verify limit (0 = use safety cap)", min_value=0, max_value=200, value=10, step=1, help="Max games to verify (0 uses safety cap)")
    safety_cap = st.number_input("Verify safety cap", min_value=1, max_value=200, value=14, step=1, help="Verify safety cap (used if limit=0)")
    scan_limit = st.number_input("Scan limit (0 = unlimited)", min_value=0, max_value=200, value=10, step=1, help="Max games to scan for deals (0 = no limit)")
    avoid_recent_days = st.number_input("Avoid recent days", min_value=0, max_value=30, value=2, step=1, help="Skip games released in the last N days")
    allow_eur = st.checkbox("Allow EUR (convert to GBP)", value=False, help="Include Euro-priced deals (convert to GBP)")
    eur_to_gbp = st.number_input("EUR→GBP rate", min_value=0.1, max_value=2.0, value=0.86, step=0.01, help="Conversion rate from EUR to GBP")
    debug = st.checkbox("Debug logging", value=False, help="Enable verbose logging output")
    item_budget = st.number_input("Item budget (seconds)", min_value=10.0, max_value=180.0, value=55.0, step=5.0, help="Time budget per item (seconds)")
    run_budget = st.number_input("Run budget (0 disables)", min_value=0.0, max_value=3600.0, value=0.0, step=10.0, help="Time budget for entire run (0 = none)")
    st.divider()
    settings = Settings(
        max_buy=float(max_buy),
        watchlist_target=int(watchlist_target),
        verify_candidates=int(verify_candidates),
        pages_per_source=int(pages_per_source),
        verify_limit=int(verify_limit),
        safety_cap=int(safety_cap),
        scan_limit=int(scan_limit),
        avoid_recent_days=int(avoid_recent_days),
        allow_eur=bool(allow_eur),
        eur_to_gbp=float(eur_to_gbp),
        debug=bool(debug),
        item_budget=float(item_budget),
        run_budget=float(run_budget),
    )
    colA, colB = st.columns(2)
    clear_cache = colA.button("Clear cache", disabled=st.session_state.running, use_container_width=True, help="Clear cached price data")
    clear_recent = colB.button("Clear recent", disabled=st.session_state.running, use_container_width=True, help="Clear recently seen items record")

# Handle maintenance actions
if clear_cache:
    argv = build_args_common(settings) + ["--clear-cache"]
    run_action("CLEAR-CACHE", argv)
    show_last_run()
    st.stop()

if clear_recent:
    argv = build_args_common(settings) + ["--clear-recent"]
    run_action("CLEAR-RECENT", argv)
    show_last_run()
    st.stop()

# =========================
# Main action buttons
# =========================
c1, c2, c3 = st.columns(3)
do_play = c1.button("▶ PLAY", use_container_width=True, disabled=st.session_state.running)
do_build = c2.button("🔨 Build", use_container_width=True, disabled=st.session_state.running)
do_scan = c3.button("🔍 Scan", use_container_width=True, disabled=st.session_state.running)

if do_play or do_build or do_scan:
    argv = build_args_common(settings)
    if do_play:
        argv += ["--play"]
        run_action("PLAY", argv)
    elif do_build:
        argv += ["--build"]
        run_action("BUILD", argv)
    elif do_scan:
        argv += ["--scan"]
        run_action("SCAN", argv)

show_last_run()

st.divider()
st.subheader("Results")
show_downloads_row()

tabs = st.tabs(["Watchlist", "Scans", "Good Deals", "Files"])

with tabs[0]:
    df = safe_read_csv(WATCHLIST)
    if df is None or df.empty:
        st.info("No watchlist yet (use Build or Play).")
    else:
        st.dataframe(df, use_container_width=True)

with tabs[1]:
    df = safe_read_csv(SCANS)
    if df is None or df.empty:
        st.info("No scan results yet.")
    else:
        latest_ts, latest_df = latest_timestamp_from_scans(df)
        if latest_ts is not None:
            st.caption(f"Latest timestamp: {latest_ts}")
            st.dataframe(latest_df, use_container_width=True)
        else:
            st.caption("Showing last 50 rows (no usable timestamp column).")
            st.dataframe(df.tail(50), use_container_width=True)

with tabs[2]:
    df = safe_read_csv(PASSES)
    if df is None or df.empty:
        st.info("No good deals found yet.")
    else:
        st.dataframe(df, use_container_width=True)

with tabs[3]:
    st.write("Output files:")
    for p in [WATCHLIST, PASSES, SCANS, DB]:
        st.write(file_status_line(p))
