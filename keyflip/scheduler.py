from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .config import MIN_PROFIT_GBP, MIN_ROI, RunConfig
from .core import SCANS_COLS, WATCHLIST_COLS, scan_watchlist


@dataclass
class ItemState:
    item_key: str
    last_scanned_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_fail_at: Optional[datetime] = None
    fail_count: int = 0
    next_allowed_at: Optional[datetime] = None
    last_alert_at: Optional[datetime] = None
    last_seen_sell_gbp: Optional[float] = None
    last_fail_reason: Optional[str] = None


@dataclass
class WatchlistItem:
    item_key: str
    title: str
    buy_url: str
    row: dict[str, object]


@dataclass
class SchedulerConfig:
    state_db_path: Path
    watchlist_path: Path
    scans_path: Path
    passes_path: Path
    price_cache_path: Path
    temp_watchlist_path: Path
    batch_size: int = 20
    cooldown_min_minutes: int = 30
    cooldown_max_minutes: int = 60
    backoff_minutes: tuple[int, int, int] = (20, 60, 360)
    jitter_min_s: float = 0.4
    jitter_max_s: float = 1.6
    fail_ttl_s: int = 1200


@dataclass
class AlertThresholds:
    min_profit_gbp: float = MIN_PROFIT_GBP
    min_roi: float = MIN_ROI
    cooldown_hours: float = 6.0
    price_change_pct: float = 3.0


@dataclass
class CycleReport:
    eligible_count: int
    scanned_titles: list[str]
    next_eligible_in: Optional[timedelta]
    alerts: list[dict[str, object]]
    failures: list[dict[str, object]]
    scan_rows: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _str_to_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _to_str(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    s = str(value).strip()
    return "" if s.lower() == "nan" else s


def _to_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        s = str(value).strip()
        if not s or s.lower() == "nan":
            return None
        return float(s)
    except Exception:
        return None


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_state (
            item_key TEXT PRIMARY KEY,
            title TEXT,
            last_scanned_at TEXT,
            last_success_at TEXT,
            last_fail_at TEXT,
            fail_count INTEGER,
            next_allowed_at TEXT,
            last_alert_at TEXT,
            last_seen_sell_gbp REAL,
            last_fail_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            item_key TEXT,
            title TEXT,
            buy_gbp REAL,
            sell_gbp REAL,
            profit_gbp REAL,
            roi REAL,
            buy_url TEXT,
            sell_url TEXT,
            notes TEXT
        )
        """
    )
    conn.commit()


def load_state(db_path: Path) -> dict[str, ItemState]:
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT item_key, last_scanned_at, last_success_at, last_fail_at,
                   fail_count, next_allowed_at, last_alert_at,
                   last_seen_sell_gbp, last_fail_reason
            FROM scheduler_state
            """
        ).fetchall()
    finally:
        conn.close()

    states: dict[str, ItemState] = {}
    for row in rows:
        (
            item_key,
            last_scanned_at,
            last_success_at,
            last_fail_at,
            fail_count,
            next_allowed_at,
            last_alert_at,
            last_seen_sell_gbp,
            last_fail_reason,
        ) = row
        states[item_key] = ItemState(
            item_key=item_key,
            last_scanned_at=_str_to_dt(last_scanned_at),
            last_success_at=_str_to_dt(last_success_at),
            last_fail_at=_str_to_dt(last_fail_at),
            fail_count=int(fail_count or 0),
            next_allowed_at=_str_to_dt(next_allowed_at),
            last_alert_at=_str_to_dt(last_alert_at),
            last_seen_sell_gbp=last_seen_sell_gbp,
            last_fail_reason=last_fail_reason or None,
        )
    return states


