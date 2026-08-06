"""Automated per-location observations.

Turns the aggregated weekly metrics into a short list of plain-English,
data-driven notes for each location page (and a company-level summary for the
overview). This runs at build time, so the observations are deterministic and
refresh with the data every run — no runtime API or model calls, keeping the
published pages fully self-contained.

Each observation is a dict: {"text": str, "tone": "good"|"bad"|"neutral"}.
Tone drives only the colored dot in the UI; the wording stays factual.
"""

from __future__ import annotations

from statistics import mean

from .aggregate import WeeklyMetrics, group_by_week

# Thresholds tuned for full-service restaurant norms. Kept here so they're easy
# to adjust in one place.
_MEANINGFUL = 0.03      # >=3% change is worth mentioning
_NOTABLE = 0.10         # >=10% change is called out more strongly
_LABOR_HIGH = 0.32      # labor cost above 32% of sales reads as elevated
_LABOR_EFFICIENT = 0.25  # below 25% reads as efficient
_VS_AVG = 0.05          # +/-5% vs the company average is "in line"

_COMPANY_KEY = "__company__"


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _money2(v: float) -> str:
    return f"${v:,.2f}"


def _pct_change(a: float, b: float) -> float | None:
    return (a - b) / b if b else None


def _pctpts(v: float) -> str:
    return f"{v * 100:.1f}%"


def build_observations(
    metrics: list[WeeklyMetrics], kpi_by_loc: dict | None = None
) -> dict[str, list[dict]]:
    """Return {location_name: [observation, ...]} plus a company summary under
    ``__company__``. Observations are ordered most-important first and capped so
    the card stays scannable."""
    kpi_by_loc = kpi_by_loc or {}
    if not metrics:
        return {}

    by_loc: dict[str, list[WeeklyMetrics]] = {}
    for m in metrics:
        by_loc.setdefault(m.location_name, []).append(m)
    for rows in by_loc.values():
        rows.sort(key=lambda r: r.week_start)

    latest = max(m.week_start for m in metrics)
    latest_rows = [m for m in metrics if m.week_start == latest]
    avg_splh = mean([r.sales_per_labor_hour for r in latest_rows if r.labor_hours]) \
        if any(r.labor_hours for r in latest_rows) else 0.0
    # Rank locations present in the latest week by SPLH (efficiency).
    splh_rank = [r.location_name for r in
                 sorted((r for r in latest_rows if r.labor_hours),
                        key=lambda r: r.sales_per_labor_hour, reverse=True)]

    out: dict[str, list[dict]] = {}
    for name, rows in by_loc.items():
        out[name] = _location_notes(name, rows, latest, avg_splh, splh_rank,
                                    kpi_by_loc.get(name, {}))[:5]
    out[_COMPANY_KEY] = _company_notes(metrics, latest, splh_rank)[:5]
    return out


