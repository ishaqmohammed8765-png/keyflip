from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

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


# =========================
# Streamlit setup
# =========================
st.set_page_config(page_title="Keyflip Scanner", layout="wide")

st.title("Keyflip — Fanatical → Eneba Scanner (No Playwright)")
st.caption("Builds a fresh watchlist (<= max buy) then scans for edge. HTTP-only (requests + bs4).")

# =========================
# Session state
# =========================
if "running" not in st.session_state:
    st.session_state.running = False
if "last_output" not in st.session_state:
    st.session_state.last_output = ""
if "last_exit_code" not in st.session_state:
    st.session_state.last_exit_code = None


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
        buf.write(f"\nERROR: {type(e).__name__}: {e}\n")
    return code, buf.getvalue()


def safe_read_csv(path: Path) -> pd.DataFrame | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return pd.read_csv(path).fillna("")
    except Exception as e:
        st.error(f"Failed to read {path.name}: {type(e).__name__}: {e}")
        return None


def file_status_line(p: Path) -> str:
    try:
        if not p.exists():
            return f"- {p.name}: —  ({p.resolve()})"
        return f"- {p.name}: ✅  ({p.resolve()})  • {p.stat().st_size:,} bytes"
    except Exception:
        return f"- {p.name}: ✅  ({p})"


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
        "--root",
        str(ROOT),
        "--max-buy",
        str(float(s.max_buy)),
        "--watchlist-target",
        str(int(s.watchlist_target)),
        "--verify-candidates",
        str(int(s.verify_candidates)),
        "--pages-per-source",
        str(int(s.pages_per_source)),
        "--verify-limit",
        str(int(s.verify_limit)),
        "--verify-safety-cap",
        str(int(s.safety_cap)),
        "--scan-limit",
        str(int(s.scan_limit)),
        "--avoid-recent-days",
        str(int(s.avoid_recent_days)),
        "--item-budget",
        str(float(s.item_budget)),
        "--run-budget",
        str(float(s.run_budget)),
        "--cache-fail-ttl",
        "1200",
    ]
    if s.allow_eur:
        args += ["--allow-eur", "--eur-to-gbp", str(float(s.eur_to_gbp))]
    if s.debug:
        args += ["--debug"]
    return args


def run_action(label: str, argv: list[str]) -> None:
    st.session_state.running = True
    try:
        with st.spinner(f"Running {label}…"):
            code, out = run_keyflip(argv)
        st.session_state.last_exit_code = code
        st.session_state.last_output = out
    finally:
        st.session_state.running = False


def show_last_run() -> None:
    if st.session_state.last_exit_code is None:
        return

    st.subheader("Run output")
    st.code(st.session_state.last_output or "(no output)")

    code = st.session_state.last_exit_code
    if code != 0:
        st.error(f"Exited with code {code}")
    else:
        st.success("Done")


# =========================
# Sidebar settings
# =========================
with st.sidebar:
    st.header("Settings")

    max_buy = st.number_input("Max buy (£)", min_value=1.0, max_value=200.0, value=10.0, step=0.5)
    watchlist_target = st.number_input("Watchlist target", min_value=1, max_value=50, value=10, step=1)
    verify_candidates = st.number_input("Verify candidates", min_value=20, max_value=2000, value=220, step=20)
    pages_per_source = st.number_input("Pages per source", min_value=1, max_value=10, value=2, step=1)

    verify_limit = st.number_input("Verify limit (0 = unlimited)", min_value=0, max_value=200, value=10, step=1)
    safety_cap = st.number_input("Verify safety cap", min_value=1, max_value=200, value=14, step=1)
    scan_limit = st.number_input("Scan limit (0 = unlimited)", min_value=0, max_value=200, value=10, step=1)

    avoid_recent_days = st.number_input("Avoid recent days", min_value=0, max_value=30, value=2, step=1)

    allow_eur = st.checkbox("Allow EUR (convert to GBP)", value=False)
    eur_to_gbp = st.number_input("EUR→GBP rate", min_value=0.1, max_value=2.0, value=0.86, step=0.01)

    debug = st.checkbox("Debug logging", value=False)

    item_budget = st.number_input("Item budget (seconds)", min_value=10.0, max_value=180.0, value=55.0, step=5.0)
    run_budget = st.number_input("Run budget (0 disables)", min_value=0.0, max_value=3600.0, value=0.0, step=10.0)

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
    clear_cache = colA.button("Clear cache", disabled=st.session_state.running, use_container_width=True)
    clear_recent = colB.button("Clear recent", disabled=st.session_state.running, use_container_width=True)

# Handle maintenance
if clear_cache:
    argv = build_args_common(settings) + ["--clear-cache"]
    run_action("clear-cache", argv)
    show_last_run()
    st.stop()

if clear_recent:
    argv = build_args_common(settings) + ["--clear-recent"]
    run_action("clear-recent", argv)
    show_last_run()
    st.stop()


# =========================
# Main action buttons (with diagnostics)
# =========================
c1, c2, c3, c4, c5 = st.columns(5)
do_play = c1.button("▶ PLAY", use_container_width=True, disabled=st.session_state.running)
do_build = c2.button("🔨 Build", use_container_width=True, disabled=st.session_state.running)
do_scan = c3.button("🔍 Scan", use_container_width=True, disabled=st.session_state.running)
do_diag_h = c4.button("🧪 Harvest diag", use_container_width=True, disabled=st.session_state.running)
do_diag_p = c5.button("🧪 Price diag", use_container_width=True, disabled=st.session_state.running)

if do_play or do_build or do_scan or do_diag_h or do_diag_p:
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
    elif do_diag_h:
        argv += ["--diag-harvest"]
        run_action("DIAG-HARVEST", argv)
    elif do_diag_p:
        argv += ["--diag-price", "5"]
        run_action("DIAG-PRICE", argv)

show_last_run()

st.divider()
st.subheader("Results")

tabs = st.tabs(["passes.csv", "latest scan", "watchlist.csv", "files"])

with tabs[0]:
    df = safe_read_csv(PASSES)
    if df is None:
        st.info("No passes.csv yet (or it’s empty).")
    else:
        st.dataframe(df, use_container_width=True)

with tabs[1]:
    df = safe_read_csv(SCANS)
    if df is None:
        st.info("No scans.csv yet (or it’s empty).")
    else:
        if "timestamp" in df.columns and not df.empty:
            ts = df["timestamp"].astype(str)
            last_ts = ts.max()
            st.caption(f"Latest timestamp: {last_ts}")
            st.dataframe(df[ts == last_ts], use_container_width=True)
        else:
            st.dataframe(df.tail(50), use_container_width=True)

with tabs[2]:
    df = safe_read_csv(WATCHLIST)
    if df is None:
        st.info("No watchlist.csv yet (or it’s empty). Run Build or Play.")
    else:
        st.dataframe(df, use_container_width=True)

with tabs[3]:
    st.write("Working directory files:")
    for p in [WATCHLIST, PASSES, SCANS, DB]:
        st.write(file_status_line(p))
