from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

ROOT_DIR = Path(__file__).parent
LATEST_SCAN_PATH = ROOT_DIR / "data" / "latest.json"

st.set_page_config(page_title="Marketplace Flip Dashboard", layout="wide")
st.title("Marketplace Flip Dashboard")
st.caption("Read-only view of the latest scheduled scan run from GitHub Actions.")

st.markdown(
    """
    <style>
        .summary-card {
            background: linear-gradient(135deg, #0f172a, #1e293b);
            border: 1px solid #334155;
            border-radius: 14px;
            padding: 0.9rem 1rem;
            color: #e2e8f0;
            margin-bottom: 0.8rem;
        }
        .summary-card h4 {
            margin: 0;
            font-size: 0.85rem;
            color: #94a3b8;
            letter-spacing: 0.02em;
        }
        .summary-card p {
            margin: 0.2rem 0 0;
            font-size: 1.25rem;
            font-weight: 700;
        }
        .listing-chip {
            display: inline-block;
            margin: 0.2rem 0.25rem 0.2rem 0;
            padding: 0.25rem 0.6rem;
            border-radius: 999px;
            background: #e2e8f0;
            color: #0f172a;
            font-size: 0.8rem;
            border: 1px solid #cbd5e1;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def _load_latest_scan(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _to_display_dataframe(items: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in items:
        rows.append(
            {
                "Title": item.get("title") or "Untitled",
                "Decision": item.get("decision") or "unknown",
                "Buy (£)": item.get("total_buy_gbp"),
                "Resale Est (£)": item.get("resale_est_gbp"),
                "Profit (£)": item.get("expected_profit_gbp"),
                "ROI": item.get("roi"),
                "Confidence": item.get("confidence"),
                "Score": item.get("deal_score"),
                "Link": item.get("url"),
                "Evaluated At": item.get("evaluated_at"),
            }
        )
    return pd.DataFrame(rows)


payload = _load_latest_scan(LATEST_SCAN_PATH)
if payload is None:
    st.info(
        "No scan data found yet. Once GitHub Actions runs `scanner/run_scan.py`, "
        "this dashboard will populate from `data/latest.json`."
    )
    st.stop()

items = payload.get("items") or []
scan_summary = payload.get("scan_summary") or {}
scanned_titles = [str(item.get("title") or "Untitled") for item in items]

col1, col2, col3, col4 = st.columns(4)
col1.markdown(
    f"""
    <div class="summary-card">
        <h4>Generated At</h4>
        <p>{payload.get("generated_at", "-")}</p>
    </div>
    """,
    unsafe_allow_html=True,
)
col2.markdown(
    f"""
    <div class="summary-card">
        <h4>Items Stored</h4>
        <p>{payload.get("count", len(items))}</p>
    </div>
    """,
    unsafe_allow_html=True,
)
col3.markdown(
    f"""
    <div class="summary-card">
        <h4>Scanned Targets</h4>
        <p>{scan_summary.get("scanned_targets", "-")}</p>
    </div>
    """,
    unsafe_allow_html=True,
)
col4.markdown(
    f"""
    <div class="summary-card">
        <h4>Deals Found</h4>
        <p>{scan_summary.get("deals", "-")}</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.subheader("Scanned item overview")
if scanned_titles:
    st.write(f"**{len(scanned_titles)} item(s)** are included in this latest snapshot.")
    st.markdown("".join(f'<span class="listing-chip">{title}</span>' for title in scanned_titles), unsafe_allow_html=True)
else:
    st.write(
        "No item titles are available in this snapshot yet. Once listings are evaluated, their names will appear here."
    )

if not items:
    st.warning("Latest scan completed but returned no evaluated items.")
    st.stop()

frame = _to_display_dataframe(items)
st.dataframe(frame, use_container_width=True, hide_index=True)

st.subheader("Top opportunities")
for item in items[:25]:
    title = item.get("title") or "Untitled"
    decision = item.get("decision") or "unknown"
    profit = item.get("expected_profit_gbp")
    roi = item.get("roi")
    link = item.get("url")
    st.markdown(f"**{title}** · `{decision}`")
    st.markdown(f"Profit: £{profit if profit is not None else '-'} · ROI: {roi if roi is not None else '-'}")
    if link:
        st.markdown(f"[Open listing]({link})")
    reasons = item.get("reasons") or []
    if reasons:
        st.caption("; ".join(str(reason) for reason in reasons))
    st.divider()
