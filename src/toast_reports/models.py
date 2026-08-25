"""Normalized data models.

The Toast API returns deeply nested JSON. We flatten it into these small,
explicit records so the aggregation layer never has to know about Toast's wire
format. If Toast changes a field name, the only place that needs to change is
``client.py`` (the mapping from raw JSON -> these dataclasses).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Location:
    """A single restaurant/location."""

    guid: str
    name: str
    timezone: str = "America/New_York"


@dataclass
class TimeEntry:
    """One employee clock-in/out record (from the Labor API)."""

    location_guid: str
    business_date: date
    employee_id: str
    job: str
    in_date: datetime | None
    out_date: datetime | None
    regular_hours: float
    overtime_hours: float
    hourly_wage: float
    # Declared cash/non-cash tips attached to the shift, if provided.
    tips: float = 0.0

    @property
    def total_hours(self) -> float:
        return self.regular_hours + self.overtime_hours

    @property
    def labor_cost(self) -> float:
        """Wage cost of this shift. Overtime is paid at 1.5x by convention."""
        return (
            self.regular_hours * self.hourly_wage
            + self.overtime_hours * self.hourly_wage * 1.5
        )


@dataclass
class OrderRecord:
    """One order/ticket (from the Orders API), flattened to the numbers we report."""

    location_guid: str
    business_date: date
    order_guid: str
    opened_at: datetime | None
    guest_count: int
    check_count: int
    # Net sales = pre-tax revenue actually collected (excludes tax & tips).
    net_sales: float
    tax: float
    tips: float
    voided: bool = False
    # Order channel/type, used for the kitchen ticket-time board.
    source: str = ""              # e.g. "In Store", "Online", "API"
    dining_behavior: str = ""     # DINE_IN / TAKE_OUT / DELIVERY
    # Revenue center the order rang in under (Toast config: Dining Room, Bar,
    # Patio, To-Go, ...). Empty when the location doesn't use revenue centers or
    # the order wasn't assigned one.
    revenue_center: str = ""
    # Item sales on this order grouped by Toast sales category name (Food,
    # Liquor, Beer, Wine, ...), used for the food-vs-alcohol mix. Key "" means
    # the item carried no sales category. Amounts are summed selection prices,
    # which exclude order-level discounts and service charges, so they do NOT
    # add up to net_sales — treat them as a mix, not a total.
    sales_by_category: dict[str, float] = field(default_factory=dict)
    # Whole-ticket kitchen time in minutes: first item fired -> last item marked
    # READY on the KDS. None when the kitchen didn't bump this ticket.
    ticket_ready_minutes: float | None = None

    @property
    def gross_sales(self) -> float:
        return self.net_sales + self.tax


@dataclass
class LocationDataset:
    """Everything pulled for one location over the reporting window."""

    location: Location
    time_entries: list[TimeEntry] = field(default_factory=list)
    orders: list[OrderRecord] = field(default_factory=list)
