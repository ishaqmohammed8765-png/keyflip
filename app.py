from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pandas as pd
import streamlit as st

from keyflip.main import main as keyflip_main

ROOT = Path(__file__).parent
WATCHLIST = ROOT / "watchlist.csv"
SCANS = ROOT / "scans.csv"
PASSES = ROOT / "passes.csv"
DB = ROOT / "price_cache.sqlite"

st.set_page_config(page_title="Keyflip Scanner", layout="wide")

st.title("Keyflip — Fanatical → Eneba Scanner (No Playwright)")
st.caption("Builds a fresh watchlist (<= max buy) then scans for edge. HTTP-only (requests + bs4).")


def run_keyflip(argv: list[str]) -> tuple[int, str]:
    """
    Run keyflip.main.main(argv) and capture stdout/stderr into a string.
    Returns (exit_code, combined_output).
    """
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            code = int(keyflip_main(argv))
    except SystemExit as e:
        # argparse may raise SystemExit
        code = int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        code = 1
        buf.write(f"\nERROR: {type(e).__name__}: {e}\n")
    return code, buf.getvalue()


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
    colA, colB = st.columns(2)
    clear_cache = colA.button("Clear cache")
    clear_recent = colB.button("Clear recent")


def build_args_common() -> list[str]:
    args = [
        "--max-buy", str(float(max_buy)),
        "--watchlist-target", str(int(watchlist_target)),
        "--verify-candidates", str(int(verify_candidates)),
        "--pages-per-source", str(int(pages_per_source)),
        "--verify-limit", str(int(verify_limit)),
        "--verify-safety-cap", str(int(safety_cap)),
        "--scan-limit", str(int(scan_limit)),
        "--avoid-recent-days", str(int(avoid_recent_days)),
        "--item-budget", str(float(item_budget)),
        "--run-budget", str(float(run_budget)),
        "--cache-fail-ttl", "1200",
    ]
    if allow_eur:
        args += ["--allow-eur", "--eur-to-gbp", str(float(eur_to_gbp))]
    if debug:
        args += ["--debug"]
    return args


if clear_cache:
    code, out = run_keyflip(build_args_common() + ["--clear-cache"])
    st.subheader("Output")
    st.code(out)
    st.success("Cache cleared" if code == 0 else f"Clear cache failed ({code})")
    st.stop()

if clear_recent:
    code, out = run_keyflip(build_args_common() + ["--clear-recent"])
    st.subheader("Output")
    st.code(out)
    st.success("Recent cleared" if code == 0 else f"Clear recent failed ({code})")
    st.stop()


# Actions
c1, c2, c3 = st.columns(3)
do_play = c1.button("▶ PLAY (build + scan)", use_container_width=True)
do_build = c2.button("🔨 Build watchlist", use_container_width=True)
do_scan = c3.button("🔍 Scan watchlist", use_container_width=True)

if do_play or do_build or do_scan:
    args = build_args_common()
    if do_play:
        args += ["--play"]
    elif do_build:
        args += ["--build"]
    else:
        args += ["--scan"]

    with st.spinner("Running…"):
        code, out = run_keyflip(args)

    st.subheader("Run output")
    st.code(out)

    if code != 0:
        st.error(f"Exited with code {code}")
    else:
        st.success("Done")


st.divider()
st.subheader("Results")

tabs = st.tabs(["passes.csv", "latest scan", "watchlist.csv", "files"])

with tabs[0]:
    if PASSES.exists():
        df = pd.read_csv(PASSES).fillna("")
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No passes.csv yet.")

with tabs[1]:
    if SCANS.exists():
        df = pd.read_csv(SCANS).fillna("")
        if "timestamp" in df.columns and not df.empty:
            last_ts = df["timestamp"].iloc[-1]
            st.caption(f"Latest timestamp: {last_ts}")
            df_latest = df[df["timestamp"] == last_ts]
            st.dataframe(df_latest, use_container_width=True)
        else:
            st.dataframe(df.tail(50), use_container_width=True)
    else:
        st.info("No scans.csv yet.")

with tabs[2]:
    if WATCHLIST.exists():
        df = pd.read_csv(WATCHLIST).fillna("")
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No watchlist.csv yet. Run Build or Play.")

with tabs[3]:
    st.write("Working directory files:")
    for p in [WATCHLIST, PASSES, SCANS, DB]:
        st.write(f"- {p.name}: {'✅' if p.exists() else '—'}  ({p.resolve()})")

