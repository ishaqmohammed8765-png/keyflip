from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from keyflip.config import (
    DEFAULT_PASSES_COLUMNS,
    DEFAULT_SCANS_COLUMNS,
    DEFAULT_WATCHLIST_COLUMNS,
    RunConfig,
)
from keyflip.core import build_watchlist, scan_watchlist

ROOT_DIR = Path(__file__).parent.resolve()
WATCHLIST_CSV = ROOT_DIR / "watchlist.csv"
SCANS_CSV = ROOT_DIR / "scans.csv"
PASSES_CSV = ROOT_DIR / "passes.csv"
DB_PATH = ROOT_DIR / "price_cache.sqlite"


def read_csv_if_present(path: Path) -> Optional[pd.DataFrame]:
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


def write_watchlist(df: pd.DataFrame) -> None:
    WATCHLIST_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.reindex(columns=DEFAULT_WATCHLIST_COLUMNS).to_csv(WATCHLIST_CSV, index=False)


def build_config(
    *,
    allow_non_gbp: bool,
    scan_limit: int,
    rate_limit_per_min: int,
    min_profit_gbp: float,
    min_roi: float,
) -> RunConfig:
    return RunConfig.from_kwargs(
        root=ROOT_DIR,
        allow_non_gbp=allow_non_gbp,
        scan_limit=scan_limit,
        rate_limit_per_min=rate_limit_per_min,
        min_profit_gbp=min_profit_gbp,
        min_roi=min_roi,
    )


st.set_page_config(page_title="Keyflip", layout="wide")

if "auto_scan" not in st.session_state:
    st.session_state.auto_scan = False
if "last_auto_scan" not in st.session_state:
    st.session_state.last_auto_scan = 0.0

st.markdown("## Keyflip — eBay Mispricing Radar")
st.caption("Manual arbitrage assistant: no auto-buying or auto-selling.")

with st.sidebar:
    st.markdown("### Scan Settings")
    allow_non_gbp = st.toggle("Convert non-GBP listings", value=False)
    rate_limit_per_min = st.slider("Rate limit (req/min)", 10, 120, 60, 5)
    scan_limit = st.slider("Queries per scan", 1, 200, 50, 5)

    st.markdown("### Deal Thresholds")
    min_profit_gbp = st.number_input("Min profit (£)", min_value=0.0, max_value=200.0, value=20.0, step=1.0)
    min_roi = st.number_input("Min ROI", min_value=0.0, max_value=2.0, value=0.2, step=0.05)

    st.markdown("### Watchlist")
    if st.button("Create watchlist template", use_container_width=True):
        build_watchlist(build_config(
            allow_non_gbp=allow_non_gbp,
            scan_limit=scan_limit,
            rate_limit_per_min=rate_limit_per_min,
            min_profit_gbp=min_profit_gbp,
            min_roi=min_roi,
        ), WATCHLIST_CSV, overwrite=True)
        st.success("watchlist.csv template created.")


col1, col2, col3 = st.columns([1.1, 1.1, 1.5])
with col1:
    run_scan = st.button("Run scan now", use_container_width=True)
with col2:
    st.session_state.auto_scan = st.toggle("Auto-scan", value=st.session_state.auto_scan)
with col3:
    auto_interval_min = st.number_input("Auto-scan interval (min)", min_value=1, max_value=120, value=10, step=1)

if run_scan:
    cfg = build_config(
        allow_non_gbp=allow_non_gbp,
        scan_limit=scan_limit,
        rate_limit_per_min=rate_limit_per_min,
        min_profit_gbp=min_profit_gbp,
        min_roi=min_roi,
    )
    with st.spinner("Scanning eBay listings..."):
        scan_watchlist(cfg, WATCHLIST_CSV, SCANS_CSV, PASSES_CSV, DB_PATH)
    st.success("Scan complete. Results saved to scans.csv.")

if st.session_state.auto_scan:
    now = time.time()
    interval_s = max(60, int(auto_interval_min * 60))
    if now - st.session_state.last_auto_scan >= interval_s:
        cfg = build_config(
            allow_non_gbp=allow_non_gbp,
            scan_limit=scan_limit,
            rate_limit_per_min=rate_limit_per_min,
            min_profit_gbp=min_profit_gbp,
            min_roi=min_roi,
        )
        with st.spinner("Auto-scan running..."):
            scan_watchlist(cfg, WATCHLIST_CSV, SCANS_CSV, PASSES_CSV, DB_PATH)
        st.session_state.last_auto_scan = now
    st.caption("Auto-scan runs when the page refreshes. Streamlit Cloud may restrict background loops.")
    st_autorefresh(interval=interval_s * 1000, key="auto_scan_refresh")

