"""Tests for the sales-and-transactions-by-revenue-center board.

Covers the two things that must be right: the counts and sales reconcile with
the orders that went in, and locations are only shown a breakdown when they
actually use revenue centers.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toast_reports.aggregate import (  # noqa: E402
    UNASSIGNED_CENTER,
    orders_by_revenue_center,
)
from toast_reports.models import Location, LocationDataset, OrderRecord  # noqa: E402


def _order(guid: str, day: date, center: str, voided: bool = False,
           net: float = 10.0) -> OrderRecord:
    return OrderRecord(
        location_guid=guid, business_date=day, order_guid=f"{guid}-{day}-{center}-{voided}-{net}",
        opened_at=None, guest_count=1, check_count=1, net_sales=net, tax=1.0, tips=1.0,
        voided=voided, revenue_center=center,
    )


def _ds(name: str, orders: list[OrderRecord]) -> LocationDataset:
    ds = LocationDataset(location=Location(name.lower(), name, "America/Los_Angeles"))
    ds.orders = orders
    return ds


D1, D2 = date(2026, 8, 3), date(2026, 8, 4)


def test_counts_transactions_and_sales_per_day_per_center():
    ds = _ds("Pacific Beach", [
        _order("g", D1, "Bar", net=12.0), _order("g", D1, "Bar", net=8.0),
        _order("g", D1, "Patio", net=30.0),
        _order("g", D2, "Bar", net=25.0),
    ])
    out = orders_by_revenue_center([ds])

    rc = out["Pacific Beach"]
    assert rc["days"] == [D1.isoformat(), D2.isoformat()]
    assert rc["counts"][f"{D1.isoformat()}|Bar"] == 2
    assert rc["sales"][f"{D1.isoformat()}|Bar"] == 20.0
    assert rc["counts"][f"{D1.isoformat()}|Patio"] == 1
    assert rc["sales"][f"{D1.isoformat()}|Patio"] == 30.0
    assert rc["dayTotals"] == {D1.isoformat(): 3, D2.isoformat(): 1}
    assert rc["daySales"] == {D1.isoformat(): 50.0, D2.isoformat(): 25.0}
    assert rc["centerTotals"] == {"Bar": 3, "Patio": 1}
    assert rc["centerSales"] == {"Bar": 45.0, "Patio": 30.0}
    assert rc["max"] == 2            # busiest cell by transactions
    assert rc["maxSales"] == 30.0    # biggest cell by sales


def test_voided_orders_excluded_from_both_metrics():
    ds = _ds("Encinitas", [
        _order("g", D1, "Bar", net=10.0),
        _order("g", D1, "Bar", voided=True, net=999.0),
        _order("g", D1, "Patio", net=10.0),
    ])
    rc = orders_by_revenue_center([ds])["Encinitas"]
    assert rc["centerTotals"] == {"Bar": 1, "Patio": 1}
    assert rc["centerSales"] == {"Bar": 10.0, "Patio": 10.0}


def test_centers_ordered_by_sales_with_unassigned_last():
    """Order follows sales, not transaction count — the Patio turns fewer
    covers than the Bar but takes more money, so it leads."""
    ds = _ds("Point Loma", [
        _order("g", D1, "", net=50.0),   # unassigned — must sort last despite volume
        _order("g", D1, "", net=50.0),
        _order("g", D1, "", net=50.0),
        _order("g", D1, "Patio", net=80.0),
        _order("g", D1, "Bar", net=20.0), _order("g", D1, "Bar", net=20.0),
    ])
    rc = orders_by_revenue_center([ds])["Point Loma"]
    assert rc["centers"] == ["Patio", "Bar", UNASSIGNED_CENTER]
    assert rc["centerTotals"] == {"Patio": 1, "Bar": 2, UNASSIGNED_CENTER: 3}
    assert rc["centerSales"] == {"Patio": 80.0, "Bar": 40.0, UNASSIGNED_CENTER: 150.0}


def test_location_without_revenue_centers_is_omitted():
    """Every order unassigned means the location doesn't use revenue centers —
    the breakdown would just restate the daily order count."""
    with_centers = _ds("Has Centers", [_order("a", D1, "Bar")])
    without = _ds("No Centers", [_order("b", D1, ""), _order("b", D2, "")])

    out = orders_by_revenue_center([with_centers, without])
    assert "Has Centers" in out
    assert "No Centers" not in out


def test_returns_none_when_no_location_uses_revenue_centers():
    assert orders_by_revenue_center([_ds("No Centers", [_order("b", D1, "")])]) is None
    assert orders_by_revenue_center([_ds("Empty", [])]) is None


def test_window_keeps_the_most_recent_days_only():
    orders = []
    for i in range(40):
        orders.append(_order("g", D1 + timedelta(days=i), "Bar"))
    rc = orders_by_revenue_center([_ds("Oceanside", orders)], days=28)["Oceanside"]

    assert len(rc["days"]) == 28
    assert rc["days"][-1] == (D1 + timedelta(days=39)).isoformat()
    assert rc["days"][0] == (D1 + timedelta(days=12)).isoformat()
    # Totals cover only the kept window, so the board's rows and totals agree.
    assert rc["centerTotals"]["Bar"] == 28
    assert sum(rc["counts"].values()) == sum(rc["dayTotals"].values()) == 28
    assert rc["centerSales"]["Bar"] == 280.0
    assert sum(rc["sales"].values()) == sum(rc["daySales"].values()) == 280.0
