from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from ebayflip import get_logger
from ebayflip.config import (
    AlertSettings,
    AppConfig,
    RunSettings,
    DEFAULT_SCAN_INTERVAL_MIN,
    MIN_CONFIDENCE,
    MIN_PROFIT_GBP,
    MIN_ROI,
)
from ebayflip.db import (
    delete_target,
    init_db,
    list_comps_by_listing,
    list_evaluations_with_listings,
    list_targets,
)
from ebayflip.ebay_client import EbayClient
from ebayflip.models import Target
from ebayflip.scheduler import run_scan
from ebayflip.taxonomy import (
    Category,
    ensure_categories_loaded,
    get_category_path,
    get_child_categories,
    get_top_categories,
)

LOGGER = get_logger()
ROOT_DIR = Path(__file__).parent
DB_PATH = str(ROOT_DIR / "ebayflip.sqlite")

st.set_page_config(page_title="eBay Flip Scanner", layout="wide")

if "last_scan" not in st.session_state:
    st.session_state.last_scan = None
if "auto_scan" not in st.session_state:
    st.session_state.auto_scan = False
if "auto_scan_interval" not in st.session_state:
    st.session_state.auto_scan_interval = DEFAULT_SCAN_INTERVAL_MIN

if "settings" not in st.session_state:
    st.session_state.settings = RunSettings()
