from __future__ import annotations

# IMPORTANT: set Playwright browser install dir BEFORE importing Playwright anywhere
import os

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw-browsers")

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import streamlit as st

from keyflip.cache import PriceCache
from keyflip.config import RunConfig
from keyflip.core import build_watchlist, scan_watchlist

# ============================================================
# Paths (relative to this app file)
# ============================================================
ROOT_DIR = Path(__file__).parent.resolve()
WATCHLIST_CSV = ROOT_DIR / "watchlist.csv"
SCANS_CSV = ROOT_DIR / "scans.csv"
PASSES_CSV = ROOT_DIR / "passes.csv"
DB_PATH = ROOT_DIR / "price_cache.sqlite"

DEFAULT_CACHE_FAIL_TTL = 1200  # seconds (20 minutes)


# ============================================================
# Utilities
# ============================================================
def _open_cache(db_path: Path) -> PriceCache:
    """
    Support multiple PriceCache constructor signatures without changing cache.py.
    """
    db_path = Path(db_path)
    for ctor in (
        lambda: PriceCache(db_path),
        lambda: PriceCache(path=db_path),
        lambda: PriceCache(str(db_path)),
        lambda: PriceCache(path=str(db_path)),
        lambda: PriceCache(db_path=str(db_path)),
    ):
        try:
            return ctor()
        except TypeError:
            continue
    raise TypeError(
        "Unsupported PriceCache constructor. Tried: PriceCache(path/str), PriceCache(path=...), PriceCache(db_path=...)."
    )


def safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
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


def safe_read_bytes(path: Path) -> Optional[bytes]:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return path.read_bytes()
    except Exception:
        return None


def latest_batch_from_scans(df: pd.DataFrame) -> Tuple[Optional[str], Optional[pd.Timestamp], pd.DataFrame]:
    """
    Show the most recent scan run.
    Prefer grouping by batch_id (written by core.scan_watchlist).
    """
    if df.empty:
        return None, None, df

    if "batch_id" in df.columns and df["batch_id"].astype(str).str.len().gt(0).any():
        last_batch = str(df.iloc[-1]["batch_id"])
        batch_df = df[df["batch_id"].astype(str) == last_batch].copy()

        ts_val: Optional[pd.Timestamp] = None
        if "timestamp" in batch_df.columns:
            ts = pd.to_datetime(batch_df["timestamp"], errors="coerce")
            if not ts.isna().all():
                ts_val = ts.max()

        return last_batch, ts_val, batch_df.head(500)

    ts_val = None
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        if not ts.isna().all():
            ts_val = ts.max()
    return None, ts_val, df.tail(200)


def watchlist_is_ready() -> bool:
    """
    True if watchlist.csv exists and contains at least 1 data row.
    """
    df = safe_read_csv(WATCHLIST_CSV)
    return df is not None and len(df) > 0


