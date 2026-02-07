from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Optional

from ebayflip import get_logger
from ebayflip.models import CompStats, Evaluation, Listing, Target

LOGGER = get_logger()


@contextmanager
def get_connection(db_path: str) -> Iterable[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                query TEXT NOT NULL,
                category_id TEXT,
                condition TEXT,
                max_buy_gbp REAL,
                shipping_max_gbp REAL,
                listing_type TEXT DEFAULT 'any',
                country TEXT DEFAULT 'UK',
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id TEXT UNIQUE,
                name TEXT,
                parent_id TEXT,
                level INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_categories_parent
            ON categories(parent_id)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebay_item_id TEXT UNIQUE,
                target_id INTEGER,
                title TEXT,
                url TEXT,
                price_gbp REAL,
                shipping_gbp REAL,
                total_buy_gbp REAL,
                condition TEXT,
                seller_feedback_pct REAL,
                seller_feedback_score INTEGER,
                returns_accepted INTEGER,
                listing_type TEXT,
                start_time TEXT,
                end_time TEXT,
                location TEXT,
                image_url TEXT,
                raw_json TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                FOREIGN KEY (target_id) REFERENCES targets(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS comps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                comp_query TEXT,
                sold_count INTEGER,
                median_sold_gbp REAL,
                p25_sold_gbp REAL,
                p75_sold_gbp REAL,
                spread_gbp REAL,
                computed_at TEXT,
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                resale_est_gbp REAL,
                ebay_fee_pct REAL,
                other_fees_gbp REAL,
                shipping_out_gbp REAL,
                buffer_gbp REAL,
                expected_profit_gbp REAL,
                roi REAL,
                confidence REAL,
                deal_score REAL,
                decision TEXT,
                reasons_json TEXT,
                evaluated_at TEXT,
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts_sent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                channel TEXT,
                sent_at TEXT,
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            )
            """
        )


def add_target(db_path: str, target: Target) -> int:
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO targets
                (name, query, category_id, condition, max_buy_gbp, shipping_max_gbp,
                 listing_type, country, enabled, created_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target.name,
                target.query,
                target.category_id,
                target.condition,
                target.max_buy_gbp,
                target.shipping_max_gbp,
                target.listing_type,
                target.country,
                1 if target.enabled else 0,
                target.created_at,
            ),
        )
        return int(cursor.lastrowid)


def update_target(db_path: str, target: Target) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE targets
            SET name = ?, query = ?, category_id = ?, condition = ?, max_buy_gbp = ?,
                shipping_max_gbp = ?, listing_type = ?, country = ?, enabled = ?
            WHERE id = ?
            """,
            (
                target.name,
                target.query,
                target.category_id,
                target.condition,
                target.max_buy_gbp,
                target.shipping_max_gbp,
                target.listing_type,
                target.country,
                1 if target.enabled else 0,
                target.id,
            ),
        )


def delete_target(db_path: str, target_id: int) -> None:
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))


def list_targets(db_path: str) -> list[Target]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM targets ORDER BY created_at DESC").fetchall()
    return [Target.from_row(row) for row in rows]


def upsert_listing(db_path: str, listing: Listing) -> tuple[int, bool]:
    now_iso = datetime.utcnow().isoformat()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO listings
                (ebay_item_id, target_id, title, url, price_gbp, shipping_gbp, total_buy_gbp,
                 condition, seller_feedback_pct, seller_feedback_score, returns_accepted,
                 listing_type, start_time, end_time, location, image_url, raw_json,
                 first_seen_at, last_seen_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ebay_item_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                price_gbp = excluded.price_gbp,
                shipping_gbp = excluded.shipping_gbp,
                total_buy_gbp = excluded.total_buy_gbp,
                condition = excluded.condition,
                seller_feedback_pct = excluded.seller_feedback_pct,
                seller_feedback_score = excluded.seller_feedback_score,
                returns_accepted = excluded.returns_accepted,
                listing_type = excluded.listing_type,
                end_time = excluded.end_time,
                location = excluded.location,
                image_url = excluded.image_url,
                raw_json = excluded.raw_json
            """,
            (
                listing.ebay_item_id,
                listing.target_id,
                listing.title,
                listing.url,
                listing.price_gbp,
                listing.shipping_gbp,
                listing.total_buy_gbp,
                listing.condition,
                listing.seller_feedback_pct,
                listing.seller_feedback_score,
                1 if listing.returns_accepted else 0,
                listing.listing_type,
                listing.start_time,
                listing.end_time,
                listing.location,
                listing.image_url,
                json.dumps(listing.raw_json),
                listing.first_seen_at or now_iso,
                now_iso,
            ),
        )
        # Check if this was an insert or update by looking up the row
        row = conn.execute(
            "SELECT id FROM listings WHERE ebay_item_id = ?",
            (listing.ebay_item_id,),
        ).fetchone()
        listing_id = int(row["id"])
        # rowcount == 1 for insert, but ON CONFLICT UPDATE also reports 1
        # Use lastrowid: non-zero only on actual INSERT
        is_new = cursor.lastrowid is not None and cursor.lastrowid > 0 and cursor.lastrowid == listing_id
        return listing_id, is_new


def get_listing(db_path: str, listing_id: int) -> Optional[Listing]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return Listing.from_row(row) if row else None


def get_latest_comps(db_path: str, listing_id: int) -> Optional[CompStats]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM comps
            WHERE listing_id = ?
            ORDER BY computed_at DESC
            LIMIT 1
            """,
            (listing_id,),
        ).fetchone()
    return CompStats.from_row(row) if row else None


