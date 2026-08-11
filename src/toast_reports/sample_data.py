"""Deterministic sample data.

Lets you run the whole pipeline and see real-looking Excel + HTML output before
Toast credentials are wired up. Numbers are generated with a seeded RNG so runs
are reproducible. This is NOT random production data — it's a fixture.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from .models import Location, LocationDataset, OrderRecord, TimeEntry

# Mirror the real Toast job titles so sample output exercises role bucketing
# and the Register exclusion.
_JOBS = ["Kitchen", "Staff", "Register", "Shift Capt", "Shift Lead", "General Manager", "Chef"]
_WAGES = {
    "Kitchen": 18.0, "Staff": 14.0, "Register": 13.0, "Shift Capt": 20.0,
    "Shift Lead": 19.0, "General Manager": 30.0, "Chef": 28.0,
}


def build_sample_datasets(
    locations: list[Location] | None = None, weeks: int = 4, end: date | None = None
) -> list[LocationDataset]:
    """Generate ``weeks`` of orders + time entries per location, ending near ``end``."""
    if locations is None:
        locations = [
            Location("sample-guid-downtown", "Fish Shop — Downtown", "America/New_York"),
            Location("sample-guid-harbor", "Fish Shop — Harbor", "America/New_York"),
            Location("sample-guid-airport", "Fish Shop — Airport", "America/Chicago"),
        ]
    end = end or date(2026, 8, 2)
    start = end - timedelta(days=weeks * 7 - 1)

    # Mirror reality: only one location's kitchen bumps tickets on the KDS, so
    # only it produces fired->ready ticket times. Prefer an "Oceanside" location.
    bump_idx = next((i for i, l in enumerate(locations) if "oceanside" in l.name.lower()),
                    len(locations) - 1)
    # (source, behavior) channel mix for sample orders -> maps to Dine-in/Online/Takeout.
    _CHANNELS = [("In Store", "DINE_IN"), ("In Store", "DINE_IN"), ("In Store", "DINE_IN"),
                 ("Online", "TAKE_OUT"), ("API", "DELIVERY"), ("In Store", "TAKE_OUT")]

    datasets: list[LocationDataset] = []
    for li, loc in enumerate(locations):
        rng = random.Random(f"{loc.guid}:{end.isoformat()}")
        # Give each location a different volume profile.
        base_covers = 60 + li * 25
        bumps_tickets = li == bump_idx
        ds = LocationDataset(location=loc)

        day = start
        while day <= end:
            # Weekends busier.
            weekend = day.weekday() >= 4
            covers = int(base_covers * (1.5 if weekend else 1.0) * rng.uniform(0.8, 1.2))

            for _ in range(covers):
                net = round(rng.uniform(14, 55), 2)
                tax = round(net * 0.0825, 2)
                tip = round(net * rng.uniform(0.1, 0.22), 2)
                opened = datetime.combine(day, time(rng.randint(11, 21), rng.randint(0, 59)))
                source, behavior = rng.choice(_CHANNELS)
                # Only the KDS-bumping location has fired->ready times; vary a bit
                # by channel so the board shows a realistic spread.
                ticket_mins = None
                if bumps_tickets:
                    base = {"DINE_IN": 11.0, "TAKE_OUT": 12.0, "DELIVERY": 10.0}[behavior]
                    ticket_mins = round(max(2.0, rng.gauss(base, 3.5)), 2)
                ds.orders.append(
                    OrderRecord(
                        location_guid=loc.guid,
                        business_date=day,
                        order_guid=f"{loc.guid}-{day.isoformat()}-{len(ds.orders)}",
                        opened_at=opened,
                        guest_count=rng.randint(1, 4),
                        check_count=1,
                        net_sales=net,
                        tax=tax,
                        tips=tip,
                        source=source,
                        dining_behavior=behavior,
                        ticket_ready_minutes=ticket_mins,
                    )
                )

            # Staff the day: a handful of shifts across jobs.
            shift_count = 6 + (2 if weekend else 0)
            for _ in range(shift_count):
                job = rng.choice(_JOBS)
                reg = round(rng.uniform(4, 8), 2)
                ot = round(rng.uniform(0, 1.5), 2) if rng.random() < 0.15 else 0.0
                start_hr = rng.randint(8, 16)
                in_dt = datetime.combine(day, time(start_hr, 0))
                ds.time_entries.append(
                    TimeEntry(
                        location_guid=loc.guid,
                        business_date=day,
                        employee_id=f"emp-{rng.randint(1, 40)}",
                        job=job,
                        in_date=in_dt,
                        out_date=in_dt + timedelta(hours=reg + ot),
                        regular_hours=reg,
                        overtime_hours=ot,
                        hourly_wage=_WAGES[job],
                        tips=0.0,
                    )
                )
            day += timedelta(days=1)

        datasets.append(ds)
    return datasets
