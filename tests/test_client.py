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