if "alerts" not in st.session_state:
    st.session_state.alerts = AlertSettings(discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL"))

init_db(DB_PATH)

st.title("eBay Flip Scanner")
st.caption("Scan eBay listings for underpriced flips, estimate resale, and alert on deals.")


def build_config() -> AppConfig:
    return AppConfig(
        db_path=DB_PATH,
        run=st.session_state.settings,
        alerts=st.session_state.alerts,
    )


def build_client() -> EbayClient:
    return EbayClient(st.session_state.settings, app_id=os.getenv("EBAY_APP_ID"))


def _format_category_option(option: Optional[Category]) -> str:
    return "Any" if option is None else option.name


def _category_selectbox(
    label: str,
    options: list[Category],
    selected_category_id: Optional[str],
    key: str,
) -> Optional[Category]:
    choices: list[Optional[Category]] = [None] + options
    index = 0
    if selected_category_id:
        for idx, option in enumerate(choices):
            if option and option.category_id == selected_category_id:
                index = idx
                break
    return st.selectbox(label, choices, index=index, format_func=_format_category_option, key=key)


def _render_category_picker(
    prefix: str,
    selected_category_id: Optional[str],
) -> Optional[str]:
    top_categories = get_top_categories()
    selected_path = get_category_path(selected_category_id) if selected_category_id else []

    selected_top = _category_selectbox(
        "Category",
        top_categories,
        selected_path[0].category_id if selected_path else None,
        key=f"{prefix}_category",
    )

    selected_sub: Optional[Category] = None
    child_categories = get_child_categories(selected_top.category_id) if selected_top else []
    if child_categories:
        selected_sub = _category_selectbox(
            "Subcategory",
            child_categories,
            selected_path[1].category_id if len(selected_path) > 1 else None,
            key=f"{prefix}_subcategory",
        )

    selected_sub_sub: Optional[Category] = None
    sub_children = get_child_categories(selected_sub.category_id) if selected_sub else []
    if sub_children:
        selected_sub_sub = _category_selectbox(
            "Sub-subcategory",
            sub_children,
            selected_path[2].category_id if len(selected_path) > 2 else None,
            key=f"{prefix}_subsubcategory",
        )

    chosen = selected_sub_sub or selected_sub or selected_top
    if chosen:
        category_path = " \u203a ".join(category.name for category in get_category_path(chosen.category_id))
        st.caption(f"Selected category: {category_path}")
        return chosen.category_id
    return None


def _format_category_path(category_id: Optional[str]) -> str:
    if not category_id:
        return "-"
    path = get_category_path(category_id)
    if not path:
        return category_id
    return " \u203a ".join(category.name for category in path)


st.sidebar.header("Scan Controls")
run_scan_now = st.sidebar.button("Scan now", use_container_width=True)

st.sidebar.toggle("Auto-scan", key="auto_scan")
interval_min = st.sidebar.number_input(
    "Auto-scan interval (minutes)",
    min_value=1,
    max_value=120,
    value=st.session_state.auto_scan_interval,
    step=1,
)
st.session_state.auto_scan_interval = interval_min

if run_scan_now:
    config = build_config()
    client = build_client()
    with st.spinner("Scanning eBay..."):
        summary = run_scan(config, client)
    st.session_state.last_scan = summary.last_scan
    st.success(
        f"Scan complete: {summary.scanned_targets} targets, {summary.evaluated} listings evaluated, {summary.deals} deals."
    )

if st.session_state.auto_scan:
    interval_s = int(st.session_state.auto_scan_interval * 60)
    st_autorefresh(interval=interval_s * 1000, key="auto_scan_refresh")
    if st.session_state.last_scan:
        last_scan_time = datetime.fromisoformat(st.session_state.last_scan)
        elapsed = (datetime.utcnow() - last_scan_time).total_seconds()
        if elapsed >= interval_s:
            config = build_config()
            client = build_client()
            with st.spinner("Auto-scan running..."):
                summary = run_scan(config, client)
            st.session_state.last_scan = summary.last_scan
    else:
        st.info("Auto-scan enabled. A scan will run on the next refresh.")


Tabs = st.tabs(["Dashboard", "Targets", "Deals Feed", "Settings"])

with Tabs[0]:
    targets = list_targets(DB_PATH)
    evaluations = list_evaluations_with_listings(DB_PATH)
    eval_df = pd.DataFrame(evaluations)
    deals_today = 0
    if not eval_df.empty:
        eval_df["evaluated_at"] = pd.to_datetime(eval_df["evaluated_at"], errors="coerce")
        today = pd.Timestamp.utcnow().date()
        deals_today = eval_df[
            (eval_df["decision"] == "deal") & (eval_df["evaluated_at"].dt.date == today)
        ].shape[0]

    col1, col2, col3 = st.columns(3)
    col1.metric("Enabled targets", sum(1 for t in targets if t.enabled))
    col2.metric("Deals today", deals_today)
    col3.metric("Last scan", st.session_state.last_scan or "-")

    st.subheader("Recent Deals")
    if eval_df.empty:
        st.info("No evaluations yet. Run a scan to populate results.")
    else:
        deals_df = eval_df[eval_df["decision"] == "deal"].copy()
        if deals_df.empty:
            st.info("No deals flagged yet.")
        else:
            display = deals_df[[
                "title",
                "expected_profit_gbp",
                "roi",
                "confidence",
                "deal_score",
                "url",
                "evaluated_at",
            ]].sort_values(by=["deal_score"], ascending=False)
            st.dataframe(display, use_container_width=True, height=320)

with Tabs[1]:
    st.subheader("Targets")
    targets = list_targets(DB_PATH)
    categories_ready = ensure_categories_loaded(DB_PATH)
    target_df = pd.DataFrame([dataclasses.asdict(t) for t in targets]) if targets else pd.DataFrame()
    if target_df.empty:
        st.info("No targets yet. Add one below.")
    else:
        if categories_ready:
            target_df["category"] = target_df["category_id"].apply(_format_category_path)
        else:
            target_df["category"] = target_df["category_id"].fillna("-")
        st.dataframe(
            target_df[[
                "id",
                "name",
                "query",
                "category",
                "condition",
                "max_buy_gbp",
                "shipping_max_gbp",
                "listing_type",
                "country",
                "enabled",
            ]],
            use_container_width=True,
            height=260,
        )

    with st.expander("Add target", expanded=True):
        with st.form("add_target_form"):
            name = st.text_input("Name")
            query = st.text_input("Keywords")
            if categories_ready:
                category_id = _render_category_picker("add", None)
            else:
                st.warning("Category list unavailable. Category filters are disabled for now.")
                category_id = None
            condition = st.text_input("Condition (optional)")
            max_buy_gbp = st.number_input("Max buy (£)", min_value=0.0, value=0.0, step=1.0)
            shipping_max_gbp = st.number_input("Max shipping (£)", min_value=0.0, value=0.0, step=1.0)
            listing_type = st.selectbox("Listing type", ["any", "auction", "bin"])
            enabled = st.toggle("Enabled", value=True)
            submitted = st.form_submit_button("Add target")
        if submitted:
            target = Target(
                id=None,
                name=name,
                query=query,
                category_id=category_id or None,
                condition=condition or None,
                max_buy_gbp=max_buy_gbp or None,
                shipping_max_gbp=shipping_max_gbp or None,
                listing_type=listing_type,
                enabled=enabled,
            )
            from ebayflip.db import add_target

            add_target(DB_PATH, target)
            st.success("Target added. Refresh to see it in the table.")

    if targets:
        st.subheader("Edit or delete target")
        selected = st.selectbox("Select target", targets, format_func=lambda t: f"{t.id}: {t.name}")
        if selected:
            with st.form("edit_target_form"):
                name = st.text_input("Name", value=selected.name)
                query = st.text_input("Keywords", value=selected.query)
                if categories_ready:
                    category_id = _render_category_picker("edit", selected.category_id)
                else:
                    st.warning("Category list unavailable. Keeping existing category filter.")
                    category_id = selected.category_id
                condition = st.text_input("Condition", value=selected.condition or "")
                max_buy_gbp = st.number_input(
                    "Max buy (£)",
                    min_value=0.0,
                    value=float(selected.max_buy_gbp or 0.0),
                    step=1.0,
                )
                shipping_max_gbp = st.number_input(
                    "Max shipping (£)",
                    min_value=0.0,
                    value=float(selected.shipping_max_gbp or 0.0),
                    step=1.0,
                )
                listing_type_options = ["any", "auction", "bin"]
                listing_type_index = (
                    listing_type_options.index(selected.listing_type)
                    if selected.listing_type in listing_type_options
                    else 0
                )
                listing_type = st.selectbox(
                    "Listing type", listing_type_options, index=listing_type_index
                )
                enabled = st.toggle("Enabled", value=selected.enabled)
                updated = st.form_submit_button("Save changes")
            if updated:
                from ebayflip.db import update_target

                update_target(
                    DB_PATH,
                    Target(
                        id=selected.id,
                        name=name,
                        query=query,
                        category_id=category_id or None,
                        condition=condition or None,
                        max_buy_gbp=max_buy_gbp or None,
                        shipping_max_gbp=shipping_max_gbp or None,
                        listing_type=listing_type,
                        enabled=enabled,
                    ),
                )
                st.success("Target updated. Refresh to see changes.")
        if st.button("Delete selected target"):
            delete_target(DB_PATH, selected.id)
            st.warning("Target deleted. Refresh to update list.")

with Tabs[2]:
    st.subheader("Deals Feed")
    evaluations = list_evaluations_with_listings(DB_PATH)
    eval_df = pd.DataFrame(evaluations)
    if eval_df.empty:
        st.info("No evaluations yet.")
    else:
        targets = list_targets(DB_PATH)
        target_map = {t.id: t.name for t in targets}
        eval_df["target_name"] = eval_df["target_id"].map(target_map)
        eval_df["expected_profit_gbp"] = pd.to_numeric(eval_df["expected_profit_gbp"], errors="coerce")
        eval_df["roi"] = pd.to_numeric(eval_df["roi"], errors="coerce")
        eval_df["confidence"] = pd.to_numeric(eval_df["confidence"], errors="coerce")

        filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)
        with filter_col1:
            decision_filter = st.selectbox("Decision", ["deal", "maybe", "ignore"], index=0)
        with filter_col2:
            min_profit = st.number_input("Min profit (£)", value=MIN_PROFIT_GBP, step=1.0)
        with filter_col3:
            min_roi = st.number_input("Min ROI", value=MIN_ROI, step=0.05)
        with filter_col4:
            min_confidence = st.number_input("Min confidence", value=MIN_CONFIDENCE, step=0.05)

        filtered = eval_df[
            (eval_df["decision"] == decision_filter)
            & (eval_df["expected_profit_gbp"] >= min_profit)
            & (eval_df["roi"] >= min_roi)
            & (eval_df["confidence"] >= min_confidence)
        ].copy()
        if filtered.empty:
            st.info("No listings match the current filters.")
        else:
            display = filtered[[
                "title",
                "target_name",
                "expected_profit_gbp",
                "roi",
                "confidence",
                "deal_score",
                "url",
                "evaluated_at",
            ]].sort_values(by=["deal_score"], ascending=False)
            st.dataframe(display, use_container_width=True, height=320)

            selection = st.selectbox("View details", filtered["listing_id"].unique())
            if selection:
                listing_row = filtered[filtered["listing_id"] == selection].iloc[0]
                st.markdown(f"**{listing_row['title']}**")
                st.markdown(f"[Open listing]({listing_row['url']})")
                st.markdown("**Why flagged**")
                reasons = listing_row.get("reasons_json", "[]")
                try:
                    reasons_list = json.loads(reasons)
                except json.JSONDecodeError:
                    reasons_list = []
                for reason in reasons_list:
                    st.write(f"- {reason}")
                st.markdown("**Comps**")
                comps_rows = list_comps_by_listing(DB_PATH, int(selection))
                if comps_rows:
                    comps_df = pd.DataFrame([c.__dict__ for c in comps_rows])
                    st.dataframe(comps_df, use_container_width=True)
                else:
                    st.info("No comps stored.")

