"""Weekly aggregation.

Turns raw ``TimeEntry`` / ``OrderRecord`` lists into per-location, per-week
metric rows: sales, transactions, labor hours, labor cost, and the derived
efficiency ratios operators actually manage to (labor %, sales per labor hour,
average check).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import mean, median

from .models import LocationDataset, OrderRecord, TimeEntry


def week_start_of(day: date, week_start: str = "monday") -> date:
    """Return the first day of the week containing ``day``."""
    # Monday=0 ... Sunday=6
    offset = day.weekday() if week_start == "monday" else (day.weekday() + 1) % 7
    return day - timedelta(days=offset)


@dataclass
class WeeklyMetrics:
    """One location for one week."""

    location_guid: str
    location_name: str
    week_start: date

    # Sales
    net_sales: float = 0.0
    gross_sales: float = 0.0
    tax: float = 0.0
    tips: float = 0.0

    # Volume
    transaction_count: int = 0  # orders/tickets
    check_count: int = 0
    guest_count: int = 0

    # Labor
    regular_hours: float = 0.0
    overtime_hours: float = 0.0
    labor_cost: float = 0.0

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)

    @property
    def labor_hours(self) -> float:
        return self.regular_hours + self.overtime_hours

    @property
    def avg_check(self) -> float:
        return self.net_sales / self.transaction_count if self.transaction_count else 0.0

    @property
    def labor_pct(self) -> float:
        """Labor cost as a share of net sales (the number managers watch)."""
        return self.labor_cost / self.net_sales if self.net_sales else 0.0

    @property
    def sales_per_labor_hour(self) -> float:
        return self.net_sales / self.labor_hours if self.labor_hours else 0.0


def aggregate_weekly(
    datasets: list[LocationDataset], week_start: str = "monday"
) -> list[WeeklyMetrics]:
    """Aggregate all locations into (location, week) rows, sorted by week then name."""
    buckets: dict[tuple[str, date], WeeklyMetrics] = {}
    names: dict[str, str] = {}

    for ds in datasets:
        names[ds.location.guid] = ds.location.name

        for order in ds.orders:
            if order.voided:
                continue
            m = _bucket(buckets, ds, order.business_date, week_start)
            m.net_sales += order.net_sales
            m.gross_sales += order.gross_sales
            m.tax += order.tax
            m.tips += order.tips
            m.transaction_count += 1
            m.check_count += order.check_count
            m.guest_count += order.guest_count

        for te in ds.time_entries:
            m = _bucket(buckets, ds, te.business_date, week_start)
            m.regular_hours += te.regular_hours
            m.overtime_hours += te.overtime_hours
            m.labor_cost += te.labor_cost

    return sorted(buckets.values(), key=lambda m: (m.week_start, m.location_name))


def _bucket(
    buckets: dict[tuple[str, date], WeeklyMetrics],
    ds: LocationDataset,
    day: date,
    week_start: str,
) -> WeeklyMetrics:
    ws = week_start_of(day, week_start)
    key = (ds.location.guid, ws)
    if key not in buckets:
        buckets[key] = WeeklyMetrics(
            location_guid=ds.location.guid,
            location_name=ds.location.name,
            week_start=ws,
        )
    return buckets[key]


@dataclass
class CompanyWeek:
    """All locations rolled up for a single week (for the summary view)."""

    week_start: date
    rows: list[WeeklyMetrics] = field(default_factory=list)

    @property
    def net_sales(self) -> float:
        return sum(r.net_sales for r in self.rows)

    @property
    def labor_cost(self) -> float:
        return sum(r.labor_cost for r in self.rows)

    @property
    def labor_hours(self) -> float:
        return sum(r.labor_hours for r in self.rows)

    @property
    def transaction_count(self) -> int:
        return sum(r.transaction_count for r in self.rows)

    @property
    def labor_pct(self) -> float:
        return self.labor_cost / self.net_sales if self.net_sales else 0.0


def group_by_week(metrics: list[WeeklyMetrics]) -> list[CompanyWeek]:
    weeks: dict[date, CompanyWeek] = {}
    for m in metrics:
        weeks.setdefault(m.week_start, CompanyWeek(week_start=m.week_start)).rows.append(m)
    return [weeks[k] for k in sorted(weeks)]


@dataclass
class DailyMetrics:
    """One location for one business day (used by the per-location daily board)."""

    location_guid: str
    location_name: str
    business_date: date

    net_sales: float = 0.0
    transaction_count: int = 0
    regular_hours: float = 0.0
    overtime_hours: float = 0.0
    labor_cost: float = 0.0

    @property
    def labor_hours(self) -> float:
        return self.regular_hours + self.overtime_hours

    @property
    def sales_per_labor_hour(self) -> float:
        return self.net_sales / self.labor_hours if self.labor_hours else 0.0

    @property
    def avg_check(self) -> float:
        return self.net_sales / self.transaction_count if self.transaction_count else 0.0

    @property
    def labor_pct(self) -> float:
        return self.labor_cost / self.net_sales if self.net_sales else 0.0


@dataclass
class LaborByRole:
    """Labor hours grouped by role bucket x location, for the latest week."""

    week_start: date
    role_order: list[str]
    # hours[location_name][role_bucket] = hours
    hours: dict[str, dict[str, float]] = field(default_factory=dict)


def labor_by_role_last_week(
    datasets: list[LocationDataset], week_start: str, role_groups: dict
) -> LaborByRole | None:
    """Sum labor hours per role bucket per location for the most recent week
    present in the data. Titles not in any bucket get their own row. Assumes
    excluded roles (e.g. Register) were already dropped from the datasets."""
    title_to_bucket: dict[str, str] = {}
    for bucket, titles in role_groups.items():
        for t in titles:
            title_to_bucket[str(t).strip().lower()] = bucket

    all_dates = [te.business_date for ds in datasets for te in ds.time_entries]
    if not all_dates:
        return None
    latest = max(week_start_of(d, week_start) for d in all_dates)

    hours: dict[str, dict[str, float]] = {}
    extra: list[str] = []
    for ds in datasets:
        for te in ds.time_entries:
            if week_start_of(te.business_date, week_start) != latest:
                continue
            bucket = title_to_bucket.get(te.job.strip().lower())
            if bucket is None:  # unmapped title -> its own row
                bucket = te.job
                if bucket not in extra:
                    extra.append(bucket)
            loc = hours.setdefault(ds.location.name, {})
            loc[bucket] = loc.get(bucket, 0.0) + te.total_hours

    role_order = list(role_groups.keys()) + sorted(extra)
    return LaborByRole(week_start=latest, role_order=role_order, hours=hours)


# Order channel buckets for the kitchen ticket-time board, matching how an
# operator thinks about the business: dine-in, digital/online, and counter to-go.
_TICKET_BUCKETS = ["Dine-in", "Online", "Takeout"]
_ONLINE_SOURCES = {"online", "api", "toast local"}


def ticket_bucket(source: str, behavior: str) -> str | None:
    """Classify an order into Dine-in / Online / Takeout (or None to skip).
    Digital channels (online ordering, delivery apps) are 'Online'; in-house
    dine-in is 'Dine-in'; everything else to-go is 'Takeout'."""
    s = (source or "").strip().lower()
    b = (behavior or "").strip().upper()
    if s in _ONLINE_SOURCES:
        return "Online"
    if b == "DINE_IN":
        return "Dine-in"
    if b in {"TAKE_OUT", "DELIVERY"}:
        return "Takeout"
    return None


@dataclass
class TicketTimes:
    """Whole-ticket kitchen times (fired -> last item ready) for the latest week,
    per location, bucketed by order channel. Only locations whose kitchen bumps
    tickets on the KDS appear here."""

    week_start: date
    buckets: list[str]
    # stats[location_name][bucket] = {"n", "median", "mean", "p90"}
    stats: dict = field(default_factory=dict)


def ticket_times_last_week(
    datasets: list[LocationDataset], week_start: str
) -> TicketTimes | None:
    """Aggregate order-level fired->ready times for the most recent week that has
    any ticket timing, bucketed by channel. Returns None if no location records
    ready events."""
    timed = [(ds, o) for ds in datasets for o in ds.orders
             if o.ticket_ready_minutes is not None and not o.voided]
    if not timed:
        return None
    latest = max(week_start_of(o.business_date, week_start) for _, o in timed)

    per: dict[str, dict[str, list[float]]] = {}
    for ds, o in timed:
        if week_start_of(o.business_date, week_start) != latest:
            continue
        bucket = ticket_bucket(o.source, o.dining_behavior)
        if not bucket:
            continue
        per.setdefault(ds.location.name, {}).setdefault(bucket, []).append(o.ticket_ready_minutes)
    if not per:
        return None

    stats: dict = {}
    for name, buckets in per.items():
        stats[name] = {}
        for b, vals in buckets.items():
            vs = sorted(vals)
            p90 = vs[min(len(vs) - 1, int(len(vs) * 0.9))]
            stats[name][b] = {
                "n": len(vs),
                "median": round(median(vs), 1),
                "mean": round(mean(vs), 1),
                "p90": round(p90, 1),
            }
    return TicketTimes(week_start=latest, buckets=_TICKET_BUCKETS, stats=stats)


def ticket_times_by_week(
    datasets: list[LocationDataset], week_start: str
) -> dict | None:
    """Per-location weekly median ticket time (fired->ready) by channel, for the
    week-over-week trend. Returns {location: {bucket: {week_iso: median}}} or
    None if no location records ready events."""
    per: dict[str, dict[str, dict[str, list[float]]]] = {}
    for ds in datasets:
        for o in ds.orders:
            if o.ticket_ready_minutes is None or o.voided:
                continue
            bucket = ticket_bucket(o.source, o.dining_behavior)
            if not bucket:
                continue
            wk = week_start_of(o.business_date, week_start).isoformat()
            (per.setdefault(ds.location.name, {})
                .setdefault(bucket, {})
                .setdefault(wk, [])
                .append(o.ticket_ready_minutes))
    if not per:
        return None
    out: dict = {}
    for name, buckets in per.items():
        out[name] = {b: {wk: round(median(v), 1) for wk, v in weeks.items()}
                     for b, weeks in buckets.items()}
    return out


def aggregate_daily(datasets: list[LocationDataset]) -> list[DailyMetrics]:
    """Aggregate raw orders + time entries into (location, business day) rows."""
    buckets: dict[tuple[str, date], DailyMetrics] = {}

    def bucket(ds: LocationDataset, day: date) -> DailyMetrics:
        key = (ds.location.guid, day)
        if key not in buckets:
            buckets[key] = DailyMetrics(ds.location.guid, ds.location.name, day)
        return buckets[key]

    for ds in datasets:
        for order in ds.orders:
            if order.voided:
                continue
            m = bucket(ds, order.business_date)
            m.net_sales += order.net_sales
            m.transaction_count += 1
        for te in ds.time_entries:
            m = bucket(ds, te.business_date)
            m.regular_hours += te.regular_hours
            m.overtime_hours += te.overtime_hours
            m.labor_cost += te.labor_cost

    return sorted(buckets.values(), key=lambda m: (m.business_date, m.location_name))


# Orders that carry no revenue center (location doesn't use them, or the order
# wasn't assigned one) are reported under this label rather than dropped, so the
# per-day totals always reconcile with the transaction counts elsewhere.
UNASSIGNED_CENTER = "Unassigned"


def orders_by_revenue_center(
    datasets: list[LocationDataset], days: int = 28
) -> dict | None:
    """Order counts per business day per revenue center, per location.

    Covers the most recent ``days`` business dates that have orders (the daily
    grid gets unreadable much past four weeks). Locations whose orders carry no
    revenue center at all are omitted — there is nothing to break down there.

    Returns ``{location_name: {"centers": [...], "days": [iso...],
    "counts": {"<iso>|<center>": n}, "dayTotals": {iso: n},
    "centerTotals": {center: n}, "max": n}}`` or None when no location uses
    revenue centers.
    """
    out: dict = {}
    for ds in datasets:
        counts: dict[tuple[date, str], int] = {}
        seen_days: set[date] = set()
        for o in ds.orders:
            if o.voided:
                continue
            center = (o.revenue_center or "").strip() or UNASSIGNED_CENTER
            key = (o.business_date, center)
            counts[key] = counts.get(key, 0) + 1
            seen_days.add(o.business_date)
        if not counts:
            continue
        # Nothing to show when every order is unassigned — that's just the daily
        # order count, which the current-week board already covers.
        if {c for _, c in counts} == {UNASSIGNED_CENTER}:
            continue

        day_list = sorted(seen_days)[-days:]
        in_window = {d: True for d in day_list}
        counts = {(d, c): n for (d, c), n in counts.items() if d in in_window}

        center_totals: dict[str, int] = {}
        day_totals: dict[str, int] = {}
        for (d, c), n in counts.items():
            center_totals[c] = center_totals.get(c, 0) + n
            day_totals[d.isoformat()] = day_totals.get(d.isoformat(), 0) + n

        # Busiest revenue center first, but always park Unassigned last: it's a
        # data-quality bucket, not a real place in the restaurant.
        centers = sorted(
            center_totals,
            key=lambda c: (c == UNASSIGNED_CENTER, -center_totals[c], c),
        )
        out[ds.location.name] = {
            "centers": centers,
            "days": [d.isoformat() for d in day_list],
            "counts": {f"{d.isoformat()}|{c}": n for (d, c), n in counts.items()},
            "dayTotals": day_totals,
            "centerTotals": center_totals,
            "max": max(counts.values()),
        }
    return out or None
