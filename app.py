from __future__ import annotations

import csv
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

ROOT_DIR = Path(__file__).parent
LATEST_SCAN_PATH = ROOT_DIR / "data" / "latest.json"
HISTORY_PATH = ROOT_DIR / "data" / "history.jsonl"
DB_PATH = ROOT_DIR / "ebayflip.sqlite"

# Ensure package is importable
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

st.set_page_config(page_title="KeyFlip - Flip Scanner", layout="wide")

try:
    from streamlit_autorefresh import st_autorefresh
    auto_refresh_interval = int(os.getenv("AUTO_REFRESH_MS", "60000"))
    st_autorefresh(interval=auto_refresh_interval, key="auto_refresh")
except ImportError:
    pass

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
    except (json.JSONDecodeError, OSError):
        return None


def _load_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    entries = []
    try:
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


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
                "Location": item.get("location") or "-",
                "Link": item.get("url") or "",
            }
        )
    return pd.DataFrame(rows)


def _items_to_csv(items: list[dict[str, Any]]) -> bytes:
    output = io.StringIO()
    fieldnames = [
        "decision", "title", "url", "total_buy_gbp", "resale_est_gbp",
        "expected_profit_gbp", "roi", "confidence", "deal_score",
        "location", "listing_type", "evaluated_at", "reasons",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = dict(item)
        reasons = row.get("reasons") or []
        row["reasons"] = "; ".join(str(r) for r in reasons)
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


# --- Navigation tabs ---
tab_dashboard, tab_history, tab_targets = st.tabs(["Dashboard", "Scan History", "Manage Targets"])

# ======== DASHBOARD TAB ========
with tab_dashboard:
    payload = _load_latest_scan(LATEST_SCAN_PATH)
    if payload is None:
        st.info(
            "No scan data found yet. Run `python scanner/run_scan.py` to populate this dashboard."
        )
    else:
        items = payload.get("items") or []
        scan_summary = payload.get("scan_summary") or {}

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
            f'<div class="summary-card"><h4>Deals Found</h4><p>{deal_count}</p></div>',
            unsafe_allow_html=True,
        )
        col2.markdown(
            f'<div class="summary-card"><h4>Maybe Deals</h4><p>{maybe_count}</p></div>',
            unsafe_allow_html=True,
        )
        col3.markdown(
            f'<div class="summary-card"><h4>Total Potential Profit</h4><p>\u00a3{total_potential:.2f}</p></div>',
            unsafe_allow_html=True,
        )
        col4.markdown(
            f'<div class="summary-card"><h4>Last Scan</h4><p>{payload.get("generated_at", "-")[:19]}</p></div>',
            unsafe_allow_html=True,
        )

        # Zero-result diagnostics
        zero_result_targets = scan_summary.get("zero_result_targets") or []
        if zero_result_targets:
            with st.expander(f"Targets with no results ({len(zero_result_targets)})", expanded=not items):
                for zrt in zero_result_targets:
                    st.markdown(f"**{zrt.get('target_name', 'Unknown')}** (query: `{zrt.get('target_query', '-')}`)")
                    if zrt.get("blocked_reason"):
                        st.error(f"Blocked: {zrt['blocked_reason']} - {zrt.get('blocked_message', '')}")
                    retry_report = zrt.get("retry_report") or []
                    if retry_report:
                        for note in retry_report:
                            st.caption(f"  {note}")
                    rejections = zrt.get("rejection_counts") or {}
                    active_rejections = {k: v for k, v in rejections.items() if v > 0}
                    if active_rejections:
                        st.caption(f"  Rejected: {active_rejections}")
                    raw_c = zrt.get("raw_count", 0)
                    filt_c = zrt.get("filtered_count", 0)
                    if raw_c > 0:
                        st.caption(f"  Found {raw_c} raw, {filt_c} after filtering")

        if not items:
            st.warning("Latest scan completed but returned no evaluated items. Check the diagnostics above for details.")
        else:
            # Filter controls
            st.subheader("Flip Opportunities")
            filter_col1, filter_col2, filter_col3 = st.columns([1, 2, 1])
            with filter_col1:
                filter_decision = st.selectbox("Filter by decision", ["All", "deal", "maybe", "ignore"], index=0)
            with filter_col2:
                search_term = st.text_input("Search titles", "")
            with filter_col3:
                st.download_button(
                    label="Export CSV",
                    data=_items_to_csv(items),
                    file_name="keyflip_scan.csv",
                    mime="text/csv",
                )

            filtered_items = items if filter_decision == "All" else [i for i in items if i.get("decision") == filter_decision]
            if search_term:
                search_lower = search_term.lower()
                filtered_items = [i for i in filtered_items if search_lower in (i.get("title") or "").lower()]

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
                location = item.get("location")
                image_url = item.get("image_url")

                tag_class = f"deal-tag-{decision}" if decision in ("deal", "maybe", "ignore") else "deal-tag-ignore"

                col_left, col_right = st.columns([3, 1])
                with col_left:
                    st.markdown(
                        f'<span class="deal-tag {tag_class}">{decision.upper()}</span> **{title[:100]}**',
                        unsafe_allow_html=True,
                    )
                    profit_str = f"Profit: **\u00a3{profit:.2f}**" if profit is not None else ""
                    location_str = f" | Location: {location}" if location else ""
                    st.markdown(f"Buy: **\u00a3{buy:.2f}** | Resale: **\u00a3{resale:.2f}** | {profit_str}{location_str}")
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