def ensure_playwright_chromium_installed() -> bool:
    """
    Best-effort runtime install. Prefer installing browsers during deploy.
    Returns True if browsers appear present after this function.
    """
    browsers_dir = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    try:
        if browsers_dir.exists() and any(browsers_dir.iterdir()):
            return True
    except Exception:
        pass

    with st.spinner("Installing Playwright Chromium browser (first-time setup)..."):
        try:
            subprocess.run(
                ["playwright", "install", "chromium"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            st.success("Chromium installed successfully.")
        except Exception as e:
            st.warning(
                "Automatic Chromium install did not succeed in this environment.\n\n"
                "If scans fail on Streamlit Cloud, install Playwright browsers during deploy.\n\n"
                f"Details: {type(e).__name__}: {e}"
            )

    try:
        return browsers_dir.exists() and any(browsers_dir.iterdir())
    except Exception:
        return False


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


def render_paths() -> None:
    st.caption("Files are saved next to this app file:")
    st.code(
        "\n".join(
            [
                f"watchlist.csv: {WATCHLIST_CSV}",
                f"scans.csv:     {SCANS_CSV}",
                f"passes.csv:    {PASSES_CSV}",
                f"cache db:      {DB_PATH}",
            ]
        )
    )


def make_config(
    *,
    max_buy: float,
    watchlist_target: int,
    verify_candidates: int,
    pages_per_source: int,
    verify_limit: int,
    safety_cap: int,
    avoid_recent_days: int,
    allow_eur: bool,
    eur_to_gbp: float,
    item_budget: float,
    run_budget: float,
    include_scan_fields: bool,
    scan_limit: int,
    refresh_buy_price: bool,
) -> RunConfig:
    raw = dict(
        max_buy=float(max_buy),
        target=int(watchlist_target),
        verify_candidates=int(verify_candidates),
        pages_per_source=int(pages_per_source),
        verify_limit=int(verify_limit),
        safety_cap=int(safety_cap),
        avoid_recent_days=int(avoid_recent_days),
        allow_eur=bool(allow_eur),
        eur_to_gbp=float(eur_to_gbp),
        item_budget=float(item_budget),
        run_budget=float(run_budget),
    )
    if include_scan_fields:
        raw["scan_limit"] = int(scan_limit)
        raw["refresh_buy_price"] = bool(refresh_buy_price)

    return RunConfig.from_kwargs(**raw)


def describe_scan_plan(scan_limit: int) -> Tuple[int, int, int]:
    wl = safe_read_csv(WATCHLIST_CSV)
    wl_n = len(wl) if wl is not None else 0
    planned = wl_n if int(scan_limit) == 0 else min(wl_n, int(scan_limit))
    return planned, wl_n, int(scan_limit)


def safe_result_count(result: object) -> Optional[int]:
    """
    scan_watchlist() might return a list, df, dict, None, etc.
    We only display a row count if it's safely discoverable.
    """
    if result is None:
        return None
    try:
        return len(result)  # type: ignore[arg-type]
    except Exception:
        return None


# ============================================================
# Streamlit UI
# ============================================================
st.set_page_config(page_title="Keyflip Scanner", layout="wide")

st.title("Keyflip — Fanatical → Eneba Scanner")
st.caption("Build a watchlist from Fanatical, then scan Eneba to find profitable resale deals.")

# ------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------
with st.sidebar:
    st.header("Run settings")

    max_buy = st.number_input("Max buy price (£)", min_value=1.0, max_value=200.0, value=15.0, step=0.5)
    watchlist_target = st.number_input("Watchlist size target", min_value=1, max_value=200, value=15, step=1)

    st.divider()
    st.subheader("Build (verification)")

    verify_candidates = st.number_input("Verify candidates", min_value=0, max_value=5000, value=300, step=25)
    pages_per_source = st.number_input("Pages per source", min_value=1, max_value=25, value=5, step=1)
    verify_limit = st.number_input("Verify limit (0 = use safety cap)", min_value=0, max_value=500, value=0, step=1)
    safety_cap = st.number_input("Verify safety cap", min_value=1, max_value=500, value=20, step=1)

    st.divider()
    st.subheader("Scan")

    scan_limit = st.number_input("Scan limit (0 = scan ALL)", min_value=0, max_value=20000, value=0, step=25)
    refresh_buy_price = st.checkbox("Refresh buy price during scan (Playwright)", value=False)
    avoid_recent_days = st.number_input("Avoid recent days", min_value=0, max_value=30, value=0, step=1)

    st.divider()
    st.subheader("Currency / timeouts")

    allow_eur = st.checkbox("Allow EUR prices (convert to GBP)", value=False)
    eur_to_gbp = st.number_input("EUR→GBP conversion rate", min_value=0.1, max_value=2.0, value=0.86, step=0.01)
    item_budget = st.number_input("Per-item time budget (sec)", min_value=5.0, max_value=180.0, value=55.0, step=5.0)
    run_budget = st.number_input("Overall run time budget (sec, 0 = none)", min_value=0.0, max_value=3600.0, value=0.0, step=10.0)

    st.divider()
    st.header("Maintenance")

    colA, colB = st.columns(2)
    do_install_pw = colA.button("Install Chromium", use_container_width=True)
    do_clear_cache = colB.button("Clear cache DB", use_container_width=True)

    do_clear_recent = st.button("Clear recent flags", use_container_width=True)
    do_delete_outputs = st.button("Delete CSV outputs", use_container_width=True)

# ------------------------------------------------------------
# Maintenance actions
# ------------------------------------------------------------
if do_install_pw:
    ensure_playwright_chromium_installed()

if do_clear_cache:
    try:
        cache = _open_cache(DB_PATH)
        cache.clear_all()
        st.success("Cache cleared.")
    except Exception as e:
        st.error(f"Failed to clear cache: {type(e).__name__}: {e}")

if do_clear_recent:
    try:
        cache = _open_cache(DB_PATH)
        if hasattr(cache, "clear_recent"):
            cache.clear_recent()  # type: ignore[attr-defined]
            st.success("Recent flags cleared.")
        else:
            cache.clear_all()
            st.success("Recent flags cleared (by clearing cache).")
    except Exception as e:
        st.error(f"Failed to clear recent flags: {type(e).__name__}: {e}")

if do_delete_outputs:
    n = delete_outputs()
    st.success(f"Deleted {n} output file(s).")

# ------------------------------------------------------------
# Action buttons
# ------------------------------------------------------------
st.divider()
a1, a2, a3 = st.columns([1, 1, 1])
build_clicked = a1.button("🔨 Build watchlist", use_container_width=True)
scan_clicked = a2.button("🔍 Scan watchlist", use_container_width=True)
play_clicked = a3.button("▶️ Play all (build + scan)", use_container_width=True)

# ------------------------------------------------------------
# Build / Scan logic (preserve core functionality)
# ------------------------------------------------------------
def run_build() -> None:
    cfg = make_config(
        max_buy=max_buy,
        watchlist_target=watchlist_target,
        verify_candidates=verify_candidates,
        pages_per_source=pages_per_source,
        verify_limit=verify_limit,
        safety_cap=safety_cap,
        avoid_recent_days=avoid_recent_days,
        allow_eur=allow_eur,
        eur_to_gbp=eur_to_gbp,
        item_budget=item_budget,
        run_budget=run_budget,
        include_scan_fields=False,
        scan_limit=scan_limit,
        refresh_buy_price=refresh_buy_price,
    )
    with st.spinner("Building watchlist from Fanatical..."):
        added = build_watchlist(cfg, WATCHLIST_CSV)

    if added:
        st.success(f"Watchlist built with {added} item(s).")
    else:
        st.warning("Watchlist built, but no items were added (no matches found).")


def run_scan() -> None:
    planned, wl_n, lim = describe_scan_plan(int(scan_limit))
    st.info(f"Scan plan: {planned} / {wl_n} watchlist items (scan_limit={lim})")

    if wl_n == 0:
        st.warning("No watchlist items found. Build a watchlist first.")
        return

    cfg = make_config(
        max_buy=max_buy,
        watchlist_target=watchlist_target,
        verify_candidates=verify_candidates,
        pages_per_source=pages_per_source,
        verify_limit=verify_limit,
        safety_cap=safety_cap,
        avoid_recent_days=avoid_recent_days,
        allow_eur=allow_eur,
        eur_to_gbp=eur_to_gbp,
        item_budget=item_budget,
        run_budget=run_budget,
        include_scan_fields=True,
        scan_limit=scan_limit,
        refresh_buy_price=refresh_buy_price,
    )

    with st.spinner("Scanning watchlist on Eneba..."):
        result = scan_watchlist(cfg, WATCHLIST_CSV, SCANS_CSV, PASSES_CSV, DB_PATH, DEFAULT_CACHE_FAIL_TTL)

    n = safe_result_count(result)
    if n is None:
        st.success("Scan complete. Results saved to scans.csv (and passes.csv if any deals matched).")
    else:
        st.success(f"Scan complete. Wrote {n} row(s) to scans.csv.")


# Execute actions
try:
    if build_clicked:
        run_build()

    if scan_clicked:
        run_scan()

    if play_clicked:
        run_build()
        # Only scan if a watchlist exists and has items
        if watchlist_is_ready():
            run_scan()
        else:
            st.warning("Play All stopped after build because the watchlist is empty.")
except Exception as e:
    st.error(f"Run failed: {type(e).__name__}: {e}")

# ------------------------------------------------------------
# Outputs
# ------------------------------------------------------------
st.divider()
render_paths()

watch_df = safe_read_csv(WATCHLIST_CSV)
scans_df = safe_read_csv(SCANS_CSV)
passes_df = safe_read_csv(PASSES_CSV)

tabs = st.tabs(["📋 Watchlist", "✅ Good deals", "📈 Latest scan", "⬇️ Downloads"])

with tabs[0]:
    if watch_df is None or watch_df.empty:
        st.info("No watchlist available yet. Build one first.")
    else:
        st.dataframe(watch_df, use_container_width=True, height=520)

with tabs[1]:
    if passes_df is None or passes_df.empty:
        st.info("No good deals found yet (passes.csv is empty).")
    else:
        st.dataframe(passes_df, use_container_width=True, height=520)

with tabs[2]:
    if scans_df is None or scans_df.empty:
        st.info("No scan results found yet.")
    else:
        batch_id, ts, recent = latest_batch_from_scans(scans_df)
        meta = []
        if batch_id:
            meta.append(f"Batch: `{batch_id}`")
        if ts is not None:
            meta.append(f"Latest time: `{ts}`")
        if meta:
            st.caption(" • ".join(meta))
        st.dataframe(recent, use_container_width=True, height=520)

with tabs[3]:
    b = safe_read_bytes(WATCHLIST_CSV)
    st.download_button(
        "Download watchlist.csv",
        data=b if b is not None else b"",
        file_name="watchlist.csv",
        use_container_width=True,
        disabled=b is None,
    )

    b = safe_read_bytes(SCANS_CSV)
    st.download_button(
        "Download scans.csv",
        data=b if b is not None else b"",
        file_name="scans.csv",
        use_container_width=True,
        disabled=b is None,
    )

    b = safe_read_bytes(PASSES_CSV)
    st.download_button(
        "Download passes.csv",
        data=b if b is not None else b"",
        file_name="passes.csv",
        use_container_width=True,
        disabled=b is None,
    )

st.caption(
    "Note: On Streamlit Cloud, Playwright browsers are best installed during deploy. "
    "If you enable Playwright-based scanning, use the sidebar ‘Install Chromium’ button if needed."
)
