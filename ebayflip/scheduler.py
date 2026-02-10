from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import os
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
    reserve_alert_send,
    release_alert_send,
    upsert_listing,
)
from ebayflip.ebay_client import EbayClient, RequestBudget, SearchAttemptLog, SearchResult
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
    buy_marketplace: str = "ebay"
    sell_marketplace: str = "ebay"
    opportunities: list["ArbitrageOpportunity"] = dataclasses.field(default_factory=list)


@dataclass(slots=True)
class ScannedListing:
    title: str
    url: str
    target_name: str
    total_buy_gbp: float
    decision: str
    condition: Optional[str] = None
    source: Optional[str] = None


@dataclass(slots=True)
class ArbitrageOpportunity:
    listing_id: int
    target_name: str
    listing_title: str
    listing_url: str
    buy_marketplace: str
    sell_marketplace: str
    source: str
    total_buy_gbp: float
    resale_est_gbp: float
    expected_profit_gbp: float
    roi: float
    confidence: float
    deal_score: float
    decision: str
    comp_query: str
    evaluated_at: str


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


@dataclass(slots=True)
class TargetScanResult:
    scanned_targets: int = 0
    new_listings: int = 0
    evaluated: int = 0
    deals: int = 0
    scanned_listings: list[ScannedListing] = dataclasses.field(default_factory=list)
    zero_result_debug: list[TargetSearchDebug] = dataclasses.field(default_factory=list)
    opportunities: list[ArbitrageOpportunity] = dataclasses.field(default_factory=list)
    request_cap_reached: bool = False
    request_count: int = 0


def run_scan(config: AppConfig, client: EbayClient) -> ScanSummary:
    scanner = ArbitrageScanner(config=config, client=client)
    return scanner.scan()


