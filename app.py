from __future__ import annotations

# MUST be set before importing anything that may import playwright
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

import io
import time
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import streamlit as st

from keyflip.main import main as keyflip_main

# =========================
# Streamlit setup
# =========================
st.set_page_config(page_title="Keyflip Scanner", layout="wide")

st.title("Keyflip — Fanatical → Eneba Scanner (Playwright Fanatical)")
st.caption(
    "Builds a fresh watchlist (<= max buy) then scans Eneba for edge. "
    "Fanatical is fetched via Playwright; Eneba is fetched via requests."
)

# Install Playwright Chromium browser if needed at startup
if not st.session_state.get('playwright_installed', False):
    with st.spinner('Installing Chromium for Playwright...'):
        try:
            subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'], check=True)
        except Exception as e:
            st.error('Chromium installation failed. Please check your internet connection and refresh the app.')
            st.stop()
    st.session_state.playwright_installed = True

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
    Returns (exit_code, combined_output).
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
        err = str(e)
        if 'browsertype.launch' in err.lower() or 'executable doesn' in err.lower():
            buf.write("\nERROR: Could not launch the browser. Please try restarting the app or contact support.\n")
        else:
            buf.write(f"\nERROR: {type(e).__name__}: {e}\n")
    return code, buf.getvalue()

def safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
    """Safe CSV loader that avoids EmptyDataError and preserves numeric types."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        df = pd.read_csv(path)
        # Fill NaNs in object columns only
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
    return None if df is None else int(len(df))

def latest_timestamp_from_scans(df: pd.DataFrame) -> Tuple[Optional[pd.Timestamp], pd.DataFrame]:
    """
    Returns (latest_ts, filtered_df_for_latest_ts).
    If timestamp column missing or unparseable, returns (None, df.tail(50)).
    """
    if df.empty:
        return None, df
    if "timestamp" not in df.columns:
        return None, df.tail(50)
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().all():
        return None, df.tail(50)
    latest = ts.max()
    return latest, df[df["timestamp"] == latest]

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
    # Always pass --root so CSVs output to the Streamlit app directory
    args = [
        "--root", str(Path(__file__).parent.resolve()),
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
        "--cache-fail-ttl", str(1200),
    ]
    if s.allow_eur:
        args += ["--allow-eur", "--eur-to-gbp", str(float(s.eur_to_gbp))]
    if s.debug:
        args += ["--debug"]
    return args

# =========================
# Actions and UI Handlers
# =========================
def run_action(label: str, argv: list[str]) -> None:
    """Execute a Keyflip action (build, scan, etc.) and update session state with results."""
    st.session_state.running = True
    st.session_state.last_label = label
    st.session_state.last_output = ""
    st.session_state.last_exit_code = None
    st.session_state.last_elapsed_s = None
    t0 = time.perf_counter()
    try:
        with st.spinner(f"Running {label}…"):
            code, out = run_keyflip(argv)
        st.session_state.last_elapsed_s = time.perf_counter() - t0
        st.session_state.last_exit_code = code
        st.session_state.last_output = out
    finally:
        st.session_state.running = False

def show_run_summary_banner() -> None:
    """Display a summary of the latest outputs (rows counts and latest timestamp)."""
    wl_n = rows_count(Path("watchlist.csv"))
    sc_n = rows_count(Path("scans.csv"))
    pa_n = rows_count(Path("passes.csv"))
    cols = st.columns(4)
    cols[0].metric("watchlist.csv rows", "—" if wl_n is None else f"{wl_n:,}")
    cols[1].metric("scans.csv rows", "—" if sc_n is None else f"{sc_n:,}")
    cols[2].metric("passes.csv rows", "—" if pa_n is None else f"{pa_n:,}")
    latest_str = "—"
    df_sc = safe_read_csv(Path("scans.csv"))
    if df_sc is not None and not df_sc.empty and "timestamp" in df_sc.columns:
        latest_ts, _ = latest_timestamp_from_scans(df_sc)
        if latest_ts is not None:
            latest_str = str(latest_ts)
    cols[3].metric("latest scan timestamp", latest_str)

def show_last_run() -> None:
    """Show details of the most recent action run, including any errors or warnings."""
    if st.session_state.last_exit_code is None:
        return
    st.subheader("Last run")
    cols = st.columns(3)
    cols[0].write(f"**Action:** {st.session_state.last_label or '—'}")
    elapsed = st.session_state.last_elapsed_s
    cols[1].write(f"**Elapsed:** {('—' if elapsed is None else f'{elapsed:.2f} s')}")
    code = st.session_state.last_exit_code
    cols[2].write(f"**Exit code:** {code}")
    if code != 0:
        st.error("Run failed. Check output below.")
    else:
        st.success("Done")
        if st.session_state.last_label in ('BUILD', 'PLAY'):
            wl_n = rows_count(Path("watchlist.csv")) or 0
            if wl_n == 0:
                st.warning("Watchlist is empty – no items were found. Try adjusting settings (e.g. increase Max buy) and run **Build** again.")
    show_run_summary_banner()
    # Expand and show the captured output (stdout/stderr)
    with st.expander("Run output (stdout/stderr)", expanded=(code != 0)):
        st.code(st.session_state.last_output or "(no output)")

# ... (The rest of the app code for settings, buttons, and tabs remains unchanged)
