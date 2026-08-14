"""Labor impact on kitchen ticket times.

Relates kitchen staffing to fired->ready ticket times. Ticket times degrade when
demand per cook-hour outruns the line, so the key metric is **kitchen load** =
orders per kitchen labor hour. Produces three views per location (only where
ticket times exist — i.e. the kitchen bumps the KDS):

  * bands   — hours grouped into Well-staffed / Adequate / Understaffed (by load),
              with median & 90th-pct ticket time each.
  * scatter — one point per business hour: load vs median ticket time, + trend.
  * hourly  — hour-of-day averages: orders, kitchen staff on clock, median ticket.

All timestamps are converted to each location's local timezone before bucketing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

try:
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001
    ZoneInfo = None  # type: ignore

from .models import LocationDataset

_KITCHEN_TITLES = {"kitchen"}
_MIN_ORDERS = 3          # ignore near-empty hours (unstable load)
_MIN_KITCHEN_HOURS = 0.25
_MIN_UNITS = 6           # need enough hours to say anything


def _tz(name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


def _to_local(dt: datetime | None, tz):
    """Convert an aware (UTC) timestamp to local; pass naive timestamps through
    (sample data is already local)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(tz) if tz else dt


def _pct(vals: list[float], q: float) -> float:
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * q))]


def _accumulate_hours(acc: dict, start: datetime, end: datetime) -> None:
    """Add each shift's fractional hours into (date, hour) buckets."""
    cur = start
    while cur < end:
        nxt = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        seg_end = min(nxt, end)
        acc[(cur.date(), cur.hour)] += (seg_end - cur).total_seconds() / 3600.0
        cur = seg_end


def build_labor_impact(datasets: list[LocationDataset], kitchen_titles=None) -> dict | None:
    titles = {t.strip().lower() for t in (kitchen_titles or _KITCHEN_TITLES)}
    out: dict = {}
    for ds in datasets:
        payload = _for_location(ds, titles)
        if payload:
            out[ds.location.name] = payload
    return out or None


def _for_location(ds: LocationDataset, titles: set[str]) -> dict | None:
    if not any(o.ticket_ready_minutes is not None for o in ds.orders):
        return None
    tz = _tz(ds.location.timezone)

    kitchen: dict = defaultdict(float)
    for te in ds.time_entries:
        if te.job.strip().lower() not in titles:
            continue
        s, e = _to_local(te.in_date, tz), _to_local(te.out_date, tz)
        if not s or not e or e <= s:
            continue
        _accumulate_hours(kitchen, s, e)

    orders_ct: dict = defaultdict(int)
    ticket: dict = defaultdict(list)
    for o in ds.orders:
        if o.voided:
            continue
        t = _to_local(o.opened_at, tz)
        if not t:
            continue
        key = (t.date(), t.hour)
        orders_ct[key] += 1
        if o.ticket_ready_minutes is not None:
            ticket[key].append(o.ticket_ready_minutes)

    # Business-hour units (a specific date+hour) with enough signal to be stable.
    units = []
    for key, n in orders_ct.items():
        kh = kitchen.get(key, 0.0)
        tks = ticket.get(key, [])
        if n >= _MIN_ORDERS and kh >= _MIN_KITCHEN_HOURS and tks:
            units.append({"load": n / kh, "orders": n, "median": round(median(tks), 1), "tk": tks})
    if len(units) < _MIN_UNITS:
        return None

    return {
        "bands": _bands(units),
        "scatter": [{"load": round(u["load"], 2), "median": u["median"], "orders": u["orders"]}
                    for u in units],
        "trend": _trend([u["load"] for u in units], [u["median"] for u in units]),
        "hourly": _hourly(orders_ct, kitchen, ticket),
    }


_WD_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_hour(h: int) -> str:
    ap = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12} {ap}"