st.divider()

watch_df = read_csv_if_present(WATCHLIST_CSV)
scans_df = read_csv_if_present(SCANS_CSV)
passes_df = read_csv_if_present(PASSES_CSV)

left, right = st.columns([1.5, 1])
with left:
    st.markdown("### Top Deals")
    if scans_df is None or scans_df.empty:
        st.info("No scan results yet.")
    else:
        deals = scans_df.copy()
        for col in ["est_profit_gbp", "est_roi", "score"]:
            if col in deals.columns:
                deals[col] = pd.to_numeric(deals[col], errors="coerce")
        deals = deals[(deals["est_profit_gbp"] >= min_profit_gbp) & (deals["est_roi"] >= min_roi)]
        if deals.empty:
            st.info("No listings meet the current thresholds.")
        else:
            deals = deals.sort_values(by=["score", "est_profit_gbp"], ascending=False)
            st.dataframe(
                deals.head(50)[
                    [
                        "scanned_at_iso",
                        "query_id",
                        "title",
                        "listing_url",
                        "total_gbp",
                        "sold_comp_median_gbp",
                        "est_profit_gbp",
                        "est_roi",
                        "score",
                    ]
                ],
                use_container_width=True,
                height=420,
            )

with right:
    st.markdown("### Quick Add Query")
    with st.form("add_query"):
        query_id = st.text_input("Query ID", value="q3")
        query_text = st.text_input("Query text", value="Apple AirPods Pro")
        category_id = st.text_input("Category ID (optional)", value="")
        condition = st.text_input("Condition ID (optional)", value="")
        max_buy_gbp = st.text_input("Max buy (£, optional)", value="")
        keywords_include = st.text_input("Keywords include (comma)", value="")
        keywords_exclude = st.text_input("Keywords exclude (comma)", value="")
        min_sold_comp_gbp = st.text_input("Min sold comp (£, optional)", value="")
        min_roi_override = st.text_input("Min ROI (optional)", value="")
        min_profit_override = st.text_input("Min profit (£, optional)", value="")
        submitted = st.form_submit_button("Add to watchlist")

    if submitted:
        new_row = {
            "query_id": query_id.strip(),
            "query_text": query_text.strip(),
            "category_id": category_id.strip(),
            "condition": condition.strip(),
            "max_buy_gbp": max_buy_gbp.strip(),
            "keywords_include": keywords_include.strip(),
            "keywords_exclude": keywords_exclude.strip(),
            "min_sold_comp_gbp": min_sold_comp_gbp.strip(),
            "min_roi": min_roi_override.strip(),
            "min_profit_gbp": min_profit_override.strip(),
        }
        if watch_df is None:
            watch_df = pd.DataFrame(columns=DEFAULT_WATCHLIST_COLUMNS)
        watch_df = pd.concat([watch_df, pd.DataFrame([new_row])], ignore_index=True)
        write_watchlist(watch_df)
        st.success("Added query to watchlist.csv")

st.divider()

st.markdown("### Latest Scans")
if scans_df is None or scans_df.empty:
    st.info("No scan results yet.")
else:
    st.dataframe(scans_df.tail(200), use_container_width=True, height=360)

st.markdown("### Watchlist")
if watch_df is None or watch_df.empty:
    st.info("No watchlist yet. Create a template or add a query.")
else:
    editable = st.data_editor(watch_df, num_rows="dynamic", use_container_width=True, height=360)
    if st.button("Save watchlist edits"):
        write_watchlist(editable)
        st.success("watchlist.csv saved.")

st.markdown("### Passes")
if passes_df is None or passes_df.empty:
    st.info("No passes yet.")
else:
    st.dataframe(passes_df.tail(200), use_container_width=True, height=300)

st.divider()

st.markdown("### Environment")
st.code(
    "\n".join(
        [
            "Required env vars:",
            "- EBAY_APP_ID=...  # eBay Finding API App ID",
            "Optional:",
            "- EBAY_GLOBAL_ID=EBAY-GB",
        ]
    )
)

st.caption("Keyflip is alert-only: no auto-buying or auto-selling.")
