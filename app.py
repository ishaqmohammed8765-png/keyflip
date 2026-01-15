from __future__ import annotations

# IMPORTANT: set Playwright browser install dir BEFORE importing Playwright anywhere
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw-browsers")

import subprocess
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from keyflip.cache import PriceCache
from keyflip.config import RunConfig, trusted_buy_sources
from keyflip.core import build_watchlist, scan_watchlist
from keyflip.scheduler import (
    AlertThresholds,
    SchedulerConfig,
    build_watchlist_items,
    run_cycle,
)

# ============================================================
# Paths (relative to this app file)
# ============================================================
ROOT_DIR = Path(__file__).parent.resolve()
WATCHLIST_CSV = ROOT_DIR / "watchlist.csv"
SCANS_CSV = ROOT_DIR / "scans.csv"
PASSES_CSV = ROOT_DIR / "passes.csv"
DB_PATH = ROOT_DIR / "price_cache.sqlite"
SCHEDULER_DB = ROOT_DIR / "scheduler_state.sqlite"
SCHEDULER_TEMP_WATCHLIST = ROOT_DIR / ".scheduler_watchlist.csv"

DEFAULT_CACHE_FAIL_TTL = 1200  # seconds (20 minutes)

# ============================================================
# Minimal helpers
# ============================================================
def open_cache(db_path: Path) -> PriceCache:
    p = Path(db_path)
    attempts = (
        lambda: PriceCache(p),
        lambda: PriceCache(str(p)),
        lambda: PriceCache(path=p),
        lambda: PriceCache(path=str(p)),
        lambda: PriceCache(db_path=str(p)),
    )
    last_err: Optional[Exception] = None
    for ctor in attempts:
        try:
            return ctor()
        except TypeError as e:
            last_err = e
    raise TypeError("Unsupported PriceCache constructor signature.") from last_err


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


def file_bytes_or_none(path: Path) -> Optional[bytes]:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return path.read_bytes()
    except Exception:
        return None


def has_playwright_browsers() -> bool:
    try:
        d = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
        return d.exists() and any(d.iterdir())
    except Exception:
        return False