def insert_comps(db_path: str, listing_id: int, comps: CompStats) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO comps
                (listing_id, comp_query, sold_count, median_sold_gbp, p25_sold_gbp,
                 p75_sold_gbp, spread_gbp, computed_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                comps.comp_query,
                comps.sold_count,
                comps.median_sold_gbp,
                comps.p25_sold_gbp,
                comps.p75_sold_gbp,
                comps.spread_gbp,
                comps.computed_at,
            ),
        )


def insert_evaluation(db_path: str, listing_id: int, evaluation: Evaluation) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO evaluations
                (listing_id, resale_est_gbp, ebay_fee_pct, other_fees_gbp, shipping_out_gbp,
                 buffer_gbp, expected_profit_gbp, roi, confidence, deal_score, decision,
                 reasons_json, evaluated_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                evaluation.resale_est_gbp,
                evaluation.ebay_fee_pct,
                evaluation.other_fees_gbp,
                evaluation.shipping_out_gbp,
                evaluation.buffer_gbp,
                evaluation.expected_profit_gbp,
                evaluation.roi,
                evaluation.confidence,
                evaluation.deal_score,
                evaluation.decision,
                json.dumps(evaluation.reasons),
                evaluation.evaluated_at,
            ),
        )


def get_latest_evaluation(db_path: str, listing_id: int) -> Optional[Evaluation]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM evaluations
            WHERE listing_id = ?
            ORDER BY evaluated_at DESC
            LIMIT 1
            """,
            (listing_id,),
        ).fetchone()
    return Evaluation.from_row(row) if row else None


def list_evaluations(db_path: str) -> list[Evaluation]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT e.* FROM evaluations e
            ORDER BY evaluated_at DESC
            """
        ).fetchall()
    return [Evaluation.from_row(row) for row in rows]


def list_listings(db_path: str) -> list[Listing]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM listings ORDER BY last_seen_at DESC").fetchall()
    return [Listing.from_row(row) for row in rows]


def list_evaluations_with_listings(db_path: str) -> list[dict]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                e.id AS evaluation_id,
                e.listing_id,
                e.resale_est_gbp,
                e.expected_profit_gbp,
                e.roi,
                e.confidence,
                e.deal_score,
                e.decision,
                e.reasons_json,
                e.evaluated_at,
                l.title,
                l.url,
                l.total_buy_gbp,
                l.target_id,
                l.image_url,
                l.location,
                l.seller_feedback_pct,
                l.seller_feedback_score,
                l.returns_accepted,
                l.listing_type,
                l.start_time,
                l.end_time
            FROM evaluations e
            JOIN listings l ON l.id = e.listing_id
            ORDER BY e.evaluated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_comps_by_listing(db_path: str, listing_id: int) -> list[CompStats]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM comps
            WHERE listing_id = ?
            ORDER BY computed_at DESC
            """,
            (listing_id,),
        ).fetchall()
    return [CompStats.from_row(row) for row in rows]


def mark_alert_sent(db_path: str, listing_id: int, channel: str) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO alerts_sent (listing_id, channel, sent_at)
            VALUES (?, ?, ?)
            """,
            (listing_id, channel, datetime.utcnow().isoformat()),
        )


def was_alert_sent(db_path: str, listing_id: int, channel: str) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM alerts_sent
            WHERE listing_id = ? AND channel = ?
            LIMIT 1
            """,
            (listing_id, channel),
        ).fetchone()
    return row is not None
