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

col1, col2, col3, col4 = st.columns(4)
col1.metric("Generated At", str(payload.get("generated_at", "-")))
col2.metric("Items", str(payload.get("count", len(items))))
col3.metric("Evaluated", str(scan_summary.get("evaluated", "-")))
col4.metric("Deals", str(scan_summary.get("deals", "-")))

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
