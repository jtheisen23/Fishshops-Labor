"""Toast API client.

Pulls raw Labor (time entries) and Orders data for a location over a date range
and maps them into the normalized models. Handles pagination, rate limiting
(HTTP 429) and transient errors with exponential backoff.

NOTE ON FIELD NAMES: Toast's JSON schema varies slightly by API version and by
what your integration is provisioned for. The mapping functions below
(``_map_time_entry`` / ``_map_order``) are the single place to adjust if a field
lands somewhere different in your account. Every access is defensive so a missing
field degrades to zero rather than crashing the whole run.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .auth import ToastAuth
from .config import ToastCredentials
from .models import Location, OrderRecord, TimeEntry

log = logging.getLogger(__name__)

_MAX_RETRIES = 5
_ORDERS_PAGE_SIZE = 100

# Toast's /labor/v1/timeEntries rejects date ranges greater than 30 days with a
# 400. ordersBulk is more lenient, but we chunk both requests to the same safe
# span so any reporting window (including larger --weeks) never trips the cap.
_MAX_QUERY_RANGE_DAYS = 30


def _date_chunks(start: datetime, end: datetime, max_days: int):
    """Yield (chunk_start, chunk_end) datetime pairs that tile [start, end] with
    each span at most ``max_days`` days, so per-request Toast range caps aren't hit."""
    step = timedelta(days=max_days)
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def _iso_utc(dt: datetime) -> str:
    """Toast expects ISO-8601 with millis and a zone, e.g. 2026-08-01T00:00:00.000+0000."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_business_date(value: str | int | None, fallback: date) -> date:
    """Business date arrives as yyyyMMdd (int or str) or an ISO date."""
    if value is None:
        return fallback
    s = str(value)
    try:
        if len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, IndexError):
        return fallback


class ToastClient:
    def __init__(self, credentials: ToastCredentials, session: requests.Session | None = None):
        self._creds = credentials
        self._session = session or requests.Session()
        self._auth = ToastAuth(credentials, self._session)

    # -- HTTP plumbing -----------------------------------------------------

    def _get(self, path: str, restaurant_guid: str, params: dict | None = None) -> object:
        url = f"{self._creds.host}{path}"
        for attempt in range(_MAX_RETRIES):
            headers = {
                "Authorization": f"Bearer {self._auth.token()}",
                "Toast-Restaurant-External-ID": restaurant_guid,
                "Accept": "application/json",
            }
            resp = self._session.get(url, headers=headers, params=params, timeout=60)

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _backoff_seconds(attempt, resp)
                log.warning("Toast %s -> %s; retry %d/%d in %.1fs",
                            path, resp.status_code, attempt + 1, _MAX_RETRIES, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"Toast request to {path} failed after {_MAX_RETRIES} retries")

    def _get_paginated(self, path: str, restaurant_guid: str, params: dict) -> list[dict]:
        """Page through an endpoint that supports ?page=&pageSize=."""
        results: list[dict] = []
        page = 1
        while True:
            page_params = {**params, "page": page, "pageSize": _ORDERS_PAGE_SIZE}
            batch = self._get(path, restaurant_guid, page_params)
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if len(batch) < _ORDERS_PAGE_SIZE:
                break
            page += 1
        return results

    def _get_ranged(self, path: str, guid: str, q_start: datetime, q_end: datetime) -> list[dict]:
        """Fetch a date-filtered endpoint across [q_start, q_end] in <=30-day
        chunks (Toast's per-request range cap), concatenating and de-duplicating
        rows by guid (chunks share a boundary instant)."""
        return self._collect_chunks(
            lambda cs, ce: self._get(path, guid, {"startDate": _iso_utc(cs), "endDate": _iso_utc(ce)}),
            q_start, q_end,
        )

    def _get_paginated_ranged(
        self, path: str, guid: str, q_start: datetime, q_end: datetime
    ) -> list[dict]:
        """Like _get_ranged but for paginated endpoints (orders)."""
        return self._collect_chunks(
            lambda cs, ce: self._get_paginated(path, guid, {"startDate": _iso_utc(cs), "endDate": _iso_utc(ce)}),
            q_start, q_end,
        )

    @staticmethod
    def _collect_chunks(fetch, q_start: datetime, q_end: datetime) -> list[dict]:
        seen: set = set()
        rows: list[dict] = []
        for c_start, c_end in _date_chunks(q_start, q_end, _MAX_QUERY_RANGE_DAYS):
            batch = fetch(c_start, c_end)
            for r in (batch if isinstance(batch, list) else []):
                key = r.get("guid")
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                rows.append(r)
        return rows

    # -- Public data pulls -------------------------------------------------

    def get_restaurant_name(self, location: Location) -> str | None:
        """Fetch the human-readable restaurant name from Toast's config.

        Best-effort: a name is a nicety, not required for the numbers, so any
        failure (missing scope, endpoint variation) just returns None and the
        caller keeps whatever name it already had.
        """
        try:
            raw = self._get(f"/restaurants/v1/restaurants/{location.guid}", location.guid)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("Could not fetch name for %s: %s", location.guid, exc)
            return None
        if not isinstance(raw, dict):
            return None
        general = raw.get("general") or {}
        # Prefer the location-specific name (e.g. "Pacific Beach") over the group
        # name (e.g. "Restaurant Caddies"), so labels/buttons stay short.
        return general.get("locationName") or general.get("name") or None

    def get_jobs(self, location: Location) -> dict[str, str]:
        """Map job GUID -> job title (e.g. "Line Cook", "Server") from the Labor
        API. Time entries only reference a job by GUID, so we resolve titles here.
        Best-effort: on failure returns {} and titles fall back to the raw ref."""
        try:
            raw = self._get("/labor/v1/jobs", location.guid)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("Could not fetch jobs for %s: %s", location.guid, exc)
            return {}
        out: dict[str, str] = {}
        for j in raw if isinstance(raw, list) else []:
            guid, title = j.get("guid"), j.get("title")
            if guid and title:
                out[guid] = title
        return out

    def get_time_entries(
        self, location: Location, start: datetime, end: datetime
    ) -> list[TimeEntry]:
        lo, hi = start.date(), end.date()
        jobs = self.get_jobs(location)  # guid -> title
        # Pad the UTC query so no local-time business date at the edges is missed,
        # chunk to stay under Toast's 30-day cap, then keep only entries whose
        # business date is in-window.
        rows = self._get_ranged(
            "/labor/v1/timeEntries", location.guid,
            start - timedelta(days=1), end + timedelta(days=1),
        )
        entries = [_map_time_entry(r, location, hi, jobs) for r in rows]
        return [e for e in entries if lo <= e.business_date <= hi]

    def get_orders(
        self, location: Location, start: datetime, end: datetime
    ) -> list[OrderRecord]:
        lo, hi = start.date(), end.date()
        # Pad the UTC query by a day on each side to capture orders whose local
        # business date lands at the window edges (timezone offset), chunk to stay
        # under Toast's range cap, then filter strictly by business date.
        raw = self._get_paginated_ranged(
            "/orders/v2/ordersBulk", location.guid,
            start - timedelta(days=1), end + timedelta(days=1),
        )
        orders = [_map_order(r, location, hi) for r in raw]
        return [o for o in orders if lo <= o.business_date <= hi]


def _backoff_seconds(attempt: int, resp: requests.Response) -> float:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return min(2.0 ** attempt, 30.0)


# -- Raw JSON -> model mappings -------------------------------------------


def _map_time_entry(
    row: dict, location: Location, fallback_date: date, jobs: dict[str, str] | None = None
) -> TimeEntry:
    job_ref = row.get("jobReference") or {}
    emp_ref = row.get("employeeReference") or {}
    # Resolve the real job title from the jobs map (guid -> title); fall back to
    # any inline title, then to a placeholder.
    job_title = (jobs or {}).get(job_ref.get("guid")) or row.get("jobTitle") or "Unassigned"
    return TimeEntry(
        location_guid=location.guid,
        business_date=_parse_business_date(row.get("businessDate"), fallback_date),
        employee_id=str(emp_ref.get("guid", row.get("employeeExternalId", "unknown"))),
        job=str(job_title),
        in_date=_parse_dt(row.get("inDate")),
        out_date=_parse_dt(row.get("outDate")),
        regular_hours=float(row.get("regularHours") or 0.0),
        overtime_hours=float(row.get("overtimeHours") or 0.0),
        hourly_wage=float(row.get("hourlyWage") or 0.0),
        tips=float(row.get("declaredCashTips") or 0.0)
        + float(row.get("nonCashTips") or 0.0),
    )


def _map_order(row: dict, location: Location, fallback_date: date) -> OrderRecord:
    checks = row.get("checks") or []
    net_sales = 0.0
    tax = 0.0
    tips = 0.0
    guests = int(row.get("numberOfGuests") or 0)

    for check in checks:
        # `amount` is the pre-tax total for the check; `taxAmount` the tax.
        net_sales += float(check.get("amount") or 0.0)
        tax += float(check.get("taxAmount") or 0.0)
        for payment in check.get("payments") or []:
            tips += float(payment.get("tipAmount") or 0.0)

    return OrderRecord(
        location_guid=location.guid,
        business_date=_parse_business_date(row.get("businessDate"), fallback_date),
        order_guid=str(row.get("guid", "")),
        opened_at=_parse_dt(row.get("openedDate") or row.get("createdDate")),
        guest_count=guests,
        check_count=len(checks),
        net_sales=net_sales,
        tax=tax,
        tips=tips,
        voided=bool(row.get("voided") or False),
    )
