# eBay Flip Scanner

Streamlit app that scans eBay listings for underpriced items, estimates resale value using sold comps, and alerts you when expected profit meets your thresholds.

## Features
- Define multiple scan targets with filters (price caps, listing type, category, condition).
- Manual scan + auto-refresh scanning while the tab is open.
- Sold/completed comp lookup with median/p25/p75 stats.
- Profit, ROI, confidence, and deal score with explainable reasons.
- Discord webhook alerts for new deals.
- SQLite persistence for targets, listings, comps, evaluations, and alerts.
- HTML fallback when eBay API credentials are not available.
- Hierarchical category selection (Category → Subcategory → Sub-subcategory) with cached taxonomy.
- Fail-open retries when a target returns zero results, plus a “Why no results?” debug panel.

## Quick start
```bash
pip install -r requirements.txt
streamlit run app.py
```

## API credentials (optional)
The app uses the eBay Finding API when `EBAY_APP_ID` is available. Without credentials it falls back to HTML parsing with conservative limits.

```bash
export EBAY_APP_ID=your_app_id_here
export EBAY_OAUTH_TOKEN=your_taxonomy_oauth_token
export DISCORD_WEBHOOK_URL=your_discord_webhook
```

## Seed targets to try
- Nintendo Switch OLED
- AirPods Pro 2
- Sony WH-1000XM5

## Notes on rate limiting & caching
- Requests are rate limited with randomized delays (HTML mode) and exponential backoff on 429/5xx.
- Total request cap per scan defaults to 40 and is configurable in Settings.
- Responses are cached in a local SQLite HTTP cache (5 minute TTL) to avoid re-fetching.
- eBay categories are cached in SQLite after the first successful taxonomy load.

## Zero-results diagnostics & retries
- Each target records the request mode, query, filters, status code, and raw vs filtered counts.
- When a target yields zero listings, the app retries by removing category, condition, price filters,
  and finally broadening keywords (removing quotes, capacity, and color terms).
- The Dashboard includes a “Why no results?” panel that shows retry steps, rejection reasons,
  and the last request URL to help troubleshoot filters.

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
  data/
    ebay_categories_fallback.json
```