def build_quiet_hours(datasets: list[LocationDataset], min_open_share: float = 0.5) -> dict | None:
    """For each location and weekday, find the slowest hour — the operating hour
    with the fewest orders — and its average order count, over the window (local
    time). Returns {location: [{day, hour, avg}, ... Mon..Sun]}."""
    out: dict = {}
    for ds in datasets:
        tz = _tz(ds.location.timezone)
        counts: dict = defaultdict(int)        # (date, hour) -> orders
        wd_dates: dict = defaultdict(set)      # weekday -> operating dates
        for o in ds.orders:
            if o.voided:
                continue
            t = _to_local(o.opened_at, tz)
            if not t:
                continue
            counts[(t.date(), t.hour)] += 1
            wd_dates[t.weekday()].add(t.date())
        if not counts:
            continue

        wd_hour_total: dict = defaultdict(int)  # (wd, hour) -> total orders
        wd_hour_days: dict = defaultdict(int)   # (wd, hour) -> dates with >=1 order
        for (d, h), n in counts.items():
            wd_hour_total[(d.weekday(), h)] += n
            wd_hour_days[(d.weekday(), h)] += 1

        rows = []
        for wd in range(7):
            occ = len(wd_dates[wd])
            if occ == 0:
                continue
            best = None  # (avg, hour)
            for (w, h), total in wd_hour_total.items():
                if w != wd:
                    continue
                # Require the hour to be reliably open (orders on >= half the days).
                if wd_hour_days[(wd, h)] < max(2, occ * min_open_share):
                    continue
                avg = total / occ
                if best is None or avg < best[0]:
                    best = (avg, h)
            if best:
                rows.append({"day": _WD_ABBR[wd], "hour": _fmt_hour(best[1]), "avg": round(best[0], 1)})
        if rows:
            out[ds.location.name] = rows
    return out or None


def build_ticket_heatmap(datasets: list[LocationDataset], min_n: int = 3) -> dict | None:
    """Per-location median ticket time by (weekday, hour) in local time, for a
    day x hour heatmap. Cells below ``min_n`` tickets are omitted (too noisy).
    Only locations with ready data appear."""
    out: dict = {}
    for ds in datasets:
        if not any(o.ticket_ready_minutes is not None for o in ds.orders):
            continue
        tz = _tz(ds.location.timezone)
        grid: dict = defaultdict(list)
        for o in ds.orders:
            if o.voided or o.ticket_ready_minutes is None:
                continue
            t = _to_local(o.opened_at, tz)
            if not t:
                continue
            grid[(t.weekday(), t.hour)].append(o.ticket_ready_minutes)
        cells: dict = {}
        for (wd, h), v in grid.items():
            if len(v) < min_n:
                continue
            cells[f"{wd}-{h}"] = {
                "median": round(median(v), 1),
                "n": len(v),
                "over": round(100.0 * sum(1 for x in v if x > 20) / len(v)),
            }
        if not cells:
            continue
        hours = sorted({int(k.split("-")[1]) for k in cells})
        out[ds.location.name] = {"hours": hours, "cells": cells}
    return out or None


def _bands(units: list[dict]) -> list[dict]:
    us = sorted(units, key=lambda u: u["load"])
    third = len(us) // 3
    if third == 0:
        return []
    groups = [("Well-staffed", us[:third]),
              ("Adequate", us[third:2 * third]),
              ("Understaffed", us[2 * third:])]
    rows = []
    for label, g in groups:
        if not g:
            continue
        pooled = [t for u in g for t in u["tk"]]
        loads = [u["load"] for u in g]
        rows.append({
            "label": label,
            "loadLo": round(min(loads), 1),
            "loadHi": round(max(loads), 1),
            "hours": len(g),
            "median": round(median(pooled), 1),
            "p90": round(_pct(pooled, 0.9), 1),
        })
    return rows


def _trend(xs: list[float], ys: list[float]) -> dict | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0:
        return None
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    slope = cov / var
    intercept = my - slope * mx
    sy = sum((y - my) ** 2 for y in ys)
    r = cov / (var * sy) ** 0.5 if sy > 0 else 0.0
    x0, x1 = min(xs), max(xs)
    return {
        "x0": round(x0, 2), "y0": round(intercept + slope * x0, 1),
        "x1": round(x1, 2), "y1": round(intercept + slope * x1, 1),
        "slope": round(slope, 2), "r": round(r, 2),
    }


def _hourly(orders_ct: dict, kitchen: dict, ticket: dict) -> list[dict]:
    days: dict = defaultdict(set)
    ordc: dict = defaultdict(int)
    kh: dict = defaultdict(float)
    tks: dict = defaultdict(list)
    for (d, h), n in orders_ct.items():
        days[h].add(d)
        ordc[h] += n
    for (d, h), v in kitchen.items():
        kh[h] += v
    for (_d, h), v in ticket.items():
        tks[h].extend(v)
    rows = []
    for h in sorted(days):
        nd = len(days[h]) or 1
        avg_orders = ordc[h] / nd
        if avg_orders < 1:  # skip open/close fringe hours
            continue
        rows.append({
            "hour": h,
            "orders": round(avg_orders, 1),
            "kitchenStaff": round(kh[h] / nd, 2),
            "median": round(median(tks[h]), 1) if tks[h] else None,
        })
    return rows
