"""Self-contained HTML dashboard renderer.

Produces a single .html file with no external dependencies (no CDN, no network):
inline CSS + inline vanilla-JS SVG charts, so it opens in any browser and can be
published straight to GitHub Pages. Theme-aware (light/dark), colorblind-safe
categorical palette (validated via the dataviz method), with hover tooltips and a
full data table for accessibility.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path

from ..aggregate import WeeklyMetrics, group_by_week

# Where to look for a brand logo to embed at the top of the dashboard. First hit
# wins. Override with the DASHBOARD_LOGO env var. Embedded as a data URI so the
# page stays fully self-contained (works on GitHub Pages, offline, etc.).
_LOGO_CANDIDATES = ["assets/logo.png", "assets/logo.svg", "assets/logo.jpg", "assets/logo.webp"]

# Validated categorical palette (dataviz reference instance): light + dark steps.
_SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
_SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _location_order(metrics: list[WeeklyMetrics]) -> list[str]:
    """Location names ordered by total net sales (biggest first). This order
    drives the color slots, so every location keeps the same color on the
    overview page and on its own page."""
    totals: dict[str, float] = {}
    for m in metrics:
        totals[m.location_name] = totals.get(m.location_name, 0.0) + m.net_sales
    return sorted(totals, key=lambda n: totals[n], reverse=True)


def _build_payload(
    metrics: list[WeeklyMetrics],
    title: str,
    scope_label: str,
    loc_index: dict[str, int],
    nav: list[dict],
    active_slug: str,
    daily_rows: list[dict],
    labor_by_role: dict | None,
    kpi_ctx: dict,
) -> dict:
    weeks = group_by_week(metrics)
    week_labels = [w.week_start.isoformat() for w in weeks]

    # Locations present in *this* page's metrics, ordered by their global slot.
    present = sorted({m.location_name for m in metrics}, key=lambda n: loc_index.get(n, 999))

    # Per-location series aligned to week_labels (None where no data that week).
    def series(metric: str) -> list[dict]:
        out = []
        for name in present:
            values: list[float | None] = [None] * len(weeks)
            for wi, w in enumerate(weeks):
                for r in w.rows:
                    if r.location_name == name:
                        values[wi] = round(float(getattr(r, metric)), 4)
            out.append({"name": name, "slot": loc_index.get(name, 0), "values": values})
        return out

    return {
        "title": title,
        "scopeLabel": scope_label,
        "nav": nav,
        "activeSlug": active_slug,
        "weeks": week_labels,
        "locations": present,
        "seriesLight": _SERIES_LIGHT,
        "seriesDark": _SERIES_DARK,
        "charts": {
            "netSales": series("net_sales"),
            "laborPct": series("labor_pct"),
            "transactions": series("transaction_count"),
            "salesPerLaborHour": series("sales_per_labor_hour"),
        },
        "table": _table_rows(weeks),
        # Two KPI rows: current (in-progress) week and prior completed week.
        "kpiRows": _kpi_rows(present, kpi_ctx),
        # Overview-only: SPLH by location across weeks, with avg + first->last change.
        "splhMatrix": _splh_matrix(weeks, present),
        # Overview-only: labor hours by role x location for the latest week.
        "laborByRole": labor_by_role,
        # Location-only: per-day Sales / Labor hours / SPLH.
        "daily": daily_rows,
        # When the report was last built/synced (San Diego time).
        "syncedAt": kpi_ctx.get("synced_at", ""),
        # Downloadable report links (Excel workbook covers all locations).
        "downloads": kpi_ctx.get("downloads", {}),
    }


def _splh_matrix(weeks, present: list[str]) -> dict:
    """Sales-per-labor-hour for each location across the weeks, plus the
    multi-week average and the first->last percentage change."""
    rows = []
    for name in present:
        values: list[float | None] = []
        for w in weeks:
            v = None
            for r in w.rows:
                if r.location_name == name:
                    v = round(r.sales_per_labor_hour, 2)
            values.append(v)
        got = [v for v in values if v is not None]
        avg = round(sum(got) / len(got), 2) if got else None
        change = None
        if len(got) >= 2 and got[0]:
            change = round((got[-1] - got[0]) / got[0], 4)
        rows.append({"name": name, "values": values, "avg": avg, "change": change})
    return {"weeks": [w.week_start.isoformat() for w in weeks], "rows": rows}


def _table_rows(weeks) -> list[dict]:
    rows = []
    for w in weeks:
        for r in w.rows:
            rows.append(
                {
                    "week": r.week_start.isoformat(),
                    "location": r.location_name,
                    "netSales": round(r.net_sales, 2),
                    "transactions": r.transaction_count,
                    "avgCheck": round(r.avg_check, 2),
                    "laborHours": round(r.labor_hours, 1),
                    "laborCost": round(r.labor_cost, 2),
                    "laborPct": round(r.labor_pct, 4),
                    "salesPerLaborHour": round(r.sales_per_labor_hour, 2),
                }
            )
    return rows


_KPI_KEYS = ["netSales", "transactions", "laborCost", "laborPct", "salesPerLaborHour"]


def _kpi_rows(present: list[str], kpi_ctx: dict) -> dict:
    """Two KPI rows for this page's locations: the current (in-progress) week
    with deltas vs the same days of the prior week, and the prior completed week
    with deltas vs the week before it."""
    by_loc = kpi_ctx.get("by_loc", {})
    dates = kpi_ctx.get("dates", {})

    def window_sum(key: str) -> dict:
        tot = {"netSales": 0.0, "transactions": 0, "laborCost": 0.0, "laborHours": 0.0}
        for name in present:
            w = by_loc.get(name, {}).get(key)
            if not w:
                continue
            for k in tot:
                tot[k] += w.get(k, 0)
        return tot

    def metrics_of(t: dict) -> dict:
        net, hours = t["netSales"], t["laborHours"]
        return {
            "netSales": net,
            "transactions": t["transactions"],
            "laborCost": t["laborCost"],
            "laborPct": (t["laborCost"] / net) if net else 0.0,
            "salesPerLaborHour": (net / hours) if hours else 0.0,
        }

    def deltas(m: dict, base: dict) -> dict:
        return {k: ((m[k] - base[k]) / base[k]) if base[k] else None for k in _KPI_KEYS}

    cur_t, prior_same = window_sum("current"), window_sum("priorSame")
    prior_t, prior_prev = window_sum("prior"), window_sum("priorPrev")
    cur_m, prior_m = metrics_of(cur_t), metrics_of(prior_t)

    rows: dict = {
        "prior": {"label": "Prior week", "dates": dates.get("prior", ""),
                  **prior_m, "deltas": deltas(prior_m, metrics_of(prior_prev))},
    }
    if kpi_ctx.get("has_current") and (cur_t["netSales"] or cur_t["laborHours"]):
        rows["current"] = {"label": "Current week", "dates": dates.get("current", ""),
                           **cur_m, "deltas": deltas(cur_m, metrics_of(prior_same))}
    return rows


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "location"


def _logo_data_uri() -> str:
    """Return a data: URI for the brand logo, or "" if none is present."""
    env = os.getenv("DASHBOARD_LOGO", "").strip()
    for candidate in ([env] if env else []) + _LOGO_CANDIDATES:
        p = Path(candidate)
        if p.is_file():
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{data}"
    return ""


def _logo_block(logo_uri: str) -> str:
    if not logo_uri:
        return ""
    return f'<div class="brand"><img class="logo" src="{logo_uri}" alt="" /></div>'


def _write_page(
    path: Path,
    metrics: list[WeeklyMetrics],
    title: str,
    scope_label: str,
    loc_index: dict[str, int],
    nav: list[dict],
    active_slug: str,
    logo_uri: str,
    daily_rows: list[dict],
    labor_by_role: dict | None,
    kpi_ctx: dict,
) -> None:
    payload = _build_payload(
        metrics, title, scope_label, loc_index, nav, active_slug, daily_rows,
        labor_by_role, kpi_ctx,
    )
    page_title = title if active_slug == "index" else f"{title} — {scope_label}"
    html = (
        _HTML_TEMPLATE.replace("__TITLE__", _escape(page_title))
        .replace("__LOGO_BLOCK__", _logo_block(logo_uri))
        .replace("__DATA__", json.dumps(payload))
    )
    path.write_text(html, encoding="utf-8")


def render_dashboard(
    metrics: list[WeeklyMetrics],
    out_path: str | Path,
    title: str,
    current_daily: list | None = None,
    labor_by_role=None,
    kpi_by_loc: dict | None = None,
    kpi_dates: dict | None = None,
    has_current: bool = False,
    synced_at: str = "",
    downloads: dict | None = None,
) -> Path:
    """Write the overview page (index.html) plus one page per location, all in
    the same directory and cross-linked by a button nav. Returns the index path."""
    out_path = Path(out_path)
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    loc_names = _location_order(metrics)
    loc_index = {name: i for i, name in enumerate(loc_names)}
    kpi_ctx = {
        "by_loc": kpi_by_loc or {},
        "dates": kpi_dates or {},
        "has_current": has_current,
        "synced_at": synced_at,
        "downloads": downloads or {},
    }

    # Current-week daily rows grouped by location (chronological, Mon first).
    daily_by_loc: dict[str, list[dict]] = {}
    for d in current_daily or []:
        daily_by_loc.setdefault(d.location_name, []).append(_daily_row(d))
    for rows in daily_by_loc.values():
        rows.sort(key=lambda r: r["day"])  # chronological

    # Unique slug per location (guard against collisions).
    slugs: dict[str, str] = {}
    used: set[str] = {"index"}
    for name in loc_names:
        base = _slug(name)
        s, n = base, 2
        while s in used:
            s, n = f"{base}-{n}", n + 1
        used.add(s)
        slugs[name] = s

    nav = [{"label": "All Locations", "href": "index.html", "slug": "index"}]
    nav += [{"label": name, "href": f"{slugs[name]}.html", "slug": slugs[name]} for name in loc_names]

    logo_uri = _logo_data_uri()
    role_payload = _labor_by_role_payload(labor_by_role, loc_names)

    # Overview (all locations) — daily board is location-specific (empty here);
    # the labor-by-role matrix is a cross-location board (overview only).
    _write_page(
        out_dir / "index.html", metrics, title, "All Locations", loc_index, nav,
        "index", logo_uri, [], role_payload, kpi_ctx,
    )

    # One page per location, with its own current-week table and role matrix
    # (that single location as the only column).
    for name in loc_names:
        loc_metrics = [m for m in metrics if m.location_name == name]
        _write_page(
            out_dir / f"{slugs[name]}.html", loc_metrics, title, name, loc_index, nav,
            slugs[name], logo_uri, daily_by_loc.get(name, []),
            _labor_by_role_payload(labor_by_role, [name]), kpi_ctx,
        )

    return out_dir / "index.html"


def _labor_by_role_payload(labor_by_role, loc_names: list[str]) -> dict | None:
    """Shape the LaborByRole aggregate into a JSON-friendly matrix, with
    locations as columns ordered like the rest of the dashboard."""
    if not labor_by_role or not labor_by_role.hours:
        return None
    cols = [n for n in loc_names if n in labor_by_role.hours]
    return {
        "weekOf": labor_by_role.week_start.isoformat(),
        "roles": labor_by_role.role_order,
        "locations": cols,
        "cells": {loc: labor_by_role.hours.get(loc, {}) for loc in cols},
        "totals": {loc: round(sum(labor_by_role.hours.get(loc, {}).values()), 1) for loc in cols},
    }


_WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _daily_row(d) -> dict:
    return {
        "day": d.business_date.isoformat(),
        "dow": _WEEKDAY[d.business_date.weekday()],
        "netSales": round(d.net_sales, 2),
        "laborHours": round(d.labor_hours, 2),
        "salesPerLaborHour": round(d.sales_per_labor_hour, 2),
    }


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    color-scheme: light dark;
    --page: #f9f9f7; --surface: #fcfcfb; --border: rgba(11,11,11,0.10);
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --axis: #c3c2b7;
    --good: #006300; --bad: #d03b3b; --accent: #2a78d6;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      --page: #0d0d0d; --surface: #1a1a19; --border: rgba(255,255,255,0.10);
      --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --axis: #383835;
      --good: #0ca30c; --bad: #d03b3b; --accent: #3987e5;
    }
  }
  :root[data-theme="dark"] {
    --page: #0d0d0d; --surface: #1a1a19; --border: rgba(255,255,255,0.10);
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835;
    --good: #0ca30c; --bad: #d03b3b; --accent: #3987e5;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--page); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    line-height: 1.4;
  }
  .wrap { max-width: 1160px; margin: 0 auto; padding: 28px 20px 64px; }
  .brand { text-align: center; margin: 0 0 20px; }
  .brand .logo { height: 84px; width: auto; max-width: min(90%, 320px); object-fit: contain; }
  header { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 22px; margin: 0; }
  .sub { color: var(--ink-2); font-size: 13px; margin-top: 4px; }
  .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  button.theme, .btn {
    background: var(--surface); color: var(--ink-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 12px; font-size: 13px; cursor: pointer;
    text-decoration: none; white-space: nowrap; line-height: 1.4;
  }
  .btn:hover, button.theme:hover { border-color: var(--accent); color: var(--ink); }
  #downloadExcel { color: #fff; background: var(--accent); border-color: transparent; }
  #downloadExcel:hover { color: #fff; filter: brightness(1.05); }
  .nav { display: flex; flex-wrap: wrap; gap: 8px; margin: 18px 0 6px; }
  .nav a {
    text-decoration: none; font-size: 13px; padding: 7px 14px; border-radius: 999px;
    border: 1px solid var(--border); color: var(--ink-2); background: var(--surface);
    white-space: nowrap;
  }
  .nav a:hover { border-color: var(--accent); color: var(--ink); }
  .nav a.active { background: var(--accent); color: #fff; border-color: transparent; }
  .kpi-head { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; font-weight: 700; margin: 20px 0 8px; }
  .synced { font-size: 11px; color: var(--muted); margin: -2px 0 8px; }
  .synced .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--good); margin-right: 6px; vertical-align: middle; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 0 0 8px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
  .tile .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .tile .value { font-size: 26px; font-weight: 650; margin-top: 6px; }
  .tile .delta { font-size: 12px; margin-top: 6px; }
  .delta.up { color: var(--good); } .delta.down { color: var(--bad); } .delta.flat { color: var(--muted); }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; margin: 16px 0; }
  .card h2 { font-size: 15px; margin: 0 0 4px; }
  .card .hint { font-size: 12px; color: var(--muted); margin: 0 0 8px; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px 18px; margin: 4px 0 10px; font-size: 12.5px; color: var(--ink-2); }
  .legend .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: middle; }
  .chart { width: 100%; overflow-x: auto; }
  svg { display: block; width: 100%; height: auto; }
  .tt { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
        border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px;
        box-shadow: 0 4px 14px rgba(0,0,0,.18); opacity: 0; transition: opacity .08s; z-index: 20; max-width: 260px; }
  .tt .row { display: flex; justify-content: space-between; gap: 14px; }
  .tt .row .k { color: var(--ink-2); } .tt .row .v { font-variant-numeric: tabular-nums; }
  .tt .hd { font-weight: 650; margin-bottom: 4px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 7px 10px; text-align: right; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; font-variant-numeric: normal; }
  /* Matrix/daily tables: only the first column is a label; the rest are numbers. */
  #splhTable th:nth-child(2), #splhTable td:nth-child(2),
  #dailyTable th:nth-child(2), #dailyTable td:nth-child(2),
  #roleTable th:nth-child(2), #roleTable td:nth-child(2) { text-align: right; font-variant-numeric: tabular-nums; }
  #roleTable tr.total td { border-top: 2px solid var(--axis); }
  .muted { color: var(--muted); }
  thead th { position: sticky; top: 0; background: var(--surface); color: var(--ink-2); font-weight: 600; }
  .tablewrap { max-height: 460px; overflow: auto; }
  details summary { cursor: pointer; font-size: 14px; font-weight: 600; padding: 6px 0; }
  .foot { color: var(--muted); font-size: 12px; margin-top: 28px; }
  @media (max-width: 560px) { .tile .value { font-size: 22px; } }
  @media print {
    /* Print/PDF: keep the brand palette, drop the chrome, and avoid splitting
       a card across pages. */
    :root { --page: #fff; --surface: #fff; --ink: #0b0b0b; --ink-2: #333; }
    html, body { background: #fff; }
    * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .wrap { max-width: none; padding: 0 8px; }
    .toolbar, .nav, #tooltip, button.theme { display: none !important; }
    .card, section { break-inside: avoid; page-break-inside: avoid; }
    .tablewrap { max-height: none; overflow: visible; }
    thead th { position: static; }
    .card, .tile { box-shadow: none; }
    a[href]::after { content: ""; }
  }
</style>
</head>
<body>
<div class="wrap">
  __LOGO_BLOCK__
  <header>
    <div>
      <h1 id="title"></h1>
      <div class="sub" id="subtitle"></div>
    </div>
    <div class="toolbar">
      <a class="btn" id="downloadExcel" download hidden>⤓ Excel</a>
      <button class="btn" id="printBtn" type="button">⎙ Print / PDF</button>
      <button class="theme" id="themeToggle" type="button">Toggle theme</button>
    </div>
  </header>

  <nav class="nav" id="nav" aria-label="Locations"></nav>

  <section id="kpi-current" aria-label="Current week summary" style="display:none">
    <div class="kpi-head" id="kpi-current-head"></div>
    <div class="synced" id="synced" style="display:none"></div>
    <div class="kpis" id="kpis-current"></div>
  </section>
  <section id="kpi-prior" aria-label="Prior week summary">
    <div class="kpi-head" id="kpi-prior-head"></div>
    <div class="kpis" id="kpis-prior"></div>
  </section>

  <section class="card" id="splh-card" style="display:none">
    <h2>Sales per labor hour by location</h2>
    <p class="hint">Weekly SPLH by location, with the multi-week average and first-to-last change.</p>
    <div class="tablewrap"><table id="splhTable"></table></div>
  </section>

  <section class="card" id="role-card" style="display:none">
    <h2>Labor hours by role</h2>
    <p class="hint" id="role-week"></p>
    <div class="tablewrap"><table id="roleTable"></table></div>
  </section>

  <section class="card">
    <h2 id="title-net">Net sales</h2>
    <p class="hint">Weekly net sales (pre-tax).</p>
    <div class="legend" id="legend-net"></div>
    <div class="chart" id="chart-net"></div>
  </section>

  <section class="card">
    <h2 id="title-labor">Labor cost % of sales</h2>
    <p class="hint">Labor cost as a share of net sales — lower is more efficient.</p>
    <div class="legend" id="legend-labor"></div>
    <div class="chart" id="chart-labor"></div>
  </section>

  <section class="card">
    <h2 id="title-txn">Transactions</h2>
    <p class="hint">Weekly order/ticket counts.</p>
    <div class="legend" id="legend-txn"></div>
    <div class="chart" id="chart-txn"></div>
  </section>

  <section class="card" id="daily-card" style="display:none">
    <h2>Current week</h2>
    <p class="hint" id="daily-hint">Net sales, labor hours, and sales per labor hour by day this week (Register excluded). Updates daily.</p>
    <div class="tablewrap"><table id="dailyTable"></table></div>
  </section>

  <section class="card">
    <details open>
      <summary>Full data table</summary>
      <div class="tablewrap"><table id="dataTable"></table></div>
    </details>
  </section>

  <div class="foot" id="foot"></div>
</div>
<div class="tt" id="tooltip"></div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("payload").textContent);
const tooltip = document.getElementById("tooltip");

function isDark() {
  const t = document.documentElement.getAttribute("data-theme");
  if (t) return t === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}
function palette() { return isDark() ? DATA.seriesDark : DATA.seriesLight; }
const money = v => (v == null ? "—" : "$" + Math.round(v).toLocaleString());
const money2 = v => (v == null ? "—" : "$" + v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}));
const pct = v => (v == null ? "—" : (v*100).toFixed(1) + "%");
const num = v => (v == null ? "—" : Math.round(v).toLocaleString());
const num1 = v => (v == null ? "—" : v.toLocaleString(undefined,{maximumFractionDigits:1}));

function showTip(html, x, y) {
  tooltip.innerHTML = html; tooltip.style.opacity = "1";
  const pad = 14; let left = x + pad, top = y + pad;
  const r = tooltip.getBoundingClientRect();
  if (left + r.width > window.innerWidth) left = x - r.width - pad;
  if (top + r.height > window.innerHeight) top = y - r.height - pad;
  tooltip.style.left = left + "px"; tooltip.style.top = top + "px";
}
function hideTip() { tooltip.style.opacity = "0"; }

const NS = "http://www.w3.org/2000/svg";
function el(name, attrs) { const e = document.createElementNS(NS, name); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }

// Multi-series line chart with a hover crosshair + tooltip listing every series.
function lineChart(mountId, series, fmt) {
  const mount = document.getElementById(mountId);
  mount.innerHTML = "";
  const weeks = DATA.weeks;
  const W = 1120, H = 320, m = { top: 16, right: 18, bottom: 34, left: 62 };
  const pw = W - m.left - m.right, ph = H - m.top - m.bottom;
  const colors = palette();

  let max = 0, min = Infinity;
  series.forEach(s => s.values.forEach(v => { if (v != null) { max = Math.max(max, v); min = Math.min(min, v); } }));
  if (!isFinite(min)) min = 0;
  min = Math.min(min, 0); if (max === min) max = min + 1;
  max *= 1.08;

  const x = i => m.left + (weeks.length <= 1 ? pw/2 : (pw * i) / (weeks.length - 1));
  const y = v => m.top + ph - (ph * (v - min)) / (max - min);

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });

  // Gridlines + y labels
  const ticks = 4;
  for (let t = 0; t <= ticks; t++) {
    const val = min + (max - min) * (t / ticks);
    const yy = y(val);
    svg.appendChild(el("line", { x1: m.left, y1: yy, x2: W - m.right, y2: yy, stroke: "var(--grid)", "stroke-width": 1 }));
    const lbl = el("text", { x: m.left - 8, y: yy + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 11 });
    lbl.textContent = fmt(val); svg.appendChild(lbl);
  }
  // X labels
  weeks.forEach((wk, i) => {
    if (weeks.length > 8 && i % 2 === 1) return;
    const lbl = el("text", { x: x(i), y: H - 12, "text-anchor": "middle", fill: "var(--muted)", "font-size": 11 });
    lbl.textContent = wk.slice(5); svg.appendChild(lbl);
  });

  // Series lines + markers
  series.forEach(s => {
    const c = colors[s.slot % colors.length];
    let d = "", started = false;
    s.values.forEach((v, i) => {
      if (v == null) { started = false; return; }
      d += (started ? " L" : " M") + x(i) + " " + y(v); started = true;
    });
    if (d) svg.appendChild(el("path", { d: d.trim(), fill: "none", stroke: c, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    s.values.forEach((v, i) => {
      if (v == null) return;
      svg.appendChild(el("circle", { cx: x(i), cy: y(v), r: 3.2, fill: c, stroke: "var(--surface)", "stroke-width": 1.5 }));
    });
  });

  // Crosshair overlay
  const cross = el("line", { x1: 0, y1: m.top, x2: 0, y2: m.top + ph, stroke: "var(--axis)", "stroke-width": 1, opacity: 0 });
  svg.appendChild(cross);
  const hit = el("rect", { x: m.left, y: m.top, width: pw, height: ph, fill: "transparent" });
  svg.appendChild(hit);

  function move(evt) {
    const pt = svg.getBoundingClientRect();
    const px = (evt.clientX - pt.left) / pt.width * W;
    let idx = Math.round((px - m.left) / (pw / Math.max(weeks.length - 1, 1)));
    idx = Math.max(0, Math.min(weeks.length - 1, idx));
    cross.setAttribute("x1", x(idx)); cross.setAttribute("x2", x(idx)); cross.setAttribute("opacity", 1);
    let rows = "";
    series.forEach(s => {
      const v = s.values[idx];
      const c = colors[s.slot % colors.length];
      rows += `<div class="row"><span class="k"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${c};margin-right:6px"></span>${s.name}</span><span class="v">${fmt(v)}</span></div>`;
    });
    showTip(`<div class="hd">Week of ${DATA.weeks[idx]}</div>${rows}`, evt.clientX, evt.clientY);
  }
  hit.addEventListener("mousemove", move);
  hit.addEventListener("mouseleave", () => { hideTip(); cross.setAttribute("opacity", 0); });
  mount.appendChild(svg);
}

function legend(mountId, series) {
  const mount = document.getElementById(mountId);
  const colors = palette();
  mount.innerHTML = series.map(s =>
    `<span><span class="swatch" style="background:${colors[s.slot % colors.length]}"></span>${s.name}</span>`
  ).join("");
}

function kpiTiles(row) {
  const items = [
    { label: "Net Sales", value: money(row.netSales), d: row.deltas.netSales, good: "up" },
    { label: "Transactions", value: num(row.transactions), d: row.deltas.transactions, good: "up" },
    { label: "Labor Cost", value: money(row.laborCost), d: row.deltas.laborCost, good: "down" },
    { label: "Labor %", value: pct(row.laborPct), d: row.deltas.laborPct, good: "down" },
    { label: "Sales / Labor Hr", value: money2(row.salesPerLaborHour), d: row.deltas.salesPerLaborHour, good: "up" },
  ];
  return items.map(it => {
    let dd;
    if (it.d == null) { dd = `<div class="delta flat">no prior data</div>`; }
    else {
      const up = it.d > 0.0005, down = it.d < -0.0005;
      const isGood = (it.good === "up" && up) || (it.good === "down" && down);
      const cls = (up || down) ? (isGood ? "up" : "down") : "flat";
      const arrow = up ? "▲" : down ? "▼" : "–";
      dd = `<div class="delta ${cls}">${arrow} ${Math.abs(it.d*100).toFixed(1)}% vs prior week</div>`;
    }
    return `<div class="tile"><div class="label">${it.label}</div><div class="value">${it.value}</div>${dd}</div>`;
  }).join("");
}

function renderKpis() {
  const rows = DATA.kpiRows || {};
  const curSec = document.getElementById("kpi-current");
  const synced = document.getElementById("synced");
  if (rows.current) {
    curSec.style.display = "";
    document.getElementById("kpi-current-head").textContent =
      "Current week" + (rows.current.dates ? " · " + rows.current.dates : "");
    document.getElementById("kpis-current").innerHTML = kpiTiles(rows.current);
    if (DATA.syncedAt) {
      synced.style.display = "";
      synced.innerHTML = `<span class="dot"></span>Last synced ${DATA.syncedAt}`;
    } else {
      synced.style.display = "none";
    }
  } else {
    curSec.style.display = "none";
    synced.style.display = "none";
  }
  if (rows.prior) {
    document.getElementById("kpi-prior-head").textContent =
      "Prior week" + (rows.prior.dates ? " · " + rows.prior.dates : "");
    document.getElementById("kpis-prior").innerHTML = kpiTiles(rows.prior);
  }
}

function renderTable() {
  const t = document.getElementById("dataTable");
  const cols = [
    ["Week Of", "week", s => s], ["Location", "location", s => s],
    ["Net Sales", "netSales", money2], ["Transactions", "transactions", num],
    ["Avg Check", "avgCheck", money2], ["Labor Hrs", "laborHours", num1],
    ["Labor Cost", "laborCost", money2], ["Labor %", "laborPct", pct],
    ["Sales / Labor Hr", "salesPerLaborHour", money2],
  ];
  const head = "<thead><tr>" + cols.map(c => `<th>${c[0]}</th>`).join("") + "</tr></thead>";
  const body = "<tbody>" + DATA.table.map(r =>
    "<tr>" + cols.map(c => `<td>${c[2](r[c[1]])}</td>`).join("") + "</tr>"
  ).join("") + "</tbody>";
  t.innerHTML = head + body;
}

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function fmtDay(iso) { const p = iso.split("-"); return MONTHS[+p[1]-1] + " " + (+p[2]); }
function changeCell(change) {
  if (change == null) return "—";
  const up = change > 0.0005, down = change < -0.0005;
  const cls = up ? "up" : down ? "down" : "flat";
  return `<span class="delta ${cls}">${change > 0 ? "+" : ""}${(change*100).toFixed(1)}%</span>`;
}

// Overview only: SPLH by location across weeks + avg + change.
function renderSplh() {
  const card = document.getElementById("splh-card");
  const m = DATA.splhMatrix;
  if (!m || DATA.locations.length <= 1 || !m.rows.length) { card.style.display = "none"; return; }
  card.style.display = "";
  const head = "<thead><tr><th>Location</th>" +
    m.weeks.map(w => `<th>${fmtDay(w)}</th>`).join("") +
    "<th>Avg</th><th>Change</th></tr></thead>";
  const body = "<tbody>" + m.rows.map(r =>
    `<tr><td>${r.name}</td>` +
    r.values.map(v => `<td>${money2(v)}</td>`).join("") +
    `<td><strong>${money2(r.avg)}</strong></td><td>${changeCell(r.change)}</td></tr>`
  ).join("") + "</tbody>";
  document.getElementById("splhTable").innerHTML = head + body;
}

// Overview only: labor hours by role x location (latest week, Register excluded).
function renderLaborByRole() {
  const card = document.getElementById("role-card");
  const d = DATA.laborByRole;
  if (!d || !d.roles.length || !d.locations.length) { card.style.display = "none"; return; }
  card.style.display = "";
  const hm = h => Math.floor(h) + "h" + String(Math.round((h - Math.floor(h)) * 60)).padStart(2, "0") + "m";
  const head = "<thead><tr><th>Role</th>" +
    d.locations.map(l => `<th>${l}</th>`).join("") + "</tr></thead>";
  let body = "<tbody>";
  d.roles.forEach(role => {
    body += `<tr><td>${role}</td>` + d.locations.map(l => {
      const v = (d.cells[l] || {})[role] || 0;
      const tot = d.totals[l] || 0;
      if (!v) return "<td>—</td>";
      const p = tot ? (v / tot * 100).toFixed(1) : "0.0";
      return `<td>${hm(v)} <span class="muted">(${p}%)</span></td>`;
    }).join("") + "</tr>";
  });
  body += `<tr class="total"><td><strong>Total (ex-Register)</strong></td>` +
    d.locations.map(l => `<td><strong>${(d.totals[l] || 0).toFixed(1)}h</strong></td>`).join("") +
    "</tr></tbody>";
  document.getElementById("roleTable").innerHTML = head + body;
  document.getElementById("role-week").textContent = "Week of " + d.weekOf + " · Register excluded";
}

// Location only: current-week per-day Net sales / Hours / SPLH.
function renderDaily() {
  const card = document.getElementById("daily-card");
  const rows = DATA.daily || [];
  if (!rows.length) { card.style.display = "none"; return; }
  card.style.display = "";
  const hm = h => Math.floor(h) + "h" + String(Math.round((h - Math.floor(h)) * 60)).padStart(2, "0") + "m";
  const head = "<thead><tr><th>Day</th><th>Net sales</th><th>Hours</th><th>SPLH</th></tr></thead>";
  const body = "<tbody>" + rows.map(r =>
    `<tr><td>${r.dow} ${fmtDay(r.day)}</td><td>${money2(r.netSales)}</td>` +
    `<td>${hm(r.laborHours)}</td><td>${money2(r.salesPerLaborHour)}</td></tr>`
  ).join("") + "</tbody>";
  document.getElementById("dailyTable").innerHTML = head + body;
}

function renderNav() {
  const mount = document.getElementById("nav");
  mount.innerHTML = DATA.nav.map(n =>
    `<a href="${n.href}" class="${n.slug === DATA.activeSlug ? 'active' : ''}">${n.label}</a>`
  ).join("");
}

function renderAll() {
  document.getElementById("title").textContent = DATA.title;
  const byLoc = DATA.locations.length > 1;
  const scope = DATA.activeSlug === "index"
    ? `${DATA.locations.length} location${DATA.locations.length===1?"":"s"}`
    : DATA.scopeLabel;
  document.getElementById("subtitle").textContent =
    `${scope} · ${DATA.weeks.length} completed week${DATA.weeks.length===1?"":"s"}`;

  renderNav();
  renderKpis();
  renderSplh();
  renderLaborByRole();
  renderDaily();

  // Chart headings: "… by location" only makes sense on the multi-location
  // overview. All three trend charts are week-over-week series.
  document.getElementById("title-net").textContent =
    byLoc ? "Net sales week over week by location" : "Net sales week over week";
  document.getElementById("title-labor").textContent = "Labor cost % of sales week over week";
  document.getElementById("title-txn").textContent =
    byLoc ? "Transactions week over week by location" : "Transactions week over week";

  // A legend only earns its space with 2+ series; a single line is named by the title.
  legend("legend-net", byLoc ? DATA.charts.netSales : []);
  legend("legend-labor", byLoc ? DATA.charts.laborPct : []);
  legend("legend-txn", byLoc ? DATA.charts.transactions : []);

  lineChart("chart-net", DATA.charts.netSales, money);
  lineChart("chart-labor", DATA.charts.laborPct, pct);
  lineChart("chart-txn", DATA.charts.transactions, num);
  renderTable();
  document.getElementById("foot").textContent =
    "Generated from Toast POS data. Net sales are pre-tax."
    + (DATA.syncedAt ? " · Last synced " + DATA.syncedAt : "");
}

document.getElementById("themeToggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : cur === "light" ? "dark" : (isDark() ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", next);
  renderAll();
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!document.documentElement.getAttribute("data-theme")) renderAll();
});
window.addEventListener("resize", () => { /* SVG is viewBox-scaled; nothing to do */ });

// Downloads: link the full Excel workbook (all locations) and wire Print/PDF,
// which captures whichever page you're on exactly as shown.
(function initDownloads() {
  const dl = DATA.downloads || {};
  const a = document.getElementById("downloadExcel");
  if (a && dl.excel) {
    a.href = dl.excel;
    if (dl.excelName) a.setAttribute("download", dl.excelName);
    a.title = "Download the full Excel workbook — Summary + a tab per location";
    a.hidden = false;
  }
  const pb = document.getElementById("printBtn");
  if (pb) pb.addEventListener("click", () => window.print());
})();

renderAll();
</script>
</body>
</html>"""
