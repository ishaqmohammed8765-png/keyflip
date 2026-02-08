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
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from flask import Flask, Response, jsonify, render_template_string
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
  <input type="text" id="searchBox" class="search-box" placeholder="Search titles..."
         oninput="filterTable()" />
  <a href="/api/export/csv" class="btn btn-secondary" download>Export CSV</a>
  <button class="btn" onclick="location.reload()">Refresh</button>
</div>

{% if items %}
<table id="flipTable">
<thead>
<tr>
  <th>Decision</th><th>Title</th><th>Buy</th><th>Resale</th>
  <th>Profit</th><th>ROI</th><th>Confidence</th><th>Score</th>
</tr>
</thead>
<tbody>
{% for item in items %}
<tr data-decision="{{ item.decision }}" data-title="{{ item.title|lower }}">
  <td><span class="tag tag-{{ item.decision }}">{{ item.decision }}</span></td>
  <td>
    {% if item.url %}<a href="{{ item.url }}" target="_blank">{{ item.title[:80] }}</a>
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
  const search = document.getElementById('searchBox').value.toLowerCase();
  document.querySelectorAll('#flipTable tbody tr').forEach(row => {
    const rowDecision = row.getAttribute('data-decision');
    const rowTitle = row.getAttribute('data-title');
    const matchDecision = decision === 'all' || rowDecision === decision;
    const matchSearch = !search || rowTitle.includes(search);
    row.style.display = matchDecision && matchSearch ? '' : 'none';
  });
}
// Auto-refresh every 60 seconds
setTimeout(() => location.reload(), 60000);
</script>
</body>
</html>
"""


def _load_scan_data() -> dict[str, Any] | None:
    if not LATEST_SCAN_PATH.exists():
        return None
    try:
        return json.loads(LATEST_SCAN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    entries = []
    try:
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


@app.route("/")
def dashboard():
    data = _load_scan_data()
    items = []
    deal_count = maybe_count = 0
    total_profit = 0.0
    last_scan = "-"
    zero_targets = []

    if data:
        items = data.get("items") or []
        scan_summary = data.get("scan_summary") or {}
        last_scan = data.get("generated_at", "-")[:19]
        zero_targets = scan_summary.get("zero_result_targets") or []

        decision_order = {"deal": 0, "maybe": 1, "ignore": 2}
        items.sort(key=lambda x: (decision_order.get(x.get("decision", "ignore"), 3), -(x.get("deal_score") or 0)))

        deal_count = sum(1 for i in items if i.get("decision") == "deal")
        maybe_count = sum(1 for i in items if i.get("decision") == "maybe")
        total_profit = sum(
            i.get("expected_profit_gbp", 0)
            for i in items
            if i.get("decision") in ("deal", "maybe") and i.get("expected_profit_gbp", 0) > 0
        )

    return render_template_string(
        DASHBOARD_HTML,
        data=data,
        items=items,
        deal_count=deal_count,
        maybe_count=maybe_count,
        total_profit=total_profit,
        total_items=len(items),
        last_scan=last_scan,
        zero_targets=zero_targets,
    )


@app.route("/api/latest")
def api_latest():
    data = _load_scan_data()
    if data is None:
        return jsonify({"error": "No scan data found"}), 404
    return jsonify(data)


@app.route("/api/health")
def api_health():
    data = _load_scan_data()
    healthy = data is not None
    scan_age_seconds = None
    if data and data.get("generated_at"):
        try:
            gen_time = datetime.fromisoformat(data["generated_at"])
            if gen_time.tzinfo is None:
                gen_time = gen_time.replace(tzinfo=timezone.utc)
            scan_age_seconds = (datetime.now(timezone.utc) - gen_time).total_seconds()
        except (ValueError, TypeError):
            pass
    return jsonify({
        "status": "ok" if healthy else "no_data",
        "scan_data_exists": healthy,
        "scan_age_seconds": scan_age_seconds,
        "items_count": len(data.get("items", [])) if data else 0,
    })


@app.route("/api/export/csv")
def api_export_csv():
    data = _load_scan_data()
    if data is None:
        return Response("No scan data available", status=404, mimetype="text/plain")

    items = data.get("items") or []
    output = io.StringIO()
    fieldnames = [
        "decision", "title", "url", "total_buy_gbp", "resale_est_gbp",
        "expected_profit_gbp", "roi", "confidence", "deal_score",
        "location", "listing_type", "evaluated_at", "reasons",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = dict(item)
        reasons = row.get("reasons") or []
        row["reasons"] = "; ".join(str(r) for r in reasons)
        writer.writerow(row)

    csv_bytes = output.getvalue().encode("utf-8")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=keyflip_scan.csv"},
    )


@app.route("/api/history")
def api_history():
    entries = _load_history()
    summary = []
    for entry in entries[-50:]:
        summary.append({
            "generated_at": entry.get("generated_at"),
            "count": entry.get("count", 0),
            "deals": (entry.get("scan_summary") or {}).get("deals", 0),
            "evaluated": (entry.get("scan_summary") or {}).get("evaluated", 0),
        })
    return jsonify({"history": summary})


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true")
    print(f"KeyFlip standalone server running on http://0.0.0.0:{port}")
    print("Endpoints: / (dashboard), /api/latest, /api/health, /api/export/csv, /api/history")
    app.run(host="0.0.0.0", port=port, debug=debug)
