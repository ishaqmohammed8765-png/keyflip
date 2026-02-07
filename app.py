from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

ROOT_DIR = Path(__file__).parent
LATEST_SCAN_PATH = ROOT_DIR / "data" / "latest.json"

st.set_page_config(page_title="KeyFlip - Flip Scanner", layout="wide")
st.title("KeyFlip")
st.caption("Personal flip scanner - find underpriced items and flip for profit.")

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
        .deal-tag {
            display: inline-block;
            padding: 0.15rem 0.5rem;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .deal-tag-deal { background: #16a34a; color: white; }
        .deal-tag-maybe { background: #d97706; color: white; }
        .deal-tag-ignore { background: #6b7280; color: white; }
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
        profit = item.get("expected_profit_gbp")
        roi = item.get("roi")
        rows.append(
            {
                "Decision": item.get("decision") or "unknown",
                "Title": item.get("title") or "Untitled",
                "Buy": f"\u00a3{item.get('total_buy_gbp', 0):.2f}",
                "Resale Est": f"\u00a3{item.get('resale_est_gbp', 0):.2f}",
                "Profit": f"\u00a3{profit:.2f}" if profit is not None else "-",
                "ROI": f"{roi:.0%}" if roi is not None else "-",
                "Confidence": f"{item.get('confidence', 0):.2f}",
                "Score": f"{item.get('deal_score', 0):.1f}",
                "Link": item.get("url") or "",
            }
        )
    return pd.DataFrame(rows)


payload = _load_latest_scan(LATEST_SCAN_PATH)
if payload is None:
    st.info(
        "No scan data found yet. Run `python scanner/run_scan.py` to populate this dashboard."
    )
    st.stop()

items = payload.get("items") or []
scan_summary = payload.get("scan_summary") or {}

# Sort items: deals first, then maybe, then ignore; within each group by deal_score desc
decision_order = {"deal": 0, "maybe": 1, "ignore": 2}
items.sort(key=lambda x: (decision_order.get(x.get("decision", "ignore"), 3), -(x.get("deal_score") or 0)))

deal_count = sum(1 for item in items if item.get("decision") == "deal")
maybe_count = sum(1 for item in items if item.get("decision") == "maybe")
total_potential = sum(
    item.get("expected_profit_gbp", 0)
    for item in items
    if item.get("decision") in ("deal", "maybe") and item.get("expected_profit_gbp", 0) > 0
)

col1, col2, col3, col4 = st.columns(4)
col1.markdown(
    f"""
    <div class="summary-card">
        <h4>Deals Found</h4>
        <p>{deal_count}</p>
    </div>
    """,
    unsafe_allow_html=True,
)
col2.markdown(
    f"""
    <div class="summary-card">
        <h4>Maybe Deals</h4>
        <p>{maybe_count}</p>
    </div>
    """,
    unsafe_allow_html=True,
)
col3.markdown(
    f"""
    <div class="summary-card">
        <h4>Total Potential Profit</h4>
        <p>\u00a3{total_potential:.2f}</p>
    </div>
    """,
    unsafe_allow_html=True,
)
col4.markdown(
    f"""
    <div class="summary-card">
        <h4>Last Scan</h4>
        <p>{payload.get("generated_at", "-")[:19]}</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not items:
    st.warning("Latest scan completed but returned no evaluated items.")
    st.stop()

# Filter controls
st.subheader("Flip Opportunities")
filter_decision = st.selectbox("Filter by decision", ["All", "deal", "maybe", "ignore"], index=0)
filtered_items = items if filter_decision == "All" else [i for i in items if i.get("decision") == filter_decision]

frame = _to_display_dataframe(filtered_items)
st.dataframe(frame, use_container_width=True, hide_index=True)

st.subheader("Top Flips")
for item in filtered_items[:30]:
    title = item.get("title") or "Untitled"
    decision = item.get("decision") or "unknown"
    profit = item.get("expected_profit_gbp")
    roi = item.get("roi")
    buy = item.get("total_buy_gbp", 0)
    resale = item.get("resale_est_gbp", 0)
    link = item.get("url")
    confidence = item.get("confidence", 0)
    score = item.get("deal_score", 0)

    tag_class = f"deal-tag-{decision}" if decision in ("deal", "maybe", "ignore") else "deal-tag-ignore"

    col_left, col_right = st.columns([3, 1])
    with col_left:
        st.markdown(
            f'<span class="deal-tag {tag_class}">{decision.upper()}</span> **{title[:100]}**',
            unsafe_allow_html=True,
        )
        profit_str = f"Profit: **\u00a3{profit:.2f}**" if profit is not None else ""
        st.markdown(f"Buy: **\u00a3{buy:.2f}** | Resale: **\u00a3{resale:.2f}** | {profit_str}")
    with col_right:
        if roi is not None:
            st.metric("ROI", f"{roi:.0%}")
        st.caption(f"Confidence: {confidence:.2f} | Score: {score:.1f}")

    if link:
        st.markdown(f"[Open listing]({link})")
    reasons = item.get("reasons") or []
    if reasons:
        with st.expander("Details"):
            for reason in reasons:
                st.caption(str(reason))
    st.divider()
