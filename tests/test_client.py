"""Tests for the Toast client's business-date windowing.

These guard the timezone-boundary fix: orders/labor are fetched with a padded
UTC window but must be filtered to the exact business-date range so weeks stay
whole and no sliver of an out-of-window week leaks in.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toast_reports.client import ToastClient  # noqa: E402
from toast_reports.config import ToastCredentials  # noqa: E402
from toast_reports.models import Location  # noqa: E402


def _client() -> ToastClient:
    return ToastClient(ToastCredentials("https://h", "id", "sec"))


def test_orders_filtered_to_business_date_window():
    c = _client()
    captured: dict = {}

    def fake_paginated(path, guid, params):
        captured["params"] = params
        return [
            {"guid": "in1", "businessDate": "20260706", "checks": [{"amount": 100, "taxAmount": 8, "payments": []}]},
            {"guid": "edge", "businessDate": "20260802", "checks": [{"amount": 50, "taxAmount": 4, "payments": []}]},
            {"guid": "before", "businessDate": "20260705", "checks": [{"amount": 999, "taxAmount": 0, "payments": []}]},
            {"guid": "after", "businessDate": "20260803", "checks": [{"amount": 999, "taxAmount": 0, "payments": []}]},
        ]

    c._get_paginated = fake_paginated  # type: ignore[assignment]
    orders = c.get_orders(Location("g", "Test"), datetime(2026, 7, 6), datetime(2026, 8, 2, 23, 59, 59))

    assert {o.order_guid for o in orders} == {"in1", "edge"}  # out-of-window dropped
    # The UTC query is padded a day on each side to cover the timezone offset.
    assert captured["params"]["startDate"].startswith("2026-07-05")
    assert captured["params"]["endDate"].startswith("2026-08-03")


def test_time_entries_filtered_to_business_date_window():
    c = _client()

    def fake_get(path, guid, params=None):
        # /labor/v1/jobs returns [] here (no titles), then timeEntries returns rows.
        if path.endswith("/jobs"):
            return []
        return [
            {"businessDate": "20260706", "regularHours": 8, "hourlyWage": 15},
            {"businessDate": "20260705", "regularHours": 8, "hourlyWage": 15},  # out of window
        ]

    c._get = fake_get  # type: ignore[assignment]
    entries = c.get_time_entries(Location("g", "Test"), datetime(2026, 7, 6), datetime(2026, 8, 2, 23, 59, 59))

    assert len(entries) == 1
    assert entries[0].business_date.isoformat() == "2026-07-06"


def test_time_entries_chunks_long_window_under_30_days():
    """A window wider than 30 days must be split into multiple requests, each
    spanning <=30 days (Toast rejects larger labor ranges), with rows deduped."""
    c = _client()
    calls: list[dict] = []

    def fake_get(path, guid, params=None):
        if path.endswith("/jobs"):
            return []
        calls.append(params)
        # Same entry (identical guid) can surface in adjacent chunks; must dedupe.
        return [{"guid": "te1", "businessDate": "20260715", "regularHours": 8, "hourlyWage": 15}]

    c._get = fake_get  # type: ignore[assignment]
    # 40-day window -> at least two chunks after +/-1 day padding.
    entries = c.get_time_entries(Location("g", "Test"), datetime(2026, 7, 1), datetime(2026, 8, 9, 23, 59, 59))

    assert len(calls) >= 2  # window was chunked
    for p in calls:
        span = _iso_span_days(p["startDate"], p["endDate"])
        assert span <= 30, f"chunk span {span}d exceeds Toast's 30-day cap"
    assert len(entries) == 1  # duplicate guid across chunks collapsed to one


def _iso_span_days(start_iso: str, end_iso: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%S.000+0000"
    return (datetime.strptime(end_iso, fmt) - datetime.strptime(start_iso, fmt)).total_seconds() / 86400


def test_orders_resolve_revenue_center_names():
    """Orders reference a revenue center by GUID only; the config endpoint
    supplies the names. An order with no revenue center maps to "" (which the
    board reports as Unassigned)."""
    c = _client()

    def fake_get(path, guid, params=None):
        if "revenueCenters" in path:
            return [
                {"guid": "rc-bar", "entityType": "RevenueCenter", "name": "Bar"},
                {"guid": "rc-patio", "entityType": "RevenueCenter", "name": "Patio"},
            ]
        return []  # dining options

    def fake_paginated(path, guid, params):
        return [
            {"guid": "o1", "businessDate": "20260706", "checks": [],
             "revenueCenter": {"guid": "rc-bar", "entityType": "RevenueCenter"}},
            {"guid": "o2", "businessDate": "20260706", "checks": [],
             "revenueCenter": {"guid": "rc-unknown"}},   # not in config -> ""
            {"guid": "o3", "businessDate": "20260706", "checks": []},  # no revenue center
        ]

    c._get = fake_get  # type: ignore[assignment]
    c._get_paginated = fake_paginated  # type: ignore[assignment]
    orders = c.get_orders(Location("g", "Test"), datetime(2026, 7, 6), datetime(2026, 7, 6, 23, 59, 59))

    assert {o.order_guid: o.revenue_center for o in orders} == {"o1": "Bar", "o2": "", "o3": ""}


def test_revenue_centers_falls_back_to_v1_and_degrades_to_empty():
    """v2 is tried first; a location whose integration only exposes v1 still
    resolves, and a total failure degrades to {} rather than crashing the run."""
    c = _client()
    tried: list[str] = []

    def only_v1(path, guid, params=None):
        tried.append(path)
        if path.endswith("/config/v2/revenueCenters"):
            raise RuntimeError("404 not provisioned")
        return [{"guid": "rc-1", "name": "Dining Room"}]

    c._get = only_v1  # type: ignore[assignment]
    assert c.get_revenue_centers(Location("g", "Test")) == {"rc-1": "Dining Room"}
    assert tried == ["/config/v2/revenueCenters", "/config/v1/revenueCenters"]

    def always_fails(path, guid, params=None):
        raise RuntimeError("no scope")

    c._get = always_fails  # type: ignore[assignment]
    assert c.get_revenue_centers(Location("g", "Test")) == {}