class ArbitrageScanner:
    """Scan a buy marketplace, compare against sell-market comps, and score profitability."""

    def __init__(self, *, config: AppConfig, client: EbayClient) -> None:
        self.config = config
        self.client = client
        self.new_listings = 0
        self.evaluated = 0
        self.deals = 0
        self.scanned_targets = 0
        self.request_cap_reached = False
        self.scanned_listings: list[ScannedListing] = []
        self.zero_result_debug: list[TargetSearchDebug] = []
        self.opportunities: list[ArbitrageOpportunity] = []
        self.stop_scan = False
        self.total_request_count = 0
        self.request_budget = RequestBudget(self.config.run.request_cap)
        self._attach_budget(self.client)

    def scan(self) -> ScanSummary:
        init_db(self.config.db_path)
        targets = [target for target in list_targets(self.config.db_path) if target.enabled]
        workers = max(1, int(self.config.run.scan_workers or 1))
        can_parallelize = isinstance(self.client, EbayClient)
        if workers == 1 or len(targets) <= 1 or not can_parallelize:
            for target in targets:
                if self.stop_scan:
                    break
                result = self._scan_target(target, self.client)
                self._merge_result(result)
                self.total_request_count = self._client_total_requests(self.client)
                if result.request_cap_reached:
                    self.stop_scan = True
                    self.request_cap_reached = True
                    break
        else:
            self._scan_parallel(targets, workers=workers)
            if self.total_request_count >= self.config.run.request_cap:
                self.request_cap_reached = True
        return ScanSummary(
            scanned_targets=self.scanned_targets,
            new_listings=self.new_listings,
            evaluated=self.evaluated,
            deals=self.deals,
            last_scan=datetime.now(timezone.utc).isoformat(),
            scanned_listings=self.scanned_listings,
            request_cap_reached=self.request_cap_reached,
            zero_result_debug=self.zero_result_debug,
            buy_marketplace=self.config.run.marketplace,
            sell_marketplace=self.config.run.sell_marketplace,
            opportunities=self.opportunities,
        )

    def _scan_parallel(self, targets: list[Target], *, workers: int) -> None:
        LOGGER.info("Running parallel scan with %s worker(s) across %s target(s).", workers, len(targets))
        worker_clients: list[EbayClient] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for target in targets:
                worker_client = EbayClient(
                    self.config.run,
                    app_id=self.client.app_id,
                    request_budget=self.request_budget,
                )
                worker_clients.append(worker_client)
                future = executor.submit(self._scan_target, target, worker_client)
                futures[future] = target
            for future in as_completed(futures):
                target = futures[future]
                try:
                    result = future.result()
                except Exception:
                    LOGGER.exception("Target scan failed for %s", target.name)
                    continue
                self._merge_result(result)
        self.total_request_count = self.request_budget.used

    def _scan_target(self, target: Target, client: EbayClient) -> TargetScanResult:
        result = TargetScanResult()
        normalized_target, skip_reason = _normalize_target_query(target)
        if skip_reason:
            LOGGER.warning("Skipping target %s: %s", target.name, skip_reason)
            result.zero_result_debug.append(
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
            result.request_count = self._client_total_requests(client)
            return result
        if self._client_cap_reached(client):
            LOGGER.info("Request cap reached, stopping scan.")
            result.request_cap_reached = True
            result.request_count = self._client_total_requests(client)
            return result

        result.scanned_targets = 1
        search_result: SearchResult = client.search_active_listings(normalized_target)
        listings = search_result.listings
        if not listings:
            blocked = search_result.blocked
            result.zero_result_debug.append(
                TargetSearchDebug(
                    target_name=target.name,
                    target_query=normalized_target.query,
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
            listing_id, is_new = upsert_listing(self.config.db_path, listing)
            if is_new:
                result.new_listings += 1

            comps = self._comps_for_listing(listing_id, listing, normalized_target, client=client)
            evaluation = evaluate_listing(listing, comps, self.config.run)
            insert_evaluation(self.config.db_path, listing_id, evaluation)
            result.evaluated += 1
            result.scanned_listings.append(
                ScannedListing(
                    title=listing.title or "Untitled listing",
                    url=listing.url,
                    target_name=target.name,
                    total_buy_gbp=listing.total_buy_gbp,
                    decision=evaluation.decision,
                    condition=listing.condition,
                    source=_listing_source(listing, fallback=self.config.run.marketplace),
                )
            )
            result.opportunities.append(
                ArbitrageOpportunity(
                    listing_id=listing_id,
                    target_name=target.name,
                    listing_title=listing.title or "Untitled listing",
                    listing_url=listing.url,
                    buy_marketplace=self.config.run.marketplace,
                    sell_marketplace=self.config.run.sell_marketplace,
                    source=_listing_source(listing, fallback=self.config.run.marketplace),
                    total_buy_gbp=listing.total_buy_gbp,
                    resale_est_gbp=evaluation.resale_est_gbp,
                    expected_profit_gbp=evaluation.expected_profit_gbp,
                    roi=evaluation.roi,
                    confidence=evaluation.confidence,
                    deal_score=evaluation.deal_score,
                    decision=evaluation.decision,
                    comp_query=comps.comp_query,
                    evaluated_at=evaluation.evaluated_at,
                )
            )
            if evaluation.decision in ("deal", "maybe"):
                result.deals += 1
                _send_alert_if_needed(self.config, listing_id, listing, evaluation)
            if self._client_cap_reached(client):
                result.request_cap_reached = True
                break
        result.request_count = self._client_total_requests(client)
        return result

    def _comps_for_listing(self, listing_id: int, listing: Listing, target: Target, *, client: EbayClient) -> CompStats:
        comps = get_latest_comps(self.config.db_path, listing_id)
        refresh_empty = os.getenv("REFRESH_EMPTY_COMPS", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        should_refresh_empty = bool(comps is not None and refresh_empty and comps.sold_count == 0)
        if comps is None or should_refresh_empty or _comps_stale(comps.computed_at, self.config.run.comps_ttl_hours):
            comp_query = _comp_query_for_listing(listing, target)
            if self._client_cap_reached(client):
                if comps is None:
                    LOGGER.info("Request cap reached before comps search; using empty comps.")
                    comps = compute_comp_stats(comp_query, [])
                    insert_comps(self.config.db_path, listing_id, comps)
                else:
                    LOGGER.info("Request cap reached before comps refresh; using stale comps.")
            else:
                comps_list = client.search_sold_comps(comp_query)
                comps = compute_comp_stats(comp_query, comps_list)
                insert_comps(self.config.db_path, listing_id, comps)
        return comps

    def _merge_result(self, result: TargetScanResult) -> None:
        self.scanned_targets += result.scanned_targets
        self.new_listings += result.new_listings
        self.evaluated += result.evaluated
        self.deals += result.deals
        self.scanned_listings.extend(result.scanned_listings)
        self.zero_result_debug.extend(result.zero_result_debug)
        self.opportunities.extend(result.opportunities)
        if result.request_cap_reached:
            self.request_cap_reached = True

    def _attach_budget(self, client: EbayClient) -> None:
        if hasattr(client, "attach_request_budget"):
            client.attach_request_budget(self.request_budget)

    def _client_cap_reached(self, client: EbayClient) -> bool:
        if hasattr(client, "cap_reached"):
            return bool(client.cap_reached())
        return bool(getattr(client, "request_cap_reached", False)) or (
            int(getattr(client, "request_count", 0)) >= self.config.run.request_cap
        )

    def _client_total_requests(self, client: EbayClient) -> int:
        if hasattr(client, "total_request_count"):
            return int(client.total_request_count())
        return int(getattr(client, "request_count", 0))


def _send_alert_if_needed(config: AppConfig, listing_id: int, listing: Listing, evaluation: Evaluation) -> None:
    if not reserve_alert_send(config.db_path, listing_id, "discord"):
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
    if not sent:
        release_alert_send(config.db_path, listing_id, "discord")


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
    query = (target.query or "").strip()
    if query:
        return query
    if listing.title:
        return listing.title.strip() or "item"
    return "item"


def _listing_source(listing: Listing, *, fallback: str) -> str:
    if isinstance(listing.raw_json, dict):
        source = listing.raw_json.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return fallback


def _comps_stale(computed_at: Optional[str], ttl_hours: int) -> bool:
    if not computed_at:
        return True
    try:
        computed_time = datetime.fromisoformat(computed_at)
    except ValueError:
        return True
    if computed_time.tzinfo is None:
        computed_time = computed_time.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - computed_time
    return age.total_seconds() >= ttl_hours * 3600
