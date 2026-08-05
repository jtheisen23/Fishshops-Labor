"""Tests for the weekly aggregation logic — the part that must be correct."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toast_reports.aggregate import aggregate_weekly, group_by_week, week_start_of  # noqa: E402
from toast_reports.models import Location, LocationDataset, OrderRecord, TimeEntry  # noqa: E402


def test_week_start_monday():
    # 2026-08-05 is a Wednesday -> week starts Monday 2026-08-03.
    assert week_start_of(date(2026, 8, 5), "monday") == date(2026, 8, 3)


def test_week_start_sunday():
    # Wednesday -> Sunday 2026-08-02.
    assert week_start_of(date(2026, 8, 5), "sunday") == date(2026, 8, 2)


def _dataset() -> LocationDataset:
    loc = Location("g1", "Test Shop", "America/New_York")
    ds = LocationDataset(location=loc)
    # Two orders in the same week.
    ds.orders = [
        OrderRecord("g1", date(2026, 8, 3), "o1", None, 2, 1, net_sales=100.0, tax=8.0, tips=15.0),
        OrderRecord("g1", date(2026, 8, 4), "o2", None, 1, 1, net_sales=50.0, tax=4.0, tips=5.0),
        OrderRecord("g1", date(2026, 8, 4), "ov", None, 1, 1, net_sales=999.0, tax=0.0, tips=0.0, voided=True),
    ]
    # Time entries: 10 regular hrs @ $20, 2 OT hrs @ $20 (1.5x).
    ds.time_entries = [
        TimeEntry("g1", date(2026, 8, 3), "e1", "Server", None, None, 10.0, 0.0, 20.0),
        TimeEntry("g1", date(2026, 8, 4), "e2", "Cook", None, None, 0.0, 2.0, 20.0),
    ]
    return ds


def test_sales_and_transactions():
    metrics = aggregate_weekly([_dataset()], week_start="monday")
    assert len(metrics) == 1
    m = metrics[0]
    assert m.week_start == date(2026, 8, 3)
    # Voided order excluded.
    assert m.net_sales == 150.0
    assert m.tax == 12.0
    assert m.tips == 20.0
    assert m.transaction_count == 2
    assert m.guest_count == 3


def test_labor_cost_and_ratios():
    m = aggregate_weekly([_dataset()], week_start="monday")[0]
    # 10*20 regular + 2*20*1.5 OT = 200 + 60 = 260.
    assert m.labor_cost == 260.0
    assert m.regular_hours == 10.0
    assert m.overtime_hours == 2.0
    assert m.labor_hours == 12.0
    # labor % = 260 / 150.
    assert abs(m.labor_pct - (260.0 / 150.0)) < 1e-9
    # avg check = 150 / 2 transactions.
    assert m.avg_check == 75.0
    # sales per labor hour = 150 / 12.
    assert abs(m.sales_per_labor_hour - (150.0 / 12.0)) < 1e-9


def test_group_by_week_rolls_up_locations():
    a = _dataset()
    b = LocationDataset(location=Location("g2", "Second Shop"))
    b.orders = [OrderRecord("g2", date(2026, 8, 3), "o3", None, 1, 1, 200.0, 16.0, 20.0)]
    weeks = group_by_week(aggregate_weekly([a, b], week_start="monday"))
    assert len(weeks) == 1
    week = weeks[0]
    assert len(week.rows) == 2
    assert week.net_sales == 350.0
