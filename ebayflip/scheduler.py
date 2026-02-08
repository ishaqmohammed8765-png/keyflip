from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
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
from ebayflip.ebay_client import EbayClient, SearchAttemptLog, SearchResult
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
    request_cap_reached: bool
    zero_result_debug: list["TargetSearchDebug"]


@dataclass(slots=True)
class ScannedListing:
    title: str
    url: str
    target_name: str
    total_buy_gbp: float
    decision: str
    condition: Optional[str] = None


@dataclass(slots=True)
class TargetSearchDebug:
    target_name: str
    target_query: str
    retry_report: list[str]
    diagnostics: list[SearchAttemptLog]
    rejection_counts: dict[str, int]
    raw_count: int
    filtered_count: int
    last_request_url: Optional[str]
    blocked_message: Optional[str] = None
    blocked_reason: Optional[str] = None
    blocked_url: Optional[str] = None
    debug_artifacts: list[str] = dataclasses.field(default_factory=list)


def run_scan(config: AppConfig, client: EbayClient) -> ScanSummary:
    init_db(config.db_path)
    targets = [target for target in list_targets(config.db_path) if target.enabled]
    new_listings = 0
    evaluated = 0
    deals = 0
    stop_scan = False
    scanned_targets = 0
    request_cap_reached = False
    scanned_listings: list[ScannedListing] = []
    zero_result_debug: list[TargetSearchDebug] = []

    for target in targets:
        if stop_scan:
            break
        normalized_target, skip_reason = _normalize_target_query(target)
        if skip_reason:
            LOGGER.warning("Skipping target %s: %s", target.name, skip_reason)
            zero_result_debug.append(
                TargetSearchDebug(
                    target_name=target.name,
                    target_query=target.query or target.name,
                    retry_report=[skip_reason],
                    diagnostics=[],
                    rejection_counts={},
                    raw_count=0,
                    filtered_count=0,
                    last_request_url=None,
                    blocked_message=None,
                    blocked_reason=None,
                    blocked_url=None,
                    debug_artifacts=[],
                )
            )
            continue
        if client.request_count >= config.run.request_cap:
            LOGGER.info("Request cap reached, stopping scan.")
            request_cap_reached = True
            break
        scanned_targets += 1
        search_result: SearchResult = client.search_active_listings(normalized_target)
        listings = search_result.listings
        if client.request_cap_reached:
            request_cap_reached = True
            stop_scan = True
            break
        if not listings:
            blocked = search_result.blocked
            zero_result_debug.append(
                TargetSearchDebug(
                    target_name=target.name,
                    target_query=target.query,
                    retry_report=search_result.retry_report,
                    diagnostics=search_result.diagnostics,
                    rejection_counts=search_result.rejection_counts,
                    raw_count=search_result.raw_count,
                    filtered_count=search_result.filtered_count,
                    last_request_url=search_result.last_request_url,
                    blocked_message=blocked.message if blocked else None,
                    blocked_reason=blocked.reason if blocked else None,
                    blocked_url=blocked.url if blocked else None,
                    debug_artifacts=blocked.debug_artifacts if blocked else [],
                )
            )
        for listing in listings:
            listing_id, is_new = upsert_listing(config.db_path, listing)
            if is_new:
                new_listings += 1
            comps = get_latest_comps(config.db_path, listing_id)
            if comps is None or _comps_stale(comps.computed_at, config.run.comps_ttl_hours):
                comp_query = _comp_query_for_listing(listing, target)
                if client.request_count >= config.run.request_cap:
                    if comps is None:
                        LOGGER.info("Request cap reached before comps search; using empty comps.")
                        comps = compute_comp_stats(comp_query, [])
                        insert_comps(config.db_path, listing_id, comps)
                    else:
                        LOGGER.info("Request cap reached before comps refresh; using stale comps.")
                else:
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
            if evaluation.decision in ("deal", "maybe"):
                deals += 1
                _send_alert_if_needed(config, listing_id, listing, evaluation)
            if client.request_cap_reached:
                request_cap_reached = True
                stop_scan = True
                break

    return ScanSummary(
        scanned_targets=scanned_targets,
        new_listings=new_listings,
        evaluated=evaluated,
        deals=deals,
        last_scan=datetime.now(timezone.utc).isoformat(),
        scanned_listings=scanned_listings,
        request_cap_reached=request_cap_reached,
        zero_result_debug=zero_result_debug,
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


def _normalize_target_query(target: Target) -> tuple[Target, Optional[str]]:
    query = (target.query or "").strip()
    if not query:
        query = (target.name or "").strip()
    if not query:
        return target, "skipped: empty keywords and target name"
    if query != target.query:
        return dataclasses.replace(target, query=query), None
    return target, None


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
    age = datetime.now(timezone.utc) - computed_time
    return age.total_seconds() >= ttl_hours * 3600
