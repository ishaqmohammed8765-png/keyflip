"""Standalone Flask server - works when Streamlit is down.

Run with:
    python serve.py
    # or
    FLASK_PORT=8080 python serve.py

Serves the same scan data as the Streamlit dashboard, plus:
    GET /            - HTML dashboard
    GET /api/latest  - JSON scan data
    GET /api/health  - Health check
    GET /api/export/csv - Download scan results as CSV
    POST /api/scan/run - Trigger a fresh scan cycle
"""
from __future__ import annotations

import os
import hmac
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from flask import Flask, Response, jsonify, render_template_string, request
except ImportError:
    raise SystemExit(
        "Flask is required for the standalone server.\n"
        "Install it with: pip install flask\n"
        "Or use the Streamlit dashboard instead: streamlit run app.py"
    )

ROOT_DIR = Path(__file__).parent
LATEST_SCAN_PATH = ROOT_DIR / "data" / "latest.json"
HISTORY_PATH = ROOT_DIR / "data" / "history.jsonl"

app = Flask(__name__)
_SCAN_LOCK = threading.Lock()
_LAST_SCAN_TRIGGER_MONO = 0.0

from ebayflip.dashboard_data import (
    filter_items,
    items_to_csv_bytes,
    load_history,
    load_latest_scan,
    scan_age_seconds,
    sort_items,
    summarize_items,
)
from ebayflip.deal_insights import enrich_items
from ebayflip.config import RunSettings
from ebayflip.safety import safe_external_url

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>KeyFlip Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 1.5rem; }
  h1 { font-size: 1.8rem; margin-bottom: 0.3rem; }
  .subtitle { color: #94a3b8; font-size: 0.9rem; margin-bottom: 1.5rem; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
  .card { background: linear-gradient(135deg, #1e293b, #334155); border: 1px solid #475569;
          border-radius: 12px; padding: 1rem; }
  .card h4 { color: #94a3b8; font-size: 0.8rem; letter-spacing: 0.03em; text-transform: uppercase; }
  .card .value { font-size: 1.4rem; font-weight: 700; margin-top: 0.3rem; }
  .toolbar { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem; }
  select, input { background: #1e293b; color: #e2e8f0; border: 1px solid #475569;
                  padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.9rem; }
  .btn { background: #3b82f6; color: white; border: none; padding: 0.5rem 1rem;
         border-radius: 6px; cursor: pointer; font-size: 0.85rem; text-decoration: none; }
  .btn:hover { background: #2563eb; }
  .btn-secondary { background: #475569; }
  .btn-secondary:hover { background: #64748b; }
  table { width: 100%%; border-collapse: collapse; margin-top: 1rem; }
  th { text-align: left; padding: 0.6rem 0.8rem; background: #1e293b; color: #94a3b8;
       font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em;
       border-bottom: 2px solid #334155; position: sticky; top: 0; }
  td { padding: 0.6rem 0.8rem; border-bottom: 1px solid #1e293b; font-size: 0.9rem; }
  tr:hover { background: #1e293b; }
  .tag { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px;
         font-size: 0.7rem; font-weight: 600; text-transform: uppercase; }
  .tag-deal { background: #16a34a; color: white; }
  .tag-maybe { background: #d97706; color: white; }
  .tag-ignore { background: #6b7280; color: white; }
  a { color: #60a5fa; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .details { color: #94a3b8; font-size: 0.8rem; margin-top: 0.3rem; }
  .diagnostics { background: #1e293b; border: 1px solid #475569; border-radius: 8px;
                 padding: 1rem; margin-bottom: 1.5rem; }
  .diagnostics summary { cursor: pointer; color: #f59e0b; font-weight: 600; }
  .diag-item { margin: 0.5rem 0; padding: 0.5rem; background: #0f172a; border-radius: 4px; }
  .search-box { flex: 1; min-width: 200px; }
  .status-bar { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 1rem; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%%; display: inline-block; }
  .status-ok { background: #16a34a; }
  .status-warn { background: #d97706; }
  .profit-positive { color: #4ade80; }
  .profit-negative { color: #f87171; }
  .empty-state { text-align: center; padding: 3rem; color: #94a3b8; }
  @media (max-width: 768px) {
    body { padding: 0.75rem; }
    .cards { grid-template-columns: 1fr 1fr; }
    td, th { padding: 0.4rem; font-size: 0.8rem; }
  }
</style>
</head>
<body>
<h1>KeyFlip</h1>
<p class="subtitle">Marketplace flip scanner &mdash; standalone mode (Streamlit-free)</p>
<p class="subtitle">{{ market_label }}</p>

<div class="status-bar">
  <span class="status-dot {{ 'status-ok' if data else 'status-warn' }}"></span>
  <span>{{ 'Data loaded' if data else 'No data' }} &mdash;
    Last scan: {{ last_scan }}</span>
</div>

<div class="cards">
  <div class="card"><h4>Deals Found</h4><div class="value">{{ deal_count }}</div></div>
  <div class="card"><h4>Maybe Deals</h4><div class="value">{{ maybe_count }}</div></div>
  <div class="card"><h4>Potential Profit</h4><div class="value">&pound;{{ "%.2f"|format(total_profit) }}</div></div>
  <div class="card"><h4>Total Scanned</h4><div class="value">{{ total_items }}</div></div>
</div>

{% if zero_targets %}
<details class="diagnostics">
  <summary>{{ zero_targets|length }} target(s) returned no results</summary>
  {% for zt in zero_targets %}
  <div class="diag-item">
    <strong>{{ zt.target_name }}</strong> (query: <code>{{ zt.target_query }}</code>)
    {% if zt.blocked_reason %}<br/><span style="color:#f87171">Blocked: {{ zt.blocked_reason }}</span>{% endif %}
    {% for note in zt.get('retry_report', []) %}<br/><small>{{ note }}</small>{% endfor %}
  </div>
  {% endfor %}
</details>
{% endif %}

<div class="toolbar">
  <select id="filterDecision" onchange="filterTable()">
    <option value="all">All decisions</option>
    <option value="deal">Deals only</option>
    <option value="maybe">Maybe only</option>
    <option value="ignore">Ignore only</option>
  </select>
  <input type="number" id="minScore" min="0" max="1000" step="0.1" placeholder="Min score"
         oninput="filterTable()" />
  <input type="number" id="minProfit" step="1" placeholder="Min profit GBP"
         oninput="filterTable()" />
  <input type="number" id="minConfidence" min="0" max="1" step="0.05" placeholder="Min confidence"
         oninput="filterTable()" />
  <input type="text" id="searchBox" class="search-box" placeholder="Search titles..."
         oninput="filterTable()" />
  <a href="/api/export/csv" class="btn btn-secondary" download>Export CSV</a>
  <a href="/api/latest?decision=deal&min_score=40" class="btn btn-secondary">Deals API</a>
  <button class="btn" onclick="location.reload()">Refresh</button>
</div>

{% if items %}
<table id="flipTable">
<thead>
<tr>
  <th>Decision</th><th>Title</th><th>Buy</th><th>Resale</th>
  <th>Profit</th><th>ROI</th><th>Confidence</th><th>Score</th>
  <th>Grade</th><th>Risk</th><th>Max Buy</th><th>Edge</th>
</tr>
</thead>
<tbody>
{% for item in items %}
<tr data-decision="{{ item.decision }}" data-title="{{ item.title|lower }}"
    data-score="{{ item.deal_score or 0 }}" data-profit="{{ item.expected_profit_gbp or 0 }}"
    data-confidence="{{ item.confidence or 0 }}">
  <td><span class="tag tag-{{ item.decision }}">{{ item.decision }}</span></td>
  <td>
    {% if item.safe_url %}<a href="{{ item.safe_url }}" target="_blank" rel="noopener noreferrer">{{ item.title[:80] }}</a>
    {% else %}{{ item.title[:80] }}{% endif %}
    {% if item.reasons %}
    <div class="details">{{ item.reasons[:2]|join(' | ') }}</div>
    {% endif %}
  </td>
  <td>&pound;{{ "%.2f"|format(item.total_buy_gbp or 0) }}</td>
  <td>&pound;{{ "%.2f"|format(item.resale_est_gbp or 0) }}</td>
  <td class="{{ 'profit-positive' if (item.expected_profit_gbp or 0) > 0 else 'profit-negative' }}">
    &pound;{{ "%.2f"|format(item.expected_profit_gbp or 0) }}</td>
  <td>{{ "%.0f"|format((item.roi or 0) * 100) }}%%</td>
  <td>{{ "%.2f"|format(item.confidence or 0) }}</td>
  <td>{{ "%.1f"|format(item.deal_score or 0) }}</td>
  <td>{{ item.flip_grade or '-' }}</td>
  <td>{{ item.risk_band or '-' }}</td>
  <td>&pound;{{ "%.2f"|format(item.max_total_buy_target_gbp or 0) }}</td>
  <td>&pound;{{ "%.2f"|format(item.buy_edge_gbp or 0) }}</td>
</tr>
{% endfor %}
</tbody>
</table>
{% else %}
<div class="empty-state">
  <p>No items found in the latest scan.</p>
  <p>Run <code>python scanner/run_scan.py</code> to populate data.</p>
</div>
{% endif %}

<script>
function filterTable() {
  const decision = document.getElementById('filterDecision').value;
  const minScore = parseFloat(document.getElementById('minScore').value || '0');
  const minProfitRaw = document.getElementById('minProfit').value;
  const minProfit = minProfitRaw === '' ? null : parseFloat(minProfitRaw);
  const minConfidenceRaw = document.getElementById('minConfidence').value;
  const minConfidence = minConfidenceRaw === '' ? 0 : parseFloat(minConfidenceRaw);
  const search = document.getElementById('searchBox').value.toLowerCase();
  document.querySelectorAll('#flipTable tbody tr').forEach(row => {
    const rowDecision = row.getAttribute('data-decision');
    const rowTitle = row.getAttribute('data-title');
    const rowScore = parseFloat(row.getAttribute('data-score') || '0');
    const rowProfit = parseFloat(row.getAttribute('data-profit') || '0');
    const rowConfidence = parseFloat(row.getAttribute('data-confidence') || '0');
    const matchDecision = decision === 'all' || rowDecision === decision;
    const matchSearch = !search || rowTitle.includes(search);
    const matchScore = rowScore >= minScore;
    const matchProfit = minProfit === null || rowProfit >= minProfit;
    const matchConfidence = rowConfidence >= minConfidence;
    row.style.display = matchDecision && matchSearch && matchScore && matchProfit && matchConfidence ? '' : 'none';
  });
}
// Auto-refresh every 60 seconds
setTimeout(() => location.reload(), 60000);
</script>
</body>
</html>
"""


def _to_render_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        row["safe_url"] = safe_external_url(item.get("url"))
        rendered.append(row)
    return rendered


def _query_float(name: str) -> float | None:
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _scan_trigger_enabled() -> bool:
    return os.getenv("SCAN_RUN_ENABLED", "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _extract_scan_token() -> str | None:
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    token = request.headers.get("X-Scan-Token", "").strip()
    return token or None


def _scan_token_valid() -> bool:
    expected = os.getenv("SCAN_RUN_TOKEN", "").strip()
    provided = _extract_scan_token() or ""
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _acquire_scan_slot() -> bool:
    global _LAST_SCAN_TRIGGER_MONO
    min_interval = max(1, int(os.getenv("SCAN_RUN_MIN_INTERVAL_SECONDS", "120")))
    now = time.monotonic()
    with _SCAN_LOCK:
        elapsed = now - _LAST_SCAN_TRIGGER_MONO
        if elapsed < min_interval:
            return False
        _LAST_SCAN_TRIGGER_MONO = now
        return True


@app.route("/")
def dashboard():
    data = load_latest_scan(LATEST_SCAN_PATH)
    items: list[dict[str, Any]] = []
    last_scan = "-"
    zero_targets = []
    market_label = "Buy: ebay -> Sell comps: ebay"
    summary_stats = {"deal_count": 0, "maybe_count": 0, "total_profit": 0.0, "total_items": 0}

    if data:
        items = enrich_items(
            sort_items(data.get("items") or []),
            RunSettings.from_env(),
            target_profit_gbp=float(os.getenv("FLIP_TARGET_PROFIT", "20")),
        )
        scan_summary = data.get("scan_summary") or {}
        marketplaces = data.get("marketplaces") or {}
        buy_market = marketplaces.get("buy") or scan_summary.get("buy_marketplace") or "buy"
        sell_market = marketplaces.get("sell") or scan_summary.get("sell_marketplace") or "sell"
        market_label = f"Buy: {buy_market} -> Sell comps: {sell_market}"
        last_scan = data.get("generated_at", "-")[:19]
        zero_targets = scan_summary.get("zero_result_targets") or []
        summary_stats = summarize_items(items)

    return render_template_string(
        DASHBOARD_HTML,
        data=data,
        items=_to_render_items(items),
        deal_count=summary_stats["deal_count"],
        maybe_count=summary_stats["maybe_count"],
        total_profit=summary_stats["total_profit"],
        total_items=summary_stats["total_items"],
        last_scan=last_scan,
        market_label=market_label,
        zero_targets=zero_targets,
    )


@app.route("/api/latest")
def api_latest():
    data = load_latest_scan(LATEST_SCAN_PATH)
    if data is None:
        return jsonify({"error": "No scan data found"}), 404
    decision_raw = request.args.get("decision", "All")
    decision = decision_raw.strip().lower()
    if decision in {"all", ""}:
        decision_normalized = "All"
    elif decision in {"deal", "maybe", "ignore"}:
        decision_normalized = decision
    else:
        decision_normalized = "All"
    search = request.args.get("q", "")
    min_score = _query_float("min_score") or 0.0
    min_profit = _query_float("min_profit")
    target_profit = _query_float("target_profit")
    if target_profit is None:
        target_profit = float(os.getenv("FLIP_TARGET_PROFIT", "20"))
    run_settings = RunSettings.from_env()
    source_items = enrich_items(
        sort_items(data.get("items") or []),
        run_settings,
        target_profit_gbp=target_profit,
    )
    items = filter_items(
        source_items,
        decision=decision_normalized,
        search_term=search,
        min_score=min_score,
        min_profit=min_profit,
    )
    payload = dict(data)
    payload["items"] = _to_render_items(items)
    payload["count"] = len(items)
    payload["filters"] = {
        "decision": decision_normalized,
        "q": search,
        "min_score": min_score,
        "min_profit": min_profit,
        "target_profit": target_profit,
    }
    return jsonify(payload)


@app.route("/api/health")
def api_health():
    data = load_latest_scan(LATEST_SCAN_PATH)
    healthy = data is not None
    age_seconds = scan_age_seconds(data)
    return jsonify({
        "status": "ok" if healthy else "no_data",
        "scan_data_exists": healthy,
        "scan_age_seconds": age_seconds,
        "items_count": len(data.get("items", [])) if data else 0,
    })


@app.route("/api/export/csv")
def api_export_csv():
    data = load_latest_scan(LATEST_SCAN_PATH)
    if data is None:
        return Response("No scan data available", status=404, mimetype="text/plain")

    csv_bytes = items_to_csv_bytes(sort_items(data.get("items") or []))
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=keyflip_scan.csv"},
    )


@app.route("/api/history")
def api_history():
    entries = load_history(HISTORY_PATH)
    summary = []
    for entry in entries[-50:]:
        summary.append({
            "generated_at": entry.get("generated_at"),
            "count": entry.get("count", 0),
            "deals": (entry.get("scan_summary") or {}).get("deals", 0),
            "evaluated": (entry.get("scan_summary") or {}).get("evaluated", 0),
        })
    return jsonify({"history": summary})


@app.route("/api/scan/run", methods=["POST"])
def api_scan_run():
    if not _scan_trigger_enabled():
        return jsonify({"error": "Scan trigger endpoint disabled"}), 403
    if not _scan_token_valid():
        return jsonify({"error": "Unauthorized"}), 401
    if not _acquire_scan_slot():
        return jsonify({"error": "Rate limited; try again later"}), 429

    scanner_path = ROOT_DIR / "scanner" / "run_scan.py"
    if not scanner_path.exists():
        return jsonify({"error": "scanner/run_scan.py not found"}), 500
    cmd = [sys.executable, str(scanner_path), "--max-cycles", "1"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Scan timed out after 600s"}), 504
    payload = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": cmd,
    }
    if os.getenv("SCAN_RUN_VERBOSE_RESPONSE", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        payload["stdout_tail"] = (proc.stdout or "")[-4000:]
        payload["stderr_tail"] = (proc.stderr or "")[-4000:]
    return (jsonify(payload), 200) if proc.returncode == 0 else (jsonify(payload), 500)


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true")
    print(f"KeyFlip standalone server running on http://{host}:{port}")
    print("Endpoints: / (dashboard), /api/latest, /api/health, /api/export/csv, /api/history, /api/scan/run")
    app.run(host=host, port=port, debug=debug)