with Tabs[3]:
    st.subheader("Settings")
    settings = st.session_state.settings
    alert_settings = st.session_state.alerts

    col1, col2 = st.columns(2)
    with col1:
        settings.min_profit_gbp = st.number_input("Min profit (£)", value=settings.min_profit_gbp, step=1.0)
        settings.min_roi = st.number_input("Min ROI", value=settings.min_roi, step=0.05)
        settings.min_confidence = st.number_input("Min confidence", value=settings.min_confidence, step=0.05)
        settings.ebay_fee_pct = st.number_input("eBay fee %", value=settings.ebay_fee_pct, step=0.001, format="%.3f")
        settings.shipping_out_gbp = st.number_input(
            "Shipping out (£)", value=settings.shipping_out_gbp, step=0.5
        )
        settings.buffer_fixed_gbp = st.number_input("Buffer fixed (£)", value=settings.buffer_fixed_gbp, step=0.5)
        settings.buffer_pct_of_buy = st.number_input(
            "Buffer % of buy", value=settings.buffer_pct_of_buy, step=0.01, format="%.2f"
        )

    with col2:
        settings.request_cap = st.number_input("Request cap per scan", value=settings.request_cap, step=5)
        settings.comps_limit = st.number_input("Comps per listing", value=settings.comps_limit, step=5)
        settings.scan_limit_per_target = st.number_input(
            "Listings per target", value=settings.scan_limit_per_target, step=5
        )
        settings.allow_non_gbp = st.toggle("Allow non-GBP listings", value=settings.allow_non_gbp)
        settings.gbp_exchange_rate = st.number_input(
            "GBP exchange rate", value=settings.gbp_exchange_rate, step=0.01
        )

    st.markdown("### Alerts")
    alert_settings.discord_webhook_url = st.text_input(
        "Discord webhook URL",
        value=alert_settings.discord_webhook_url or "",
        type="password",
    )
    st.caption("Discord alerts trigger when a new DEAL is found.")

    st.markdown("### API Keys")
    st.code("EBAY_APP_ID=...\nDISCORD_WEBHOOK_URL=...")
