"""Tests for the food-vs-alcohol split by revenue center.

Two things have to be right: category names land in the correct bucket (the
non-alcoholic ones are the trap), and the four-way transaction split accounts
for every ticket exactly once.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toast_reports.menu_mix import (  # noqa: E402
    ALCOHOL,
    FOOD,
    OTHER,
    classify_category,
    food_alcohol_by_revenue_center,
)
from toast_reports.models import Location, LocationDataset, OrderRecord  # noqa: E402

D1 = date(2026, 8, 3)


def _order(center: str, categories: dict, voided: bool = False, day: date = D1) -> OrderRecord:
    return OrderRecord(
        location_guid="g", business_date=day, order_guid=f"{center}-{sorted(categories)}-{voided}",
        opened_at=None, guest_count=1, check_count=1, net_sales=25.0, tax=2.0, tips=3.0,
        voided=voided, revenue_center=center, sales_by_category=categories,
    )


def _ds(name: str, orders: list[OrderRecord]) -> LocationDataset:
    ds = LocationDataset(location=Location(name.lower(), name, "America/Los_Angeles"))
    ds.orders = orders
    return ds


def test_classifies_the_obvious_categories():
    assert classify_category("Food") == FOOD
    assert classify_category("Liquor") == ALCOHOL
    assert classify_category("Beer") == ALCOHOL
    assert classify_category("Wine") == ALCOHOL
    # Case and spacing shouldn't matter.
    assert classify_category("  liquor ") == ALCOHOL


def test_non_alcoholic_categories_are_not_counted_as_alcohol():
    """The trap: these names contain an alcohol keyword but are the opposite."""
    for name in ["Non-Alcoholic", "Non Alcoholic Beverages", "N/A Beverage",
                 "NA Bev", "Zero Proof", "Mocktails"]:
        assert classify_category(name) == OTHER, name


def test_unknown_and_missing_categories_fall_to_other():
    assert classify_category("Retail Merchandise") == OTHER
    assert classify_category("") == OTHER
    assert classify_category("   ") == OTHER


def test_config_mapping_overrides_the_keyword_guess():
    """A house category the keywords would misread can be pinned in config."""
    groups = {ALCOHOL: ["Barrel Aged Program"], FOOD: ["Beer Cheese Bites"]}
    # Without config, keywords read these backwards.
    assert classify_category("Beer Cheese Bites") == ALCOHOL
    # With config, the explicit mapping wins.
    assert classify_category("Beer Cheese Bites", groups) == FOOD
    assert classify_category("Barrel Aged Program", groups) == ALCOHOL


def test_four_way_split_accounts_for_every_ticket_once():
    ds = _ds("Oceanside", [
        _order("Bar", {"Liquor": 18.0}),                    # alcohol only
        _order("Bar", {"Beer": 9.0}),                       # alcohol only
        _order("Bar", {"Food": 22.0}),                      # food only
        _order("Bar", {"Food": 20.0, "Wine": 14.0}),        # both
        _order("Bar", {"N/A Beverage": 4.0}),               # neither
    ])
    r = food_alcohol_by_revenue_center([ds])["Oceanside"]["rows"]["Bar"]

    assert r["txns"] == 5
    assert r["alcoholOnly"] == 2
    assert r["foodOnly"] == 1
    assert r["both"] == 1
    assert r["neither"] == 1
    assert r["alcoholOnly"] + r["foodOnly"] + r["both"] + r["neither"] == r["txns"]
    assert r["alcoholSales"] == 41.0   # 18 + 9 + 14
    assert r["foodSales"] == 42.0      # 22 + 20
    assert r["otherSales"] == 4.0
    assert r["itemSales"] == 87.0


def test_comped_item_still_counts_toward_the_ticket_split():
    """A $0 food item means the ticket had food on it, even at no charge."""
    ds = _ds("Oceanside", [_order("Bar", {"Food": 0.0, "Liquor": 12.0})])
    r = food_alcohol_by_revenue_center([ds])["Oceanside"]["rows"]["Bar"]
    assert r["both"] == 1
    assert r["alcoholOnly"] == 0
    assert r["foodSales"] == 0.0


def test_voided_orders_excluded():
    ds = _ds("Oceanside", [
        _order("Bar", {"Liquor": 10.0}),
        _order("Bar", {"Liquor": 999.0}, voided=True),
    ])
    r = food_alcohol_by_revenue_center([ds])["Oceanside"]["rows"]["Bar"]
    assert r["txns"] == 1
    assert r["alcoholSales"] == 10.0


def test_splits_are_per_revenue_center():
    ds = _ds("Oceanside", [
        _order("Bar", {"Liquor": 12.0}),
        _order("Dining Room", {"Food": 30.0}),
        _order("Dining Room", {"Food": 28.0, "Beer": 8.0}),
    ])
    out = food_alcohol_by_revenue_center([ds])["Oceanside"]
    assert out["centers"] == ["Dining Room", "Bar"]  # busiest first
    assert out["rows"]["Bar"]["alcoholOnly"] == 1
    assert out["rows"]["Dining Room"]["foodOnly"] == 1
    assert out["rows"]["Dining Room"]["both"] == 1


def test_location_without_sales_categories_is_omitted():
    """No categories on any item means nothing can be split — the board is
    hidden rather than reporting everything as 'neither'."""
    uncategorized = _ds("No Categories", [_order("Bar", {"": 15.0}), _order("Bar", {})])
    assert food_alcohol_by_revenue_center([uncategorized]) is None

    mixed = _ds("Has Categories", [_order("Bar", {"Liquor": 10.0})])
    out = food_alcohol_by_revenue_center([uncategorized, mixed])
    assert "Has Categories" in out
    assert "No Categories" not in out
