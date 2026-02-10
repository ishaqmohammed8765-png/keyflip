from __future__ import annotations

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

from ebayflip.env import load_dotenv

load_dotenv(ROOT_DIR / ".env")

from ebayflip.dashboard_data import (
    filter_items,
    history_summary_rows,
    items_to_csv_bytes,
    load_history,
    load_latest_scan,
    scan_age_seconds,
    sort_items,
    summarize_items,
)
from ebayflip.deal_insights import enrich_items, plan_portfolio
from ebayflip.config import RunSettings
from ebayflip.safety import safe_external_url
from ebayflip.scan_runner import (
    scan_run_status,
    start_background_scan_if_needed,
    trigger_background_scan,
)

st.set_page_config(page_title="KeyFlip - Flip Scanner", layout="wide")

try:
    from streamlit_autorefresh import st_autorefresh
    auto_refresh_interval = int(os.getenv("AUTO_REFRESH_MS", "60000"))
    st_autorefresh(interval=auto_refresh_interval, key="auto_refresh")
except ImportError:
    pass

def _inject_styles() -> None:
    st.markdown(
        """
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap');
            .stApp {
                font-family: 'Space Grotesk', sans-serif;
                background:
                    radial-gradient(1200px 600px at 90% -10%, rgba(14, 165, 233, 0.14), transparent 50%),
                    radial-gradient(900px 450px at -10% 0%, rgba(34, 197, 94, 0.12), transparent 45%),
                    linear-gradient(180deg, #0b1020, #0f172a);
            }
            .summary-card {
                background: linear-gradient(135deg, #111827, #0f172a);
                border: 1px solid #1f2937;
                border-radius: 14px;
                padding: 0.9rem 1rem;
                color: #e2e8f0;
                margin-bottom: 0.8rem;
                box-shadow: 0 12px 24px rgba(15, 23, 42, 0.24);
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
            .stDataFrame {
                border: 1px solid #1f2937;
                border-radius: 12px;
                overflow: hidden;
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
            .insight-chip {
                display: inline-block;
                margin-right: 0.35rem;
                margin-top: 0.2rem;
                padding: 0.15rem 0.45rem;
                border-radius: 999px;
                font-size: 0.72rem;
                border: 1px solid #334155;
                color: #cbd5e1;
                background: #111827;
            }
            .insight-chip-low { border-color: #16a34a; color: #86efac; }
            .insight-chip-medium { border-color: #f59e0b; color: #fcd34d; }
            .insight-chip-high { border-color: #ef4444; color: #fca5a5; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_app_intro() -> None:
    st.title("KeyFlip")
    st.caption("Automated arbitrage scanner - compare buy-side deals against resale comps.")
    with st.expander("New to flipping? Start here", expanded=False):
        st.markdown(
            "1. In **Dashboard**, click **Run scan now** (the dashboard can auto-scan when data is missing/stale).\n"
            "2. The scanner auto-adds popular-category targets and smart new targets from profitable hits.\n"
            "3. Start with **Minimum confidence** around `0.50` and filter decision to `deal` or `maybe`.\n"
            "4. Use **Max Buy** and **Suggested Offer** to avoid overpaying.\n"
            "5. Check **Capital Plan** to control bankroll and position sizing."
        )


def _format_gbp(value: float | None) -> str:
    amount = float(value or 0.0)
    return f"\u00a3{amount:.2f}"


def _render_summary_cards(
    summary: dict[str, Any],
    actionable_count: int,
    total_edge: float,
    generated_at: str,
    marketplaces_label: str | None = None,
) -> None:
    col1, col2, col3, col4 = st.columns(4)
    col1.markdown(
        f'<div class="summary-card"><h4>Deals Found</h4><p>{summary["deal_count"]}</p></div>',
        unsafe_allow_html=True,
    )
    col2.markdown(
        f'<div class="summary-card"><h4>Maybe Deals</h4><p>{summary["maybe_count"]}</p></div>',
        unsafe_allow_html=True,
    )
    col3.markdown(
        f'<div class="summary-card"><h4>Total Potential Profit</h4><p>{_format_gbp(summary["total_profit"])}</p></div>',
        unsafe_allow_html=True,
    )
    col4.markdown(
        f'<div class="summary-card"><h4>Actionable Deals</h4><p>{actionable_count}</p></div>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Last scan: {generated_at[:19]} | Best score: {summary['best_score']:.1f} | "
        f"Total buy edge: {_format_gbp(total_edge)}"
        + (f" | {marketplaces_label}" if marketplaces_label else "")
    )


def _render_zero_result_diagnostics(items: list[dict[str, Any]], scan_summary: dict[str, Any]) -> None:
    zero_result_targets = scan_summary.get("zero_result_targets") or []
    if not zero_result_targets:
        return
    blocked_count = sum(1 for zrt in zero_result_targets if zrt.get("blocked_reason"))
    with st.expander(f"Targets with no results ({len(zero_result_targets)})", expanded=not items):
        if blocked_count:
            st.warning(
                "Some targets look blocked by anti-bot checks. If this persists, try waiting, enabling Playwright "
                "(`EBAY_USE_PLAYWRIGHT=1`), or switching buy marketplace (`MARKETPLACE=mercari`)."
            )
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


def _render_strategy_controls() -> dict[str, Any]:
    target_profit_default = float(os.getenv("FLIP_TARGET_PROFIT", "20"))
    run_defaults = RunSettings.from_env()
    strategy_col1, strategy_col2, strategy_col3, strategy_col4 = st.columns([1.2, 1, 1, 1])
    with strategy_col1:
        target_profit = st.number_input(
            "Target profit per item (GBP)",
            min_value=0.0,
            value=target_profit_default,
            step=5.0,
            help="Only buy when the model predicts at least this profit.",
        )
    with strategy_col2:
        min_confidence_filter = st.slider(
            "Minimum confidence",
            min_value=0.0,
            max_value=1.0,
            value=0.45,
            step=0.05,
            help="Higher confidence means safer but fewer opportunities.",
        )
    with strategy_col3:
        actionable_only = st.checkbox(
            "Show buy-ready only",
            value=False,
            help="Only show listings currently under your target max buy.",
        )
    with strategy_col4:
        max_cards = st.slider("Results to show", min_value=10, max_value=100, value=30, step=5)
    with st.expander("Deal thresholds (labeling)", expanded=False):
        st.caption(
            "These thresholds control how rows are labelled `deal/maybe/ignore` in the dashboard. "
            "Lower them to see more candidates; raise them to be stricter."
        )
        th_col1, th_col2, th_col3 = st.columns(3)
        with th_col1:
            deal_min_profit = st.number_input(
                "Deal min profit (GBP)",
                min_value=0.0,
                value=float(run_defaults.min_profit_gbp),
                step=1.0,
            )
        with th_col2:
            deal_min_roi = st.number_input(
                "Deal min ROI",
                min_value=0.0,
                max_value=5.0,
                value=float(run_defaults.min_roi),
                step=0.05,
                help="ROI is expressed as a ratio (0.15 = 15%).",
            )
        with th_col3:
            deal_min_confidence = st.number_input(
                "Deal min confidence",
                min_value=0.0,
                max_value=1.0,
                value=float(run_defaults.min_confidence),
                step=0.05,
            )
    planner_col1, planner_col2 = st.columns([1, 1])
    with planner_col1:
        bankroll_gbp = st.number_input(
            "Budget available (GBP)",
            min_value=0.0,
            value=500.0,
            step=25.0,
            help="How much money you can spend right now.",
        )
    with planner_col2:
        max_planned_picks = st.slider(
            "Max planned buys",
            min_value=1,
            max_value=10,
            value=4,
            step=1,
        )
    return {
        "target_profit": target_profit,
        "min_confidence_filter": min_confidence_filter,
        "actionable_only": actionable_only,
        "max_cards": max_cards,
        "bankroll_gbp": bankroll_gbp,
        "max_planned_picks": max_planned_picks,
        "deal_min_profit": deal_min_profit,
        "deal_min_roi": deal_min_roi,
        "deal_min_confidence": deal_min_confidence,
    }


def _render_filter_controls(items: list[dict[str, Any]]) -> dict[str, Any]:
    filter_col1, filter_col2, filter_col3, filter_col4, filter_col5 = st.columns([1, 2, 1, 1, 1.2])
    with filter_col1:
        filter_decision = st.selectbox("Decision", ["All", "deal", "maybe", "ignore"], index=0)
    with filter_col2:
        search_term = st.text_input("Search title", "")
    with filter_col3:
        min_score = st.number_input("Min score", min_value=0.0, value=0.0, step=1.0)
    with filter_col4:
        min_profit = st.number_input("Min profit (GBP)", value=0.0, step=1.0)
    with filter_col5:
        st.download_button(
            label="Export CSV",
            data=items_to_csv_bytes(items),
            file_name="keyflip_scan.csv",
            mime="text/csv",
        )
    return {
        "filter_decision": filter_decision,
        "search_term": search_term,
        "min_score": min_score,
        "min_profit": min_profit,
    }

def _to_display_dataframe(items: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in items:
        profit = item.get("expected_profit_gbp")
        roi = item.get("roi")
        rows.append(
            {
                "Decision": item.get("decision") or "unknown",
                "Title": item.get("title") or "Untitled",
                "Source": item.get("source") or "-",
                "Buy->Sell": f"{item.get('buy_marketplace', '-')}\u2192{item.get('sell_marketplace', '-')}",
                "Buy": f"\u00a3{item.get('total_buy_gbp', 0):.2f}",
                "Resale Est": f"\u00a3{item.get('resale_est_gbp', 0):.2f}",
                "Profit": f"\u00a3{profit:.2f}" if profit is not None else "-",
                "ROI": f"{roi:.0%}" if roi is not None else "-",
                "Confidence": f"{item.get('confidence', 0):.2f}",
                "Score": f"{item.get('deal_score', 0):.1f}",
                "Grade": item.get("flip_grade", "-"),
                "Risk": item.get("risk_band", "-"),
                "Max Buy @ Target": f"\u00a3{item.get('max_total_buy_target_gbp', 0):.2f}",
                "Edge": f"\u00a3{item.get('buy_edge_gbp', 0):.2f}",
                "Location": item.get("location") or "-",
                "Link": safe_external_url(item.get("url")) or "",
            }
        )
    return pd.DataFrame(rows)


def _render_portfolio_plan(filtered_items: list[dict[str, Any]], bankroll_gbp: float, max_planned_picks: int) -> None:
    portfolio = plan_portfolio(
        filtered_items,
        budget_gbp=bankroll_gbp,
        max_items=max_planned_picks,
    )
    if not portfolio:
        return
    planned_buy = sum(float(item.get("total_buy_gbp") or 0.0) for item in portfolio)
    planned_profit = sum(float(item.get("expected_profit_gbp") or 0.0) for item in portfolio)
    st.subheader("Capital Plan")
    plan_col1, plan_col2, plan_col3 = st.columns(3)
    plan_col1.metric("Planned Spend", _format_gbp(planned_buy))
    plan_col2.metric("Planned Profit", _format_gbp(planned_profit))
    plan_col3.metric("Remaining Budget", _format_gbp(max(0.0, bankroll_gbp - planned_buy)))
    plan_rows = [
        {
            "Title": item.get("title", "Untitled"),
            "Buy": _format_gbp(float(item.get("total_buy_gbp") or 0.0)),
            "Profit": _format_gbp(float(item.get("expected_profit_gbp") or 0.0)),
            "Edge": _format_gbp(float(item.get("buy_edge_gbp") or 0.0)),
            "Grade": item.get("flip_grade", "-"),
        }
        for item in portfolio
    ]
    st.dataframe(pd.DataFrame(plan_rows), hide_index=True, use_container_width=True)


def _render_flip_card(item: dict[str, Any]) -> None:
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
    edge = item.get("buy_edge_gbp", 0.0) or 0.0
    grade = item.get("flip_grade", "-")
    risk = item.get("risk_band", "-")
    max_buy = item.get("max_total_buy_target_gbp", 0.0) or 0.0
    offer = item.get("suggested_offer_gbp", 0.0) or 0.0

    tag_class = f"deal-tag-{decision}" if decision in ("deal", "maybe", "ignore") else "deal-tag-ignore"
    risk_chip_class = f"insight-chip insight-chip-{risk}" if risk in {"low", "medium", "high"} else "insight-chip"

    col_left, col_right = st.columns([3.2, 1.2])
    with col_left:
        st.markdown(f'<span class="deal-tag {tag_class}">{decision.upper()}</span>', unsafe_allow_html=True)
        st.markdown(f"**{title[:100]}**")
        profit_str = f"Profit: **{_format_gbp(profit)}**" if profit is not None else "Profit: -"
        location_str = f" | Location: {location}" if location else ""
        st.markdown(f"Buy: **{_format_gbp(buy)}** | Resale: **{_format_gbp(resale)}** | {profit_str}{location_str}")
        st.markdown(
            f'<span class="insight-chip">Grade {grade}</span>'
            f'<span class="{risk_chip_class}">Risk {risk}</span>'
            f'<span class="insight-chip">Max Buy {_format_gbp(max_buy)}</span>'
            f'<span class="insight-chip">Offer {_format_gbp(offer)}</span>'
            f'<span class="insight-chip">Edge {_format_gbp(edge)}</span>',
            unsafe_allow_html=True,
        )
    with col_right:
        if roi is not None:
            st.metric("ROI", f"{roi:.0%}")
        st.caption(f"Confidence: {confidence:.2f} | Score: {score:.1f}")
        if edge < 0:
            st.caption("Over target buy price")
        elif edge > 0:
            st.caption("Within buy target")

    safe_link = safe_external_url(link)
    if safe_link:
        st.link_button("Open listing", safe_link)
    reasons = item.get("reasons") or []
    if reasons:
        with st.expander("Details"):
            for reason in reasons:
                st.caption(str(reason))
    st.divider()


def _render_history_tab() -> None:
    st.subheader("Scan History")
    history = load_history(HISTORY_PATH)
    if not history:
        st.info("No scan history yet. History builds up after multiple scans.")
        return

    history_rows = history_summary_rows(history, limit=50)
    st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)

    chart_data = []
    for entry in history[-50:]:
        summary = entry.get("scan_summary") or {}
        chart_data.append(
            {
                "scan": entry.get("generated_at", "")[:16],
                "Deals": summary.get("deals", 0),
                "Items": entry.get("count", 0),
            }
        )
    if chart_data:
        st.subheader("Deals Over Time")
        chart_df = pd.DataFrame(chart_data)
        if len(chart_df) > 1:
            chart_df = chart_df.set_index("scan")
            st.line_chart(chart_df[["Deals"]])


def _render_targets_tab() -> None:
    st.subheader("Automatic Targets")
    st.caption("Targets are managed automatically by scanner discovery and popular-category seeding.")
    try:
        from ebayflip.db import init_db, list_targets

        db_path = str(DB_PATH)
        init_db(db_path)
        targets = list_targets(db_path)
        if not targets:
            st.info(
                "No targets yet. Run `python scanner/run_scan.py --watch` and the scanner will auto-seed targets."
            )

        if targets:
            st.caption(f"{len(targets)} auto-managed target(s) configured")
            for target in targets:
                with st.expander(f"{'[ON]' if target.enabled else '[OFF]'} {target.name}"):
                    st.text(f"ID: {target.id}")
                    st.text(f"Query: {target.query}")
                    st.text(f"Category: {target.category_id or '-'}")
                    st.text(f"Condition: {target.condition or 'Any'}")
                    st.text(f"Max Buy: {_format_gbp(target.max_buy_gbp)}" if target.max_buy_gbp else "Max Buy: No limit")
                    st.text(
                        f"Max Shipping: {_format_gbp(target.shipping_max_gbp)}"
                        if target.shipping_max_gbp
                        else "Max Shipping: No limit"
                    )
                    st.text(f"Country: {target.country}")
                    st.text(f"Created: {target.created_at}")
        st.divider()
        st.caption(
            "Automatic behavior is controlled from environment variables: "
            "`AUTO_POPULAR_TARGETS`, `POPULAR_TARGETS_PER_CATEGORY`, `AUTO_SMART_TARGETS`, "
            "`AUTO_SMART_TARGET_LIMIT`, `MIN_SMART_TARGET_CONFIDENCE`, `MIN_SMART_TARGET_PROFIT_GBP`."
        )

    except Exception as exc:
        st.error(f"Could not load target management: {exc}")


_inject_styles()
_render_app_intro()


# --- Navigation tabs ---
tab_dashboard, tab_history, tab_targets = st.tabs(["Dashboard", "Scan History", "Automatic Targets"])

# ======== DASHBOARD TAB ========
with tab_dashboard:
    payload = load_latest_scan(LATEST_SCAN_PATH)
    auto_started = start_background_scan_if_needed(payload)
    status = scan_run_status()
    scan_col1, scan_col2, scan_col3 = st.columns([1.2, 1, 2.2])
    with scan_col1:
        if st.button("Run scan now", type="primary"):
            started = trigger_background_scan(force=True)
            if started:
                st.success("Scan started in the background. This page will update on the next refresh.")
            else:
                st.warning("Scan already running (or recently ran). Try again in a minute.")
    with scan_col2:
        st.caption(f"Scanner: **{status.get('status', 'idle')}**")
    with scan_col3:
        if status.get("message"):
            st.caption(
                f"{status.get('message')} | started: {status.get('started_at') or '-'} | ended: {status.get('ended_at') or '-'}"
            )
    if payload is None:
        if status.get("status") == "running" or auto_started:
            st.info("No scan data yet. Auto-scan is running; wait for refresh (usually under a minute).")
        else:
            st.info("No scan data yet. Click **Run scan now** to populate the dashboard.")
    else:
        run_settings = RunSettings.from_env()
        controls = _render_strategy_controls()

        items = enrich_items(
            sort_items(payload.get("items") or []),
            run_settings,
            target_profit_gbp=controls["target_profit"],
        )
        # Re-label items in the UI based on the thresholds the user picked above.
        for item in items:
            item["decision_model"] = item.get("decision")
            profit = float(item.get("expected_profit_gbp") or 0.0)
            roi = float(item.get("roi") or 0.0)
            confidence = float(item.get("confidence") or 0.0)
            if (
                profit >= float(controls["deal_min_profit"])
                and roi >= float(controls["deal_min_roi"])
                and confidence >= float(controls["deal_min_confidence"])
            ):
                item["decision"] = "deal"
            elif profit >= 0 and roi >= 0.10 and confidence >= 0.35:
                item["decision"] = "maybe"
            else:
                item["decision"] = "ignore"
            edge = float(item.get("buy_edge_gbp") or 0.0)
            item["is_actionable"] = edge > 0 and profit > 0 and confidence >= float(controls["deal_min_confidence"])
        scan_summary = payload.get("scan_summary") or {}
        summary = summarize_items(items)
        actionable_count = sum(1 for item in items if item.get("is_actionable"))
        total_edge = sum(max(0.0, item.get("buy_edge_gbp", 0.0) or 0.0) for item in items)
        marketplaces = payload.get("marketplaces") or {}
        buy_market = str(marketplaces.get("buy") or scan_summary.get("buy_marketplace") or "buy")
        sell_market = str(marketplaces.get("sell") or scan_summary.get("sell_marketplace") or "sell")
        stale_pruned = int(scan_summary.get("stale_pruned") or 0)
        age_seconds = scan_age_seconds(payload)
        if age_seconds is not None and age_seconds > 60 * 60 * 6:
            st.warning("Scan data is older than 6 hours. Run a fresh scan before purchasing inventory.")

        _render_summary_cards(
            summary,
            actionable_count,
            total_edge,
            str(payload.get("generated_at", "-")),
            marketplaces_label=f"Buy: {buy_market} -> Sell comps: {sell_market}",
        )
        if stale_pruned > 0:
            st.caption(f"Auto-cleanup removed {stale_pruned} stale listing(s) this cycle.")
        _render_zero_result_diagnostics(items, scan_summary)

        if not items:
            st.warning("Latest scan completed but returned no evaluated items. Check the diagnostics above for details.")
        else:
            st.subheader("Flip Opportunities")
            filter_controls = _render_filter_controls(items)

            filtered_items = filter_items(
                items,
                decision=filter_controls["filter_decision"],
                search_term=filter_controls["search_term"],
                min_score=filter_controls["min_score"],
                min_profit=filter_controls["min_profit"] if filter_controls["min_profit"] > 0 else None,
            )
            filtered_items = [
                item for item in filtered_items if (item.get("confidence") or 0.0) >= controls["min_confidence_filter"]
            ]
            if controls["actionable_only"]:
                filtered_items = [item for item in filtered_items if item.get("is_actionable")]

            if not filtered_items:
                st.info("No listings match current filters. Lower min score/profit or confidence to widen results.")

            frame = _to_display_dataframe(filtered_items)
            st.dataframe(frame, use_container_width=True, hide_index=True)

            _render_portfolio_plan(
                filtered_items,
                bankroll_gbp=controls["bankroll_gbp"],
                max_planned_picks=controls["max_planned_picks"],
            )

            st.subheader("Top Flips")
            for item in filtered_items[: controls["max_cards"]]:
                _render_flip_card(item)


# ======== SCAN HISTORY TAB ========
with tab_history:
    _render_history_tab()


# ======== TARGET MANAGEMENT TAB ========
with tab_targets:
    _render_targets_tab()