def save_state(db_path: Path, states: Iterable[ItemState]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_tables(conn)
        rows = [
            (
                s.item_key,
                _dt_to_str(s.last_scanned_at),
                _dt_to_str(s.last_success_at),
                _dt_to_str(s.last_fail_at),
                int(s.fail_count),
                _dt_to_str(s.next_allowed_at),
                _dt_to_str(s.last_alert_at),
                s.last_seen_sell_gbp,
                s.last_fail_reason,
            )
            for s in states
        ]
        conn.executemany(
            """
            INSERT INTO scheduler_state (
                item_key, last_scanned_at, last_success_at, last_fail_at, fail_count,
                next_allowed_at, last_alert_at, last_seen_sell_gbp, last_fail_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_key) DO UPDATE SET
                last_scanned_at=excluded.last_scanned_at,
                last_success_at=excluded.last_success_at,
                last_fail_at=excluded.last_fail_at,
                fail_count=excluded.fail_count,
                next_allowed_at=excluded.next_allowed_at,
                last_alert_at=excluded.last_alert_at,
                last_seen_sell_gbp=excluded.last_seen_sell_gbp,
                last_fail_reason=excluded.last_fail_reason
            """
            ,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def select_batch(
    now: datetime,
    items: list[WatchlistItem],
    states: dict[str, ItemState],
    batch_size: int,
) -> list[WatchlistItem]:
    eligible = []

    def _next_allowed(item: WatchlistItem) -> datetime:
        state = states.get(item.item_key)
        if state is None or state.next_allowed_at is None:
            return now
        return state.next_allowed_at

    for item in items:
        next_allowed = _next_allowed(item)
        if next_allowed <= now:
            eligible.append(item)
    eligible.sort(key=_next_allowed)
    return eligible[: max(0, int(batch_size))]


def update_state_after_result(state: ItemState, result: dict[str, object]) -> ItemState:
    now = result["now"]
    success = bool(result.get("success"))
    sell_gbp = result.get("sell_gbp")
    state.last_scanned_at = now

    if success:
        state.last_success_at = now
        state.fail_count = 0
        state.last_fail_reason = None
        state.last_seen_sell_gbp = sell_gbp if sell_gbp is not None else state.last_seen_sell_gbp
    else:
        state.last_fail_at = now
        state.fail_count = int(state.fail_count or 0) + 1
        state.last_fail_reason = str(result.get("fail_reason") or "unknown")

    state.next_allowed_at = result.get("next_allowed_at")
    return state


def build_watchlist_items(watchlist_df: pd.DataFrame) -> list[WatchlistItem]:
    if watchlist_df is None or watchlist_df.empty:
        return []
    items: list[WatchlistItem] = []
    for _, row in watchlist_df.iterrows():
        title = _to_str(row.get("title", ""))
        buy_url = _to_str(row.get("buy_url", ""))
        item_key = buy_url or title
        if not item_key:
            continue
        items.append(
            WatchlistItem(
                item_key=item_key,
                title=title,
                buy_url=buy_url,
                row=row.to_dict(),
            )
        )
    return items


def _classify_failure(notes: str) -> str:
    n = (notes or "").lower()
    if "timeout" in n:
        return "timeout"
    if "captcha" in n or "blocked" in n:
        return "blocked/captcha"
    if "parse" in n or "json" in n:
        return "parse_error"
    if "no_result" in n or "not found" in n:
        return "no_result"
    return "unknown"


def _merge_watchlist_updates(watchlist_path: Path, updated_path: Path) -> None:
    try:
        original = pd.read_csv(watchlist_path)
        updated = pd.read_csv(updated_path)
    except Exception:
        return

    if original.empty or updated.empty:
        return

    def make_key(df: pd.DataFrame) -> pd.Series:
        buy_url = df["buy_url"] if "buy_url" in df.columns else pd.Series([""] * len(df))
        title = df["title"] if "title" in df.columns else pd.Series([""] * len(df))
        return (
            buy_url.fillna("").astype(str).str.strip().replace("nan", "")
            .where(lambda s: s != "", title.fillna("").astype(str).str.strip())
        )

    original_keys = make_key(original)
    updated_keys = make_key(updated)

    updates = {}
    for col in ("eneba_url", "eneba_notes", "buy_site", "buy_trust"):
        if col in updated.columns:
            updates[col] = updated[col]

    if not updates:
        return

    changed = False
    for idx, key in original_keys.items():
        if not key:
            continue
        matches = updated_keys == key
        if not matches.any():
            continue
        update_row = updated.loc[matches].iloc[-1]
        for col, series in updates.items():
            val = update_row.get(col)
            if pd.isna(val) or val is None or val == "":
                continue
            if col not in original.columns or original.at[idx, col] != val:
                original.at[idx, col] = val
                changed = True

    if changed:
        original.reindex(columns=list(dict.fromkeys(original.columns))).to_csv(watchlist_path, index=False)


def _record_deal(db_path: Path, alert: dict[str, object]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO deals (
                created_at, item_key, title, buy_gbp, sell_gbp,
                profit_gbp, roi, buy_url, sell_url, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.get("created_at"),
                alert.get("item_key"),
                alert.get("title"),
                alert.get("buy_gbp"),
                alert.get("sell_gbp"),
                alert.get("profit_gbp"),
                alert.get("roi"),
                alert.get("buy_url"),
                alert.get("sell_url"),
                alert.get("notes"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _next_allowed_after_success(now: datetime, config: SchedulerConfig) -> datetime:
    mins = random.randint(int(config.cooldown_min_minutes), int(config.cooldown_max_minutes))
    return now + timedelta(minutes=mins)


def _next_allowed_after_fail(now: datetime, config: SchedulerConfig, fail_count: int) -> datetime:
    schedule = config.backoff_minutes
    if fail_count <= 1:
        mins = schedule[0]
    elif fail_count == 2:
        mins = schedule[1]
    else:
        mins = schedule[2]
    return now + timedelta(minutes=int(mins))


def run_cycle(
    config: SchedulerConfig,
    watchlist_items: list[WatchlistItem],
    scan_config: RunConfig,
    thresholds: AlertThresholds,
) -> CycleReport:
    now = _utcnow()
    states = load_state(config.state_db_path)

    selected = select_batch(now, watchlist_items, states, config.batch_size)
    eligible_count = sum(
        1
        for item in watchlist_items
        if states.get(item.item_key) is None
        or states[item.item_key].next_allowed_at is None
        or states[item.item_key].next_allowed_at <= now
    )

    if not selected:
        next_times = [
            state.next_allowed_at
            for state in states.values()
            if state.next_allowed_at and state.next_allowed_at > now
        ]
        next_delta = min(next_times) - now if next_times else None
        return CycleReport(
            eligible_count=eligible_count,
            scanned_titles=[],
            next_eligible_in=next_delta,
            alerts=[],
            failures=[],
            scan_rows=0,
        )

    temp_df = pd.DataFrame([item.row for item in selected])
    temp_df.reindex(columns=WATCHLIST_COLS).to_csv(config.temp_watchlist_path, index=False)

    jitter_s = random.uniform(config.jitter_min_s, config.jitter_max_s)
    scan_cfg = replace(scan_config, scan_limit=0, scan_sleep_s=jitter_s)
    scan_df = scan_watchlist(
        scan_cfg,
        config.temp_watchlist_path,
        config.scans_path,
        config.passes_path,
        config.price_cache_path,
        config.fail_ttl_s,
    )

    _merge_watchlist_updates(config.watchlist_path, config.temp_watchlist_path)

    alerts: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    scanned_titles: list[str] = []

    if scan_df is None or scan_df.empty:
        save_state(config.state_db_path, states.values())
        return CycleReport(
            eligible_count=eligible_count,
            scanned_titles=[],
            next_eligible_in=None,
            alerts=[],
            failures=[],
            scan_rows=0,
        )

    scan_df = scan_df.reindex(columns=SCANS_COLS)

    for _, row in scan_df.iterrows():
        title = _to_str(row.get("title", ""))
        buy_url = _to_str(row.get("buy_url", ""))
        item_key = buy_url or title
        if not item_key:
            continue

        scanned_titles.append(title or item_key)
        sell_gbp = _to_float(row.get("market_price"))
        profit_gbp = _to_float(row.get("edge"))
        roi = _to_float(row.get("edge_pct"))
        market_notes = _to_str(row.get("market_notes", ""))
        sell_url = _to_str(row.get("market_url", ""))

        state = states.get(item_key) or ItemState(item_key=item_key)
        success = sell_gbp is not None

        next_allowed_at = (
            _next_allowed_after_success(now, config)
            if success
            else _next_allowed_after_fail(now, config, state.fail_count + 1)
        )

        result = {
            "now": now,
            "success": success,
            "sell_gbp": sell_gbp,
            "fail_reason": None if success else _classify_failure(market_notes),
            "next_allowed_at": next_allowed_at,
        }
        state = update_state_after_result(state, result)

        if not success:
            failures.append(
                {
                    "title": title or item_key,
                    "reason": state.last_fail_reason,
                }
            )
        else:
            state.last_seen_sell_gbp = sell_gbp

        if (
            success
            and profit_gbp is not None
            and roi is not None
            and profit_gbp >= thresholds.min_profit_gbp
            and roi >= thresholds.min_roi
        ):
            should_alert = False
            last_alert_at = state.last_alert_at
            if not last_alert_at:
                should_alert = True
            else:
                cooldown = timedelta(hours=float(thresholds.cooldown_hours))
                if now - last_alert_at >= cooldown:
                    should_alert = True

            if not should_alert and state.last_seen_sell_gbp:
                pct_change = abs(sell_gbp - state.last_seen_sell_gbp) / max(state.last_seen_sell_gbp, 0.01)
                if pct_change * 100.0 >= float(thresholds.price_change_pct):
                    should_alert = True

            if should_alert:
                alert = {
                    "created_at": _dt_to_str(now),
                    "item_key": item_key,
                    "title": title or item_key,
                    "buy_gbp": _to_float(row.get("buy_price")),
                    "sell_gbp": sell_gbp,
                    "profit_gbp": profit_gbp,
                    "roi": roi,
                    "buy_url": buy_url,
                    "sell_url": sell_url,
                    "notes": market_notes,
                }
                alerts.append(alert)
                _record_deal(config.state_db_path, alert)
                state.last_alert_at = now
                state.last_seen_sell_gbp = sell_gbp

        states[item_key] = state

    save_state(config.state_db_path, states.values())

    next_times = [
        state.next_allowed_at
        for state in states.values()
        if state.next_allowed_at and state.next_allowed_at > now
    ]
    next_delta = min(next_times) - now if next_times else None

    return CycleReport(
        eligible_count=eligible_count,
        scanned_titles=scanned_titles,
        next_eligible_in=next_delta,
        alerts=alerts,
        failures=failures,
        scan_rows=int(len(scan_df)),
    )
