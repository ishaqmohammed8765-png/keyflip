from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ebayflip import get_logger
from ebayflip.alerts import send_discord_alert
from ebayflip.comps import compute_comp_stats
from ebayflip.config import AppConfig
from ebayflip.db import (
    get_latest_comps,
    init_db,
    insert_comps,
    insert_evaluation,
    list_targets,
    mark_alert_sent,
    upsert_listing,
    was_alert_sent,
)
from ebayflip.ebay_client import EbayClient
from ebayflip.models import CompStats, Evaluation, Listing, Target
from ebayflip.scoring import evaluate_listing

LOGGER = get_logger()


@dataclass(slots=True)
class ScanSummary:
    scanned_targets: int
    new_listings: int
    evaluated: int
    deals: int
    last_scan: str
    scanned_listings: list["ScannedListing"]


@dataclass(slots=True)
class ScannedListing:
    title: str
    url: str
    target_name: str
    total_buy_gbp: float
    decision: str
    condition: Optional[str] = None


def run_scan(config: AppConfig, client: EbayClient) -> ScanSummary:
    init_db(config.db_path)
    targets = [target for target in list_targets(config.db_path) if target.enabled]
    new_listings = 0
    evaluated = 0
    deals = 0
    stop_scan = False
    scanned_listings: list[ScannedListing] = []

    for target in targets:
        if stop_scan:
            break
        if client.request_count >= config.run.request_cap:
            LOGGER.info("Request cap reached, stopping scan.")
            break
        listings = client.search_active_listings(target)
        for listing in listings:
            if client.request_count >= config.run.request_cap:
                LOGGER.info("Request cap reached mid-target, stopping scan.")
                stop_scan = True
                break
            listing_id, is_new = upsert_listing(config.db_path, listing)
            if is_new:
                new_listings += 1
            comps = get_latest_comps(config.db_path, listing_id)
            if comps is None or _comps_stale(comps.computed_at, config.run.comps_ttl_hours):
                if client.request_count >= config.run.request_cap:
                    LOGGER.info("Request cap reached before comps search.")
                    stop_scan = True
                    break
                comp_query = _comp_query_for_listing(listing, target)
                comps_list = client.search_sold_comps(comp_query)
                comps = compute_comp_stats(comp_query, comps_list)
                insert_comps(config.db_path, listing_id, comps)
            evaluation = evaluate_listing(listing, comps, config.run)
            insert_evaluation(config.db_path, listing_id, evaluation)
            evaluated += 1
            scanned_listings.append(
                ScannedListing(
                    title=listing.title or "Untitled listing",
                    url=listing.url,
                    target_name=target.name,
                    total_buy_gbp=listing.total_buy_gbp,
                    decision=evaluation.decision,
                    condition=listing.condition,
                )
            )
            if evaluation.decision == "deal":
                deals += 1
                _send_alert_if_needed(config, listing_id, listing, evaluation)

    return ScanSummary(
        scanned_targets=len(targets),
        new_listings=new_listings,
        evaluated=evaluated,
        deals=deals,
        last_scan=datetime.utcnow().isoformat(),
        scanned_listings=scanned_listings,
    )


def _send_alert_if_needed(config: AppConfig, listing_id: int, listing: Listing, evaluation: Evaluation) -> None:
    if was_alert_sent(config.db_path, listing_id, "discord"):
        return
    sent = send_discord_alert(
        config.alerts.discord_webhook_url,
        listing.title,
        listing.url,
        listing.total_buy_gbp,
        evaluation.resale_est_gbp,
        evaluation.expected_profit_gbp,
        evaluation.roi,
        evaluation.confidence,
        evaluation.reasons,
    )
    if sent:
        mark_alert_sent(config.db_path, listing_id, "discord")


def _comp_query_for_listing(listing: Listing, target: Target) -> str:
    if listing.title:
        return listing.title.strip() or target.query
    return target.query


def _comps_stale(computed_at: Optional[str], ttl_hours: int) -> bool:
    if not computed_at:
        return True
    try:
        computed_time = datetime.fromisoformat(computed_at)
    except ValueError:
        return True
    age = datetime.utcnow() - computed_time
    return age.total_seconds() >= ttl_hours * 3600
