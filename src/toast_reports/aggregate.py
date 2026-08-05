"""Weekly aggregation.

Turns raw ``TimeEntry`` / ``OrderRecord`` lists into per-location, per-week
metric rows: sales, transactions, labor hours, labor cost, and the derived
efficiency ratios operators actually manage to (labor %, sales per labor hour,
average check).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

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
