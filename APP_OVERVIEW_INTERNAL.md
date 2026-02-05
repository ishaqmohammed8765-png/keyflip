# App Overview (Internal)

## What the app is
This is a Streamlit-based **eBay flip scanner** that lets you define buying targets, scan active listings, estimate resale value from sold comps, score each listing, and push deal alerts to Discord. It stores scan history and evaluation data in SQLite so results are retained across runs.

## Strengths
- **Clear end-to-end workflow in one app**: target management, scanning, comps, scoring, persistence, and alerts are all wired together, which makes it practical for solo usage without extra services.
- **Good resilience strategy**: request caps, randomized delays, retry/backoff behavior for transient errors, and local response caching reduce API pressure and improve stability.
- **Operational debugging is above average**: blocked/challenge detection and debug artifact capture make troubleshooting easier when eBay behavior changes.
- **Flexible sourcing mode**: supports API mode but also HTML parsing fallback when credentials are unavailable.
- **Pragmatic data model**: SQLite schema covers targets, listings, comps, evaluations, and alert deduping; this is enough structure for meaningful analytics later.
- **Reasonable test baseline**: focused unit tests around parsing/filtering/deal logic plus an optional live Playwright integration test.

## Weaknesses
- **UI module is large and mixed-concern**: `app.py` handles state, data loading, business flow, and rendering in one file; this will slow feature changes and make regressions likelier.
- **Retry path appears effectively disabled** for search broadening right now (`max_attempts = 1`), which may conflict with the intended fail-open behavior.
- **Single-process architecture limits scale**: Streamlit + SQLite is excellent for personal use but not ideal for concurrent users, high-frequency scans, or richer job scheduling.
- **Reliance on HTML scraping remains fragile**: even with Playwright fallback, markup changes and anti-bot controls can still break extraction.
- **Configuration governance is basic**: many settings are in code/session state and env vars; there is no profile/version layer for reproducible strategy setups.
- **Limited observability**: logging exists, but there is no metrics dashboard for hit-rate, block-rate, alert precision, or scan latency trends.

## Highest-impact improvements (priority order)
1. **Refactor `app.py` into modules** (UI components, orchestration, state/helpers). This improves maintainability and testability fastest.
2. **Re-enable true retry broadening** (category/condition/price/query relaxation) and add tests proving each relaxation stage executes.
3. **Add strategy profiles** (named presets for thresholds/filters/currency assumptions) saved in DB and exportable as JSON.
4. **Improve analytics view**: add per-target KPIs (scan count, hit rate, average expected profit, false-positive feedback loop).
5. **Strengthen scraping robustness**: parser contract tests with saved fixture HTML snapshots + automated regression checks.
6. **Add lightweight scheduling hardening**: optional background worker mode and lock to avoid overlapping scans.
7. **Add secrets/config UX**: settings validation and startup diagnostics for missing credentials/webhooks.

## Suggested 30-day plan
- **Week 1:** split app structure + add tests for new module boundaries.
- **Week 2:** implement and verify full retry relaxation behavior.
- **Week 3:** strategy profiles + improved settings UX.
- **Week 4:** KPI dashboard and parser fixture regression suite.

## Bottom line
For a personal flipping tool, this is already a strong practical base with thoughtful safeguards. The next major gains are not new features first—they are **maintainability, retry correctness, and measurable performance feedback**.