# ======== SCAN HISTORY TAB ========
with tab_history:
    st.subheader("Scan History")
    history = _load_history()
    if not history:
        st.info("No scan history yet. History builds up after multiple scans.")
    else:
        history_rows = []
        for entry in reversed(history[-50:]):
            summary = entry.get("scan_summary") or {}
            history_rows.append({
                "Scan Time": entry.get("generated_at", "-")[:19],
                "Items": entry.get("count", 0),
                "Targets Scanned": summary.get("scanned_targets", 0),
                "New Listings": summary.get("new_listings", 0),
                "Deals": summary.get("deals", 0),
                "Evaluated": summary.get("evaluated", 0),
                "Cap Reached": "Yes" if summary.get("request_cap_reached") else "No",
            })
        st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)

        # Deals over time chart
        chart_data = []
        for entry in history[-50:]:
            summary = entry.get("scan_summary") or {}
            chart_data.append({
                "scan": entry.get("generated_at", "")[:16],
                "Deals": summary.get("deals", 0),
                "Items": entry.get("count", 0),
            })
        if chart_data:
            st.subheader("Deals Over Time")
            chart_df = pd.DataFrame(chart_data)
            if len(chart_df) > 1:
                chart_df = chart_df.set_index("scan")
                st.line_chart(chart_df[["Deals"]])


# ======== TARGET MANAGEMENT TAB ========
with tab_targets:
    st.subheader("Manage Scan Targets")
    try:
        from ebayflip.db import add_target, delete_target, init_db, list_targets, update_target
        from ebayflip.models import Target

        db_path = str(DB_PATH)
        if DB_PATH.exists():
            targets = list_targets(db_path)
        else:
            st.info("Database not initialized yet. Run a scan first to create the database.")
            targets = []

        if targets:
            st.caption(f"{len(targets)} target(s) configured")
            for target in targets:
                with st.expander(f"{'[ON]' if target.enabled else '[OFF]'} {target.name}"):
                    st.text(f"ID: {target.id}")
                    st.text(f"Query: {target.query}")
                    st.text(f"Category: {target.category_id or '-'}")
                    st.text(f"Condition: {target.condition or 'Any'}")
                    st.text(f"Max Buy: \u00a3{target.max_buy_gbp}" if target.max_buy_gbp else "Max Buy: No limit")
                    st.text(f"Max Shipping: \u00a3{target.shipping_max_gbp}" if target.shipping_max_gbp else "Max Shipping: No limit")
                    st.text(f"Country: {target.country}")
                    st.text(f"Created: {target.created_at}")

                    col_toggle, col_delete = st.columns(2)
                    with col_toggle:
                        new_enabled = st.checkbox(
                            "Enabled",
                            value=target.enabled,
                            key=f"toggle_{target.id}",
                        )
                        if new_enabled != target.enabled:
                            import dataclasses
                            updated = dataclasses.replace(target, enabled=new_enabled)
                            update_target(db_path, updated)
                            st.rerun()
                    with col_delete:
                        if st.button(f"Delete", key=f"delete_{target.id}"):
                            delete_target(db_path, target.id)
                            st.rerun()

        st.divider()
        st.subheader("Add New Target")
        with st.form("add_target_form"):
            new_name = st.text_input("Target Name", placeholder="e.g., iPhone 15 Pro")
            new_query = st.text_input("Search Query", placeholder="e.g., iPhone 15 Pro 128GB")
            new_max_buy = st.number_input("Max Buy Price (\u00a3)", min_value=0.0, value=0.0, step=10.0)
            new_max_shipping = st.number_input("Max Shipping (\u00a3)", min_value=0.0, value=0.0, step=1.0)
            new_country = st.selectbox("Country", ["UK", "US", "DE", "FR", "AU"], index=0)
            submitted = st.form_submit_button("Add Target")
            if submitted and new_name.strip():
                init_db(db_path)
                add_target(
                    db_path,
                    Target(
                        id=None,
                        name=new_name.strip(),
                        query=new_query.strip() or new_name.strip(),
                        max_buy_gbp=new_max_buy if new_max_buy > 0 else None,
                        shipping_max_gbp=new_max_shipping if new_max_shipping > 0 else None,
                        country=new_country,
                    ),
                )
                st.success(f"Added target: {new_name.strip()}")
                st.rerun()

    except Exception as exc:
        st.error(f"Could not load target management: {exc}")
