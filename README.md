# Marketplace Flip Scanner

Streamlit app that automatically scans listings, compares them against multi-site sell comps, and ranks only opportunities with projected profit.

## Features
- Define multiple scan targets with filters (price caps, listing type, category, condition).
- Auto arbitrage mode with continuous scan cycles (`--watch`).
- Explicit buy marketplace vs sell marketplace comparison metadata.
- Multi-sell-source comps via `SELL_MARKETPLACE` (supports comma-separated values).
- Delivery-only filtering enabled by default.
- Automatic stale-item pruning so old listings are periodically removed.
- Parallel target scanning (`SCAN_WORKERS`) for faster throughput.
- Auto-targeting from popular categories plus smart target discovery from profitable scans.
- Live FX conversion cache for non-GBP pricing with static fallback.
- Sold/completed comp lookup with median/p25/p75 stats.
- Profit, ROI, confidence, and deal score with explainable reasons.
- Discord webhook alerts for new deals.
- SQLite persistence for targets, listings, comps, evaluations, and alerts.
- HTML fallback when eBay API credentials are not available.
- Hierarchical category selection (Category -> Subcategory -> Sub-subcategory) with cached taxonomy.
- Fail-open retries when a target returns zero results, plus a "Why no results?" debug panel.
- Dashboard filters for minimum score and minimum profit.
- Safer external-link handling (http/https only).
- Deal intelligence: max buy at target profit, break-even buy, suggested offer price, and buy-edge.
- Capital planner: pick a portfolio of actionable flips within your bankroll budget.
- Smart target auto-add: proposes and inserts new targets from recent high-confidence deals.

## Quick start
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Beginner quick win
1. Open **Manage Targets** and add 3 to 5 products you understand well.
   - Optional: use **Auto-Add Smart Targets** to let the app create new targets from strong recent results.
2. Start automatic scans: `python scanner/run_scan.py --watch`
3. In **Dashboard**, set:
   - `Minimum confidence`: `0.50`
   - `Decision`: `deal`
   - `Target profit per item`: `20` to `30` GBP
4. Only message sellers whose listing price is below **Max Buy @ Target**.
5. Use **Capital Plan** to avoid overspending your budget.

## Marketplace mode
The app defaults to buying from Craigslist and selling on eBay comps. You can configure both sides independently.

```bash
# Buy-side source for active deals
export MARKETPLACE=ebay

# Sell-side source(s) for comps (default: ebay,mercari,poshmark)
# Examples: ebay | mercari | poshmark | ebay,mercari,poshmark
export SELL_MARKETPLACE=ebay,mercari,poshmark

# Delivery-only mode (default is on)
export DELIVERY_ONLY=1

# Auto-delete listings older than this many hours (default: 72)
export LISTING_MAX_AGE_HOURS=72

# Parallel workers for scanning targets (default: 4)
export SCAN_WORKERS=4

# Auto-popular target seeding and smart discovery
export AUTO_POPULAR_TARGETS=1
export POPULAR_TARGETS_PER_CATEGORY=3
export AUTO_SMART_TARGETS=1

# Live FX conversion (fallback remains available)
export LIVE_FX_ENABLED=1
```

```bash
export DISCORD_WEBHOOK_URL=your_discord_webhook
```

## Seed targets to try
- Nintendo Switch OLED
- AirPods Pro 2
- Sony WH-1000XM5

## Notes on rate limiting and caching
- Requests are rate limited with randomized delays (HTML mode) and exponential backoff on 429/5xx.
- Total request cap per scan defaults to 60 and is configurable in settings.
- Responses are cached in a local SQLite HTTP cache (5 minute TTL) to avoid re-fetching.

## Zero-results diagnostics and retries
- Each target records the request mode, query, filters, status code, and raw vs filtered counts.
- When a target yields zero listings, the app retries by removing category, condition, price filters,
  and finally broadening keywords (removing quotes, capacity, and color terms).
- The dashboard includes a "Why no results?" panel that shows retry steps, rejection reasons,
  and the last request URL to help troubleshoot filters.
- When eBay serves a human verification challenge, debug artifacts (HTML, metadata, and screenshots when available)
  are written to `.cache/ebayflip_debug/` and surfaced in the UI.

## Standalone server API filters
`serve.py` supports query filtering on `GET /api/latest`:
- `decision`: `deal|maybe|ignore|All`
- `q`: title search text
- `min_score`: minimum deal score
- `min_profit`: minimum expected profit
- `target_profit`: target profit used for max-buy calculations

Standalone server scan trigger (when Streamlit is down):
- `POST /api/scan/run` to run a fresh scan cycle from Flask.

## Category selection
- The Targets form uses a dropdown-driven category tree (up to 3 levels deep).
- Category IDs are stored internally; users select human-readable category names only.
- If taxonomy credentials are missing, the app loads a small bundled fallback list.
- If taxonomy loading fails, category selection is hidden and scans run with keywords only.

## Limitations
- HTML fallback parsing is best-effort and can miss some data.
- Currency conversion uses a configurable fixed rate for non-GBP listings.
- Alerts only fire for new DEAL decisions.

## Project structure
```
app.py
serve.py
scanner/run_scan.py
requirements.txt
README.md
ebayflip/
  __init__.py
  config.py
  db.py
  ebay_client.py
  comps.py
  scoring.py
  alerts.py
  scheduler.py
  models.py
  taxonomy.py
  dashboard_data.py
  safety.py
  data/
    ebay_categories_fallback.json
```