def _location_notes(name, rows, latest, avg_splh, splh_rank, kpi) -> list[dict]:
    notes: list[dict] = []
    cur = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    in_latest = cur.week_start == latest

    # 1) Sales week over week.
    if prev:
        d = _pct_change(cur.net_sales, prev.net_sales)
        if d is not None and abs(d) >= _MEANINGFUL:
            word = "up" if d > 0 else "down"
            strength = "sharply " if abs(d) >= _NOTABLE else ""
            notes.append({
                "tone": "good" if d > 0 else "bad",
                "text": f"Net sales {strength}{word} {_pctpts(abs(d))} week over week "
                        f"({_money(cur.net_sales)} vs {_money(prev.net_sales)} the week prior).",
            })
        else:
            notes.append({
                "tone": "neutral",
                "text": f"Net sales held roughly flat week over week ({_money(cur.net_sales)}).",
            })

    # 2) Multi-week direction (only if we have enough weeks to mean something).
    if len(rows) >= 3:
        first = rows[0]
        d = _pct_change(cur.net_sales, first.net_sales)
        if d is not None and abs(d) >= _NOTABLE:
            notes.append({
                "tone": "good" if d > 0 else "bad",
                "text": f"Over the last {len(rows)} weeks net sales are "
                        f"{'trending up' if d > 0 else 'trending down'} {_pctpts(abs(d))} "
                        f"from the start of the window.",
            })

    # 3) Labor cost % of sales — level and direction.
    lp = cur.labor_pct
    if cur.net_sales:
        if lp >= _LABOR_HIGH:
            tone, lead = "bad", f"Labor is running high at {_pctpts(lp)} of sales"
        elif lp <= _LABOR_EFFICIENT:
            tone, lead = "good", f"Labor is efficient at {_pctpts(lp)} of sales"
        else:
            tone, lead = "neutral", f"Labor is {_pctpts(lp)} of sales (a normal range)"
        tail = "."
        if prev and prev.labor_pct:
            dl = lp - prev.labor_pct
            if abs(dl) >= 0.02:
                tail = (f", {'up' if dl > 0 else 'down'} "
                        f"{abs(dl) * 100:.1f} pts from last week.")
                if dl < 0 and tone != "good":
                    tone = "good"
                elif dl > 0 and tone != "bad":
                    tone = "bad"
        notes.append({"tone": tone, "text": lead + tail})

    # 4) Sales per labor hour vs the company average.
    if cur.labor_hours and avg_splh:
        d = _pct_change(cur.sales_per_labor_hour, avg_splh)
        if d is not None and abs(d) >= _VS_AVG:
            notes.append({
                "tone": "good" if d > 0 else "bad",
                "text": f"Sales per labor hour ({_money2(cur.sales_per_labor_hour)}) is "
                        f"{_pctpts(abs(d))} {'above' if d > 0 else 'below'} the "
                        f"4-location average ({_money2(avg_splh)}).",
            })
        # Rank call-out only for the clear best/worst.
        if in_latest and len(splh_rank) >= 3 and name in splh_rank:
            pos = splh_rank.index(name)
            if pos == 0:
                notes.append({"tone": "good",
                              "text": "Most efficient location this week by sales per labor hour."})
            elif pos == len(splh_rank) - 1:
                notes.append({"tone": "bad",
                              "text": "Lowest sales per labor hour of the locations this week."})

    # 5) Current-week pace vs the same point last week.
    cur_w, prior_same = kpi.get("current"), kpi.get("priorSame")
    if cur_w and prior_same and prior_same.get("netSales"):
        d = _pct_change(cur_w["netSales"], prior_same["netSales"])
        if d is not None and abs(d) >= _MEANINGFUL:
            notes.append({
                "tone": "good" if d > 0 else "bad",
                "text": f"Through the same point last week, current-week sales are "
                        f"{'ahead' if d > 0 else 'behind'} by {_pctpts(abs(d))}.",
            })

    return notes


def _company_notes(metrics, latest, splh_rank) -> list[dict]:
    notes: list[dict] = []
    weeks = group_by_week(metrics)

    # Company sales week over week.
    if len(weeks) >= 2:
        cur_w, prev_w = weeks[-1], weeks[-2]
        d = _pct_change(cur_w.net_sales, prev_w.net_sales)
        if d is not None:
            if abs(d) >= _MEANINGFUL:
                notes.append({
                    "tone": "good" if d > 0 else "bad",
                    "text": f"Company net sales {'up' if d > 0 else 'down'} {_pctpts(abs(d))} "
                            f"week over week ({_money(cur_w.net_sales)} vs {_money(prev_w.net_sales)}).",
                })
            else:
                notes.append({
                    "tone": "neutral",
                    "text": f"Company net sales roughly flat week over week ({_money(cur_w.net_sales)}).",
                })
        if prev_w.labor_pct:
            notes.append({
                "tone": "bad" if cur_w.labor_pct >= _LABOR_HIGH else
                        "good" if cur_w.labor_pct <= _LABOR_EFFICIENT else "neutral",
                "text": f"Company labor is {_pctpts(cur_w.labor_pct)} of sales this week.",
            })

    # Best / worst efficiency.
    if len(splh_rank) >= 2:
        notes.append({"tone": "good",
                      "text": f"{splh_rank[0]} led on sales per labor hour this week; "
                              f"{splh_rank[-1]} was lowest."})

    # Locations with elevated labor.
    latest_rows = [m for m in metrics if m.week_start == latest]
    high = [r.location_name for r in latest_rows if r.net_sales and r.labor_pct >= _LABOR_HIGH]
    if high:
        notes.append({
            "tone": "bad",
            "text": "Elevated labor % this week at: " + ", ".join(sorted(high)) + ".",
        })

    return notes
