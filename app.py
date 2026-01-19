from __future__ import annotations

import dataclasses
import json
import os
import re
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
if "last_scan_listings" not in st.session_state:
    st.session_state.last_scan_listings = []
if "last_scan_debug" not in st.session_state:
    st.session_state.last_scan_debug = []
if "last_scan_summary" not in st.session_state:
    st.session_state.last_scan_summary = None
if "last_scan_error" not in st.session_state:
    st.session_state.last_scan_error = None
if "last_scan_error_at" not in st.session_state:
    st.session_state.last_scan_error_at = None

if "settings" not in st.session_state:
    st.session_state.settings = RunSettings()
if "alerts" not in st.session_state:
    st.session_state.alerts = AlertSettings(discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL"))


def _coerce_settings(value: object) -> RunSettings:
    defaults = RunSettings()
    fields = dataclasses.fields(RunSettings)
    if isinstance(value, RunSettings):
        data = {
            field.name: getattr(value, field.name, getattr(defaults, field.name))
            for field in fields
        }
        return RunSettings(**data)
    if isinstance(value, dict):
        data = {field.name: value.get(field.name, getattr(defaults, field.name)) for field in fields}
        return RunSettings(**data)
    return defaults


st.session_state.settings = _coerce_settings(st.session_state.settings)

init_db(DB_PATH)

st.title("eBay Flip Scanner")
st.caption("Scan eBay listings for underpriced flips, estimate resale, and alert on deals.")

CONDITION_OPTIONS = {
    "Any": None,
    "New": "1000",
    "Open box": "1500",
    "Used": "3000",
    "For parts or not working": "7000",
}


@st.cache_data(show_spinner=False)
def _load_targets(db_path: str) -> list[Target]:
    return list_targets(db_path)


@st.cache_data(show_spinner=False)
def _load_evaluations(db_path: str) -> list[dict]:
    return list_evaluations_with_listings(db_path)


@st.cache_data(show_spinner=False)
def _load_comps(db_path: str, listing_id: int) -> list:
    return list_comps_by_listing(db_path, listing_id)


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


def _format_condition(condition_id: Optional[str]) -> str:
    if not condition_id:
        return "-"
    for label, value in CONDITION_OPTIONS.items():
        if value == condition_id:
            return label
    return condition_id


def _format_listing_type(listing_type: Optional[str]) -> str:
    if not listing_type or listing_type == "any":
        return "-"
    return listing_type


def _describe_active_filters(last_diag: Optional[dict]) -> list[str]:
    if not last_diag:
        return []
    filters: list[str] = []
    category_id = last_diag.get("category_id")
    if category_id:
        filters.append(f"Category: {_format_category_path(category_id)}")
    condition = last_diag.get("condition")
    if condition:
        filters.append(f"Condition: {_format_condition(condition)}")
    listing_type = _format_listing_type(last_diag.get("listing_type"))
    if listing_type != "-":
        filters.append(f"Listing type: {listing_type}")
    price_filters = last_diag.get("price_filters") or {}
    max_buy = price_filters.get("max_buy_gbp")
    if max_buy is not None:
        filters.append(f"Max buy: £{max_buy:.2f}")
    shipping_max = price_filters.get("shipping_max_gbp")
    if shipping_max is not None:
        filters.append(f"Max shipping: £{shipping_max:.2f}")
    total_max = price_filters.get("total_max_gbp")
    if total_max is not None:
        filters.append(f"Total max: £{total_max:.2f}")
    return filters


def _classify_scan_issue(entry: dict, last_diag: Optional[dict]) -> tuple[str, str]:
    if entry.get("retry_report"):
        for report in entry["retry_report"]:
            if report.startswith("skipped:"):
                return "Skipped target", "Add keywords or enable the target to scan."
    raw_count = entry.get("raw_count", 0)
    filtered_count = entry.get("filtered_count", 0)
    if raw_count == 0:
        if last_diag and last_diag.get("failure_mode"):
            mode = last_diag["failure_mode"]
            return f"Blocked ({mode})", "Retry later, slow scans, or use an API key."
        if last_diag and last_diag.get("item_count") and (last_diag.get("parsed_count") or 0) == 0:
            return "Parser mismatch", "eBay layout changed; update parsing or use API mode."
        return "No results", "Broaden keywords or remove restrictive filters."
    if filtered_count == 0:
        return "Filtered out", "Relax filters or adjust pricing/condition limits."
    return "Partial results", "Review filters if listings are missing."


def _build_diagnostic_rows(scan_debug: list) -> list[dict]:
    rows: list[dict] = []
    for entry in scan_debug:
        entry_data = dataclasses.asdict(entry)
        last_diag = entry_data["diagnostics"][-1] if entry_data.get("diagnostics") else None
        status, action = _classify_scan_issue(entry_data, last_diag)
        filters = _describe_active_filters(last_diag)
        rejections = entry_data.get("rejection_counts") or {}
        rejection_items = [(key, value) for key, value in rejections.items() if value]
        top_rejection = "-"
        if rejection_items:
            reason, count = max(rejection_items, key=lambda item: item[1])
            top_rejection = f"{reason} ({count})"
        rows.append(
            {
                "target": entry_data["target_name"],
                "query": entry_data["target_query"],
                "mode": last_diag.get("mode") if last_diag else "-",
                "status": status,
                "action": action,
                "raw": entry_data.get("raw_count", 0),
                "filtered": entry_data.get("filtered_count", 0),
                "filters": ", ".join(filters) if filters else "-",
                "top_rejection": top_rejection,
                "failure_mode": last_diag.get("failure_mode") if last_diag else "-",
                "http_status": last_diag.get("http_status") if last_diag else "-",
            }
        )
    return rows


def _build_auto_keywords(name: str, category_id: Optional[str]) -> str:
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def broaden(value: str) -> str:
        if not value:
            return value
        cleaned = re.sub(r'(["\'])(.*?)\1', r"\2", value)
        cleaned = re.sub(r"(?<=\D)(?=\d)|(?<=\d)(?=\D)", " ", cleaned)
        cleaned = re.sub(r"\b\d+\s?(gb|tb)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b\d+\s?(gig|gigabyte|terabyte)s?\b", "", cleaned, flags=re.IGNORECASE)
        colors = (
            "black",
            "white",
            "silver",
            "gray",
            "grey",
            "blue",
            "red",
            "green",
            "graphite",
            "gold",
            "pink",
            "purple",
            "midnight",
            "starlight",
        )
        pattern = r"\b(" + "|".join(colors) + r")\b"
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        return normalize(cleaned)

    def add_variant(value: str) -> None:
        normalized = normalize(value)
        if normalized and normalized not in variants:
            variants.append(normalized)

    base = normalize(name or "")
    if not base:
        return ""

    stop_words = {
        "and",
        "or",
        "with",
        "for",
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "by",
        "to",
    }
    variants: list[str] = []
    add_variant(base)

    widened = broaden(base)
    if widened and widened != base:
        add_variant(widened)

    tokens = [token for token in re.split(r"[^\w]+", base) if token]
    core_tokens = [token for token in tokens if token.lower() not in stop_words]
    if len(core_tokens) >= 2:
        add_variant(" ".join(core_tokens))
        add_variant(" ".join(core_tokens[:2]))
        add_variant(" ".join(core_tokens[-2:]))

    if len(variants) == 1:
        return variants[0]

    def format_variant(value: str) -> str:
        if " " in value:
            return f'"{value}"'
        return value

    return " OR ".join(format_variant(value) for value in variants)


def _normalize_query(query: str, name: str) -> str:
    cleaned_query = (query or "").strip()
    if cleaned_query:
        return cleaned_query
    return (name or "").strip()


def _maybe_autofill_keywords(prefix: str, name: str, category_id: Optional[str]) -> None:
    auto_value = _build_auto_keywords(name, category_id)
    if not auto_value:
        return
    query_key = f"{prefix}_query"
    auto_key = f"{prefix}_query_autofill"
    current = st.session_state.get(query_key, "")
    previous_auto = st.session_state.get(auto_key, "")
    if not current or current == previous_auto:
        st.session_state[query_key] = auto_value
        st.session_state[auto_key] = auto_value


def _condition_selectbox(label: str, key: str, selected_condition_id: Optional[str]) -> Optional[str]:
    options = list(CONDITION_OPTIONS.keys())
    condition_map = {value: name for name, value in CONDITION_OPTIONS.items()}
    selected_name = condition_map.get(selected_condition_id, "Any")
    selected_index = options.index(selected_name) if selected_name in options else 0
    choice = st.selectbox(label, options, index=selected_index, key=key)
    return CONDITION_OPTIONS.get(choice)


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

def _run_scan_with_feedback() -> None:
    config = build_config()
    client = build_client()
    try:
        with st.spinner("Scanning eBay..."):
            summary = run_scan(config, client)
    except Exception as exc:
        LOGGER.exception("Scan failed: %s", exc)
        st.error("Scan failed. Check logs or try again in a moment.")
        st.session_state.last_scan_error = str(exc)
        st.session_state.last_scan_error_at = datetime.utcnow().isoformat()
        return
    st.session_state.last_scan = summary.last_scan
    st.session_state.last_scan_listings = summary.scanned_listings
    st.session_state.last_scan_debug = summary.zero_result_debug
    st.session_state.last_scan_summary = {
        "scanned_targets": summary.scanned_targets,
        "new_listings": summary.new_listings,
        "evaluated": summary.evaluated,
        "deals": summary.deals,
        "request_cap_reached": summary.request_cap_reached,
    }
    st.session_state.last_scan_error = None
    st.session_state.last_scan_error_at = None
    _load_evaluations.clear()
    _load_comps.clear()
    st.success(
        f"Scan complete: {summary.scanned_targets} targets, {summary.evaluated} listings evaluated, {summary.deals} deals."
    )
    if summary.scanned_targets == 0:
        st.warning("No enabled targets found. Add or enable a target to scan.")
    elif summary.evaluated == 0:
        if summary.request_cap_reached:
            st.warning("Request cap reached before listings were returned. Increase the cap or try again later.")
        elif summary.zero_result_debug:
            st.warning("No results from eBay for one or more targets. See the “Why no results?” panel for details.")
        else:
            st.warning("No results from eBay for this scan. Try broader keywords or remove filters and retry.")


if run_scan_now:
    _run_scan_with_feedback()

if st.session_state.auto_scan:
    interval_s = int(st.session_state.auto_scan_interval * 60)
    st_autorefresh(interval=interval_s * 1000, key="auto_scan_refresh")
    if st.session_state.last_scan:
        last_scan_time = datetime.fromisoformat(st.session_state.last_scan)
        elapsed = (datetime.utcnow() - last_scan_time).total_seconds()
        if elapsed >= interval_s:
            _run_scan_with_feedback()
    else:
        st.info("Auto-scan enabled. A scan will run on the next refresh.")


Tabs = st.tabs(["Dashboard", "Targets", "Deals Feed", "Scan Misses", "Settings"])

with Tabs[0]:
    targets = _load_targets(DB_PATH)
    evaluations = _load_evaluations(DB_PATH)
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

    st.subheader("Last Scan Listings")
    scan_listings = st.session_state.get("last_scan_listings", [])
    if scan_listings:
        scan_df = pd.DataFrame([dataclasses.asdict(listing) for listing in scan_listings])
        if "decision" not in scan_df.columns:
            scan_df["decision"] = "-"
        display_scan = scan_df[
            ["title", "target_name", "decision", "total_buy_gbp", "condition", "url"]
        ].rename(
            columns={
                "target_name": "target",
                "total_buy_gbp": "total_buy_gbp",
            }
        )
        st.dataframe(display_scan, use_container_width=True, height=260)
    else:
        st.info("Run a scan to see which listings were evaluated.")

    st.subheader("Why no results?")
    scan_debug = st.session_state.get("last_scan_debug", [])
    if scan_debug:
        for entry in scan_debug:
            entry_data = dataclasses.asdict(entry)
            last_diag = entry_data["diagnostics"][-1] if entry_data.get("diagnostics") else None
            with st.expander(f"{entry_data['target_name']} — {entry_data['target_query']}"):
                st.markdown("**Outcome**")
                st.write(
                    f"Raw results: {entry_data.get('raw_count', 0)} → After filters: {entry_data.get('filtered_count', 0)}"
                )
                if entry_data["raw_count"] == 0:
                    st.write("0 results from eBay.")
                    if last_diag and last_diag.get("item_count"):
                        parsed_count = last_diag.get("parsed_count") or 0
                        if parsed_count == 0 and last_diag.get("mode") == "html":
                            st.caption(
                                f"Items detected in response: {last_diag['item_count']} (none parsed). "
                                "The eBay page layout may have changed."
                            )
                else:
                    st.write(
                        f"Results were filtered out locally (raw {entry_data['raw_count']}, kept {entry_data['filtered_count']})."
                    )
                active_filters = _describe_active_filters(last_diag)
                if active_filters:
                    st.markdown("**Filters applied**")
                    for active_filter in active_filters:
                        st.write(f"- {active_filter}")
                else:
                    st.write("Filters applied: -")
                if entry_data.get("retry_report"):
                    st.markdown("**Retries applied:**")
                    retry_steps = "\n".join(f"- {step}" for step in entry_data["retry_report"])
                    st.markdown(retry_steps)
                if entry_data.get("rejection_counts"):
                    reasons = {
                        key: value
                        for key, value in entry_data["rejection_counts"].items()
                        if value
                    }
                    if reasons:
                        st.markdown("**Local filter rejections:**")
                        rejection_df = pd.DataFrame(
                            [
                                {"reason": reason, "count": count}
                                for reason, count in sorted(reasons.items(), key=lambda item: item[1], reverse=True)
                            ]
                        )
                        st.dataframe(rejection_df, use_container_width=True, height=160)
                if last_diag:
                    st.markdown("**Last request details:**")
                    st.write(f"Mode: {last_diag['mode']}")
                    st.write(f"Query: {last_diag['query']}")
                    st.write(f"Category: {last_diag['category_id'] or '-'}")
                    st.write(f"Condition: {last_diag['condition'] or '-'}")
                    st.write(f"Listing type: {_format_listing_type(last_diag.get('listing_type'))}")
                    st.write(f"Page: {last_diag['pagination']['page']}")
                    st.write(f"Limit: {last_diag['pagination']['limit']}")
                    st.write(f"HTTP status: {last_diag['http_status']}")
                    if last_diag.get("failure_mode"):
                        st.warning(f"Failure mode: {last_diag['failure_mode']}")
                    if last_diag.get("response_length") is not None:
                        st.write(f"Response length: {last_diag['response_length']}")
                    if last_diag.get("item_count") is not None:
                        st.write(f"Items detected in response: {last_diag['item_count']}")
                    if last_diag.get("parsed_count") is not None:
                        st.write(f"Parsed listings: {last_diag['parsed_count']}")
                    active_price_filters = {
                        key: value
                        for key, value in last_diag["price_filters"].items()
                        if value is not None
                    }
                    if active_price_filters:
                        st.markdown("**Price filters:**")
                        st.json(active_price_filters)
                    else:
                        st.write("Price filters: -")
                    if entry_data.get("last_request_url"):
                        st.code(entry_data["last_request_url"])
    else:
        st.caption("No zero-result scans in the most recent run.")

    st.subheader("Advanced Diagnostics")
    last_error = st.session_state.get("last_scan_error")
    if last_error:
        st.error("Last scan failed to complete.")
        st.write(f"Error: {last_error}")
        if st.session_state.get("last_scan_error_at"):
            st.write(f"Time: {st.session_state['last_scan_error_at']}")

    summary = st.session_state.get("last_scan_summary")
    if summary:
        diag_col1, diag_col2, diag_col3, diag_col4, diag_col5 = st.columns(5)
        diag_col1.metric("Targets scanned", summary.get("scanned_targets", 0))
        diag_col2.metric("New listings", summary.get("new_listings", 0))
        diag_col3.metric("Evaluated", summary.get("evaluated", 0))
        diag_col4.metric("Deals", summary.get("deals", 0))
        diag_col5.metric(
            "Request cap hit",
            "Yes" if summary.get("request_cap_reached") else "No",
        )
        if summary.get("scanned_targets", 0) == 0:
            st.warning("No enabled targets were scanned. Add or enable targets in the Targets tab.")
        if summary.get("request_cap_reached"):
            st.warning("Request cap reached. Increase it in Settings or reduce scan frequency.")
    else:
        st.info("Run a scan to populate advanced diagnostics.")

    diagnostic_rows = _build_diagnostic_rows(scan_debug)
    if diagnostic_rows:
        st.markdown("**Target diagnostics summary**")
        st.dataframe(pd.DataFrame(diagnostic_rows), use_container_width=True, height=240)
        recommended_actions = sorted({row["action"] for row in diagnostic_rows if row.get("action")})
        if recommended_actions:
            st.markdown("**Suggested fixes**")
            for action in recommended_actions:
                st.write(f"- {action}")
    else:
        st.caption("No target-level diagnostics to display yet.")

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
    targets = _load_targets(DB_PATH)
    categories_ready = ensure_categories_loaded(DB_PATH)
    target_df = pd.DataFrame([dataclasses.asdict(t) for t in targets]) if targets else pd.DataFrame()
    if target_df.empty:
        st.info("No targets yet. Add one below.")
    else:
        if categories_ready:
            target_df["category"] = target_df["category_id"].apply(_format_category_path)
        else:
            target_df["category"] = target_df["category_id"].fillna("-")
        target_df["condition_display"] = target_df["condition"].apply(_format_condition)
        st.dataframe(
            target_df[
                [
                    "id",
                    "name",
                    "query",
                    "category",
                    "condition_display",
                    "listing_type",
                    "country",
                    "enabled",
                ]
            ].rename(columns={"condition_display": "condition"}),
            use_container_width=True,
            height=260,
        )

    with st.expander("Add target", expanded=True):
        name = st.text_input("Name", key="add_name")
        if categories_ready:
            category_id = _render_category_picker("add", None)
        else:
            st.warning("Category list unavailable. Category filters are disabled for now.")
            category_id = None
        st.session_state["add_category_id"] = category_id
        _maybe_autofill_keywords("add", st.session_state.get("add_name", ""), category_id)
        query = st.text_input("Keywords", key="add_query")
        query_value = _normalize_query(query, name)
        condition = _condition_selectbox("Condition", key="add_condition", selected_condition_id=None)
        listing_type = st.selectbox("Listing type", ["any", "auction", "bin"], key="add_listing_type")
        enabled = st.toggle("Enabled", value=True, key="add_enabled")
        submitted = st.button("Add target", use_container_width=True)
        if submitted:
            target = Target(
                id=None,
                name=name,
                query=query_value,
                category_id=category_id or None,
                condition=condition,
                max_buy_gbp=None,
                shipping_max_gbp=None,
                listing_type=listing_type,
                enabled=enabled,
            )
            from ebayflip.db import add_target

            add_target(DB_PATH, target)
            _load_targets.clear()
            st.success("Target added. Refresh to see it in the table.")

    if targets:
        st.subheader("Edit or delete target")
        selected = st.selectbox("Select target", targets, format_func=lambda t: f"{t.id}: {t.name}")
        if selected:
            if st.session_state.get("edit_target_id") != selected.id:
                st.session_state["edit_target_id"] = selected.id
                st.session_state["edit_name"] = selected.name
                st.session_state["edit_query"] = selected.query
                st.session_state["edit_query_autofill"] = ""
                st.session_state["edit_condition"] = selected.condition
                st.session_state["edit_listing_type"] = selected.listing_type
                st.session_state["edit_enabled"] = selected.enabled
                st.session_state["edit_category_id"] = selected.category_id

            name = st.text_input("Name", key="edit_name")
            if categories_ready:
                category_id = _render_category_picker("edit", selected.category_id)
            else:
                st.warning("Category list unavailable. Keeping existing category filter.")
                category_id = selected.category_id
            st.session_state["edit_category_id"] = category_id
            _maybe_autofill_keywords("edit", st.session_state.get("edit_name", ""), category_id)
            query = st.text_input("Keywords", key="edit_query")
            query_value = _normalize_query(query, name)
            condition = _condition_selectbox(
                "Condition",
                key="edit_condition",
                selected_condition_id=st.session_state.get("edit_condition"),
            )
            listing_type_options = ["any", "auction", "bin"]
            listing_type_index = (
                listing_type_options.index(st.session_state.get("edit_listing_type", selected.listing_type))
                if st.session_state.get("edit_listing_type", selected.listing_type) in listing_type_options
                else 0
            )
            listing_type = st.selectbox(
                "Listing type", listing_type_options, index=listing_type_index, key="edit_listing_type"
            )
            enabled = st.toggle("Enabled", key="edit_enabled")
            updated = st.button("Save changes", use_container_width=True)
            if updated:
                from ebayflip.db import update_target

                update_target(
                    DB_PATH,
                    Target(
                        id=selected.id,
                        name=name,
                        query=query_value,
                        category_id=category_id or None,
                        condition=condition or None,
                        max_buy_gbp=None,
                        shipping_max_gbp=None,
                        listing_type=listing_type,
                        enabled=enabled,
                    ),
                )
                _load_targets.clear()
                st.success("Target updated. Refresh to see changes.")
        if st.button("Delete selected target"):
            delete_target(DB_PATH, selected.id)
            _load_targets.clear()
            st.warning("Target deleted. Refresh to update list.")

with Tabs[2]:
    st.subheader("Deals Feed")
    evaluations = _load_evaluations(DB_PATH)
    eval_df = pd.DataFrame(evaluations)
    if eval_df.empty:
        st.info("No evaluations yet.")
    else:
        targets = _load_targets(DB_PATH)
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
                comps_rows = _load_comps(DB_PATH, int(selection))
                if comps_rows:
                    comps_df = pd.DataFrame([c.__dict__ for c in comps_rows])
                    st.dataframe(comps_df, use_container_width=True)
                else:
                    st.info("No comps stored.")

with Tabs[3]:
    st.subheader("Scan Misses")
    scan_listings = st.session_state.get("last_scan_listings", [])
    if scan_listings:
        scan_df = pd.DataFrame([dataclasses.asdict(listing) for listing in scan_listings])
        if "decision" not in scan_df.columns:
            scan_df["decision"] = "-"
        misses = scan_df[scan_df["decision"] != "deal"].copy()
        if misses.empty:
            st.info("All scanned listings were marked as deals.")
        else:
            display_misses = misses[
                ["title", "target_name", "decision", "total_buy_gbp", "condition", "url"]
            ].rename(
                columns={
                    "target_name": "target",
                    "total_buy_gbp": "total_buy_gbp",
                }
            )
            st.dataframe(display_misses, use_container_width=True, height=320)
    else:
        st.info("Run a scan to see which listings were evaluated.")

with Tabs[4]:
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
        settings.comps_ttl_hours = st.number_input(
            "Comps refresh (hours)", value=settings.comps_ttl_hours, min_value=1, step=1
        )
        settings.allow_non_gbp = st.toggle("Allow non-GBP listings", value=settings.allow_non_gbp)
        settings.gbp_exchange_rate = st.number_input(
            "GBP exchange rate", value=settings.gbp_exchange_rate, step=0.01
        )
        settings.allow_missing_shipping_price = st.toggle(
            "Allow missing shipping prices",
            value=settings.allow_missing_shipping_price,
        )
        settings.assumed_inbound_shipping_gbp = st.number_input(
            "Assumed inbound shipping (£)",
            value=settings.assumed_inbound_shipping_gbp,
            step=0.5,
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