def _run_playwright_install() -> subprocess.CompletedProcess:
    return subprocess.run(
        ["playwright", "install", "chromium"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def ensure_playwright_chromium_installed(show_ui: bool) -> bool:
    if has_playwright_browsers():
        return True

    proc = _run_playwright_install()
    if show_ui:
        if proc.returncode == 0:
            st.success("Playwright Chromium installed.")
        else:
            st.warning("Playwright Chromium install failed in this environment.")
            with st.expander("Install logs (stdout/stderr)"):
                st.code(proc.stdout or "(no stdout)")
                st.code(proc.stderr or "(no stderr)")

    return has_playwright_browsers()


def delete_outputs() -> int:
    deleted = 0
    for p in (WATCHLIST_CSV, SCANS_CSV, PASSES_CSV):
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except Exception:
            pass
    return deleted


def make_config(
    *,
    max_buy: float,
    watchlist_target: int,
    scan_limit: int,
    refresh_buy_price: bool,
    allow_eur: bool,
    eur_to_gbp: float,
    avoid_recent_days: int,
    include_scan_fields: bool,
) -> RunConfig:
    # Sensible defaults (hidden from UI)
    raw = {
        "max_buy": float(max_buy),
        "target": int(watchlist_target),
        "verify_candidates": 300,
        "pages_per_source": 5,
        "verify_limit": 0,
        "safety_cap": 20,
        "avoid_recent_days": int(avoid_recent_days),
        "allow_eur": bool(allow_eur),
        "eur_to_gbp": float(eur_to_gbp),
        "item_budget": 55.0,
        "run_budget": 0.0,
    }
    if include_scan_fields:
        raw["scan_limit"] = int(scan_limit)
        raw["refresh_buy_price"] = bool(refresh_buy_price)

    try:
        return RunConfig.from_kwargs(**raw)
    except Exception as e:
        st.error(f"Failed to build RunConfig: {type(e).__name__}: {e}")
        st.caption("Keys passed to RunConfig.from_kwargs:")
        st.code(str(raw))
        raise


# ============================================================
# Streamlit UI (clean)
# ============================================================
st.set_page_config(page_title="Keyflip", layout="wide")

# cold-boot auto install (once per server session)
if "pw_bootstrap_done" not in st.session_state:
    st.session_state.pw_bootstrap_done = False

if not st.session_state.pw_bootstrap_done:
    # show install outcome once
    ensure_playwright_chromium_installed(show_ui=True)
    st.session_state.pw_bootstrap_done = True

if "is_running" not in st.session_state:
    st.session_state.is_running = False
if "continuous_scan_enabled" not in st.session_state:
    st.session_state.continuous_scan_enabled = False


# ---------- Header ----------
left, right = st.columns([2.2, 1])
with left:
    st.markdown("## Keyflip")
    st.caption("Build a watchlist from trusted stores and scan Eneba for profitable resale deals.")

with right:
    wl = read_csv_if_present(WATCHLIST_CSV)
    wl_n = 0 if wl is None else len(wl)
    chrome_ok = has_playwright_browsers()

    st.markdown("#### Status")
    st.write(f"**Watchlist:** {'✅' if wl_n > 0 else '—'} {wl_n} items")
    st.write(f"**Playwright Chromium:** {'✅ Installed' if chrome_ok else '⚠️ Missing'}")


st.divider()

# ---------- Sidebar (simple) ----------
with st.sidebar:
    st.markdown("### Settings")

    max_buy = st.slider("Max buy price (£)", min_value=1.0, max_value=200.0, value=15.0, step=0.5)
    watchlist_target = st.slider("Watchlist size", min_value=5, max_value=200, value=20, step=5)
    scan_limit = st.slider("Scan items per run", min_value=5, max_value=200, value=20, step=5)

    with st.expander("Advanced options"):
        refresh_buy_price = st.toggle("Refresh buy price (Playwright)", value=False)
        avoid_recent_days = st.slider("Avoid recently scanned (days)", min_value=0, max_value=30, value=0, step=1)

        allow_eur = st.toggle("Allow EUR listings", value=False)
        eur_to_gbp = st.number_input("EUR→GBP", min_value=0.1, max_value=2.0, value=0.86, step=0.01)

    with st.expander("Maintenance"):
        m1, m2 = st.columns(2)
        do_install_pw = m1.button("Install Chromium", use_container_width=True, disabled=st.session_state.is_running)
        do_clear_cache = m2.button("Clear cache", use_container_width=True, disabled=st.session_state.is_running)

        do_delete_outputs = st.button("Delete CSV outputs", use_container_width=True, disabled=st.session_state.is_running)

    with st.expander("Trusted buy sources"):
        trusted_sources = trusted_buy_sources()
        st.dataframe(
            pd.DataFrame(
                [{"Source": s.label, "Trust": s.trust_rating, "URL": s.url} for s in trusted_sources]
            ),
            use_container_width=True,
            height=240,
            hide_index=True,
        )

# ---------- Maintenance ----------
if do_install_pw:
    ensure_playwright_chromium_installed(show_ui=True)

if do_clear_cache:
    try:
        cache = open_cache(DB_PATH)
        cache.clear_all()
        st.success("Cache cleared.")
    except Exception as e:
        st.error(f"Failed to clear cache: {type(e).__name__}: {e}")

if do_delete_outputs:
    n = delete_outputs()
    st.success(f"Deleted {n} output file(s).")


# ---------- Main actions (nice layout) ----------
action = st.selectbox(
    "Action",
    ["Build watchlist", "Scan watchlist", "Build & scan"],
    index=0,
    disabled=st.session_state.is_running,
)
run_clicked = st.button("Run", use_container_width=True, disabled=st.session_state.is_running)

if wl_n == 0:
    st.info("Build a watchlist to enable scanning.")


def run_build() -> int:
    if not ensure_playwright_chromium_installed(show_ui=True):
        st.error("Chromium is required to build the watchlist. Install it in Maintenance and retry.")
        return 0
    cfg = make_config(
        max_buy=max_buy,
        watchlist_target=watchlist_target,
        scan_limit=scan_limit,
        refresh_buy_price=refresh_buy_price,
        allow_eur=allow_eur,
        eur_to_gbp=eur_to_gbp,
        avoid_recent_days=avoid_recent_days,
        include_scan_fields=False,
    )
    with st.spinner("Building watchlist from Fanatical..."):
        added = build_watchlist(cfg, WATCHLIST_CSV)

    if added is None:
        st.warning("Build finished but returned None (expected an int).")
        return 0

    added_i = int(added)
    if added_i > 0:
        st.success(f"Watchlist built: {added_i} item(s).")
    else:
        st.warning("Watchlist built, but no items were added.")
    return added_i


def run_scan() -> None:
    if refresh_buy_price and not has_playwright_browsers():
        st.error("Refresh buy price is enabled but Chromium is missing. Install Chromium or disable this option.")
        return

    wl_df = read_csv_if_present(WATCHLIST_CSV)
    wl_count = 0 if wl_df is None else len(wl_df)
    if wl_count == 0:
        st.warning("No watchlist items found. Build first.")
        return

    planned = min(wl_count, int(scan_limit))
    st.caption(f"Scanning {planned} / {wl_count} items (limit={int(scan_limit)})")

    cfg = make_config(
        max_buy=max_buy,
        watchlist_target=watchlist_target,
        scan_limit=scan_limit,
        refresh_buy_price=refresh_buy_price,
        allow_eur=allow_eur,
        eur_to_gbp=eur_to_gbp,
        avoid_recent_days=avoid_recent_days,
        include_scan_fields=True,
    )

    with st.spinner("Scanning watchlist on Eneba..."):
        scan_watchlist(cfg, WATCHLIST_CSV, SCANS_CSV, PASSES_CSV, DB_PATH, DEFAULT_CACHE_FAIL_TTL)

    st.success("Scan complete. Results saved to scans.csv (and passes.csv if any deals matched).")


# ---------- Execute actions ----------
try:
    if run_clicked:
        st.session_state.is_running = True

        if action == "Build watchlist":
            run_build()
        elif action == "Scan watchlist":
            run_scan()
        else:
            added = run_build()
            if added > 0:
                run_scan()
            else:
                st.warning("Build & scan stopped because the watchlist is empty.")

finally:
    st.session_state.is_running = False


# ---------- Continuous scan ----------
st.divider()
st.markdown("### Continuous Scan")

ctl1, ctl2, ctl3 = st.columns([1.2, 1.2, 1.6])
with ctl1:
    start_toggle = st.toggle(
        "Start scanning",
        value=st.session_state.continuous_scan_enabled,
        disabled=st.session_state.is_running,
        key="continuous_scan_start",
    )
with ctl2:
    stop_toggle = st.toggle(
        "Stop scanning",
        value=not st.session_state.continuous_scan_enabled,
        disabled=st.session_state.is_running,
        key="continuous_scan_stop",
    )
with ctl3:
    batch_size = st.number_input(
        "Batch size (items per cycle)",
        min_value=1,
        max_value=200,
        value=20,
        step=1,
        disabled=st.session_state.is_running,
    )

cycle_interval_min = st.number_input(
    "Cycle interval (minutes)",
    min_value=1,
    max_value=120,
    value=7,
    step=1,
    disabled=st.session_state.is_running,
)

with st.expander("Alert & scheduling options"):
    cooldown_min = st.number_input("Cooldown after success (min minutes)", min_value=5, max_value=180, value=30, step=5)
    cooldown_max = st.number_input("Cooldown after success (max minutes)", min_value=5, max_value=240, value=60, step=5)
    alert_cooldown_hours = st.number_input("Alert cooldown (hours)", min_value=1.0, max_value=48.0, value=6.0, step=1.0)
    alert_price_change_pct = st.number_input(
        "Alert again if sell price changes by (%)",
        min_value=0.5,
        max_value=50.0,
        value=3.0,
        step=0.5,
    )
    min_profit_gbp = st.number_input(
        "Min profit for alert (£)",
        min_value=0.0,
        max_value=50.0,
        value=0.5,
        step=0.1,
    )
    min_roi = st.number_input(
        "Min ROI for alert",
        min_value=0.0,
        max_value=2.0,
        value=0.2,
        step=0.05,
    )

if stop_toggle and st.session_state.continuous_scan_enabled:
    st.session_state.continuous_scan_enabled = False
elif start_toggle:
    st.session_state.continuous_scan_enabled = True

if st.session_state.continuous_scan_enabled:
    if refresh_buy_price and not has_playwright_browsers():
        st.error("Continuous scan paused: Chromium is missing while refresh buy price is enabled.")
    else:
        wl_df = read_csv_if_present(WATCHLIST_CSV)
        items = build_watchlist_items(wl_df) if wl_df is not None else []
        if not items:
            st.warning("No watchlist items found. Build the watchlist before starting continuous scan.")
        else:
            scheduler_config = SchedulerConfig(
                state_db_path=SCHEDULER_DB,
                watchlist_path=WATCHLIST_CSV,
                scans_path=SCANS_CSV,
                passes_path=PASSES_CSV,
                price_cache_path=DB_PATH,
                temp_watchlist_path=SCHEDULER_TEMP_WATCHLIST,
                batch_size=int(batch_size),
                cooldown_min_minutes=int(cooldown_min),
                cooldown_max_minutes=int(cooldown_max),
            )
            thresholds = AlertThresholds(
                min_profit_gbp=float(min_profit_gbp),
                min_roi=float(min_roi),
                cooldown_hours=float(alert_cooldown_hours),
                price_change_pct=float(alert_price_change_pct),
            )
            cfg = make_config(
                max_buy=max_buy,
                watchlist_target=watchlist_target,
                scan_limit=scan_limit,
                refresh_buy_price=refresh_buy_price,
                allow_eur=allow_eur,
                eur_to_gbp=eur_to_gbp,
                avoid_recent_days=avoid_recent_days,
                include_scan_fields=True,
            )

            st.session_state.is_running = True
            try:
                with st.spinner("Running one continuous scan cycle..."):
                    report = run_cycle(scheduler_config, items, cfg, thresholds)
            finally:
                st.session_state.is_running = False

            st.caption(
                f"Eligible now: {report.eligible_count} • "
                f"Scanned this cycle: {len(report.scanned_titles)}"
            )
            if report.scanned_titles:
                st.write("Scanned this cycle:")
                st.dataframe(pd.DataFrame({"title": report.scanned_titles}), use_container_width=True, height=200)

            if report.failures:
                st.warning("Failures this cycle:")
                st.dataframe(pd.DataFrame(report.failures), use_container_width=True, height=200)

            if report.next_eligible_in:
                mins = int(report.next_eligible_in.total_seconds() / 60)
                st.info(f"Next eligible item in ~{mins} min.")
            else:
                st.info("Next eligible item: now (waiting for next cycle).")

            if report.alerts:
                for alert in report.alerts:
                    st.error(
                        f"🚨 Deal alert: {alert['title']} — "
                        f"profit £{alert['profit_gbp']:.2f}, ROI {alert['roi']:.2f}"
                    )
                    st.write(f"Buy: {alert.get('buy_url') or 'n/a'}")
                    st.write(f"Sell: {alert.get('sell_url') or 'n/a'}")
            else:
                st.success("No new alerts triggered in this cycle.")

        st_autorefresh(interval=int(cycle_interval_min * 60 * 1000), key="continuous_scan_refresh")

# ---------- Results ----------
st.divider()

watch_df = read_csv_if_present(WATCHLIST_CSV)
scans_df = read_csv_if_present(SCANS_CSV)
passes_df = read_csv_if_present(PASSES_CSV)

tab1, tab2, tab3, tab4 = st.tabs(["📋 Watchlist", "✅ Good deals", "📈 Latest scans", "⬇️ Downloads"])

with tab1:
    if watch_df is None or watch_df.empty:
        st.info("No watchlist yet.")
    else:
        st.dataframe(watch_df, use_container_width=True, height=520)

with tab2:
    if passes_df is None or passes_df.empty:
        st.info("No good deals found yet.")
        st.markdown(
            "- **Raise scan volume:** increase **Watchlist size** or **Scan items per run** in the sidebar.\n"
            "- **Loosen filters:** raise **Max buy price** or toggle **Allow EUR listings**.\n"
            "- **Refresh buy price only if needed:** enabling it can change margins but is slower.\n"
            "- **Avoid repeats:** set **Avoid recently scanned (days)** to 0 when hunting fresh finds."
        )
        if scans_df is not None and not scans_df.empty:
            candidates = scans_df.copy()
            for col in ["edge", "edge_pct", "buy_price", "market_price"]:
                if col in candidates.columns:
                    candidates[col] = pd.to_numeric(candidates[col], errors="coerce")
            candidates = candidates.dropna(subset=["edge"]) if "edge" in candidates.columns else candidates
            if not candidates.empty:
                top = candidates.sort_values(by="edge", ascending=False).head(10)
                st.caption("Best recent candidates (highest edge), even if they don't pass thresholds yet.")
                st.dataframe(
                    top[
                        [
                            c
                            for c in [
                                "title",
                                "buy_price",
                                "market_price",
                                "edge",
                                "edge_pct",
                                "buy_site",
                                "market_url",
                            ]
                            if c in top.columns
                        ]
                    ],
                    use_container_width=True,
                    height=320,
                )
    else:
        st.dataframe(passes_df, use_container_width=True, height=520)

with tab3:
    if scans_df is None or scans_df.empty:
        st.info("No scan results yet.")
    else:
        recent = scans_df.tail(500).copy()
        meta = []
        if "timestamp" in recent.columns:
            ts = pd.to_datetime(recent["timestamp"], errors="coerce")
            if not ts.isna().all():
                meta.append(f"Latest: `{ts.max()}`")
        if "batch_id" in recent.columns and recent["batch_id"].astype(str).str.len().gt(0).any():
            meta.append(f"Batch: `{str(recent.iloc[-1]['batch_id'])}`")
        if meta:
            st.caption(" • ".join(meta))
        st.dataframe(recent, use_container_width=True, height=520)

with tab4:
    b = file_bytes_or_none(WATCHLIST_CSV)
    if b is not None:
        st.download_button("Download watchlist.csv", data=b, file_name="watchlist.csv", use_container_width=True)

    b = file_bytes_or_none(SCANS_CSV)
    if b is not None:
        st.download_button("Download scans.csv", data=b, file_name="scans.csv", use_container_width=True)

    b = file_bytes_or_none(PASSES_CSV)
    if b is not None:
        st.download_button("Download passes.csv", data=b, file_name="passes.csv", use_container_width=True)

    if (
        file_bytes_or_none(WATCHLIST_CSV) is None
        and file_bytes_or_none(SCANS_CSV) is None
        and file_bytes_or_none(PASSES_CSV) is None
    ):
        st.info("Nothing to download yet.")

st.caption(
    "Tip: Keep 'Refresh buy price (Playwright)' OFF unless you need it. "
    "It’s slower and requires Chromium to be installed."
)
