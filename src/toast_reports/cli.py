"""Command-line entry point.

Examples:
    # Run against the real Toast API for the last 4 weeks:
    python -m toast_reports --weeks 4

    # See output immediately with generated sample data (no credentials needed):
    python -m toast_reports --sample --weeks 6

    # Explicit date range:
    python -m toast_reports --start 2026-07-01 --end 2026-08-01
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime, time, timedelta, timezone

from .aggregate import aggregate_daily, aggregate_weekly, labor_by_role_last_week, week_start_of
from .client import ToastClient
from .config import AppConfig, load_config
from .models import Location, LocationDataset
from .reports.excel import render_workbook
from .reports.html import render_dashboard
from .sample_data import build_sample_datasets

log = logging.getLogger("toast_reports")

# Names auto-generated in config.py when only GUIDs are supplied, e.g. "Location 3".
_PLACEHOLDER_NAME_RE = re.compile(r"^Location \d+$")


def _is_placeholder_name(loc: Location) -> bool:
    return loc.name == loc.guid or bool(_PLACEHOLDER_NAME_RE.match(loc.name))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="toast_reports", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    p.add_argument("--weeks", type=int, default=4, help="Number of trailing weeks to report (default: 4)")
    p.add_argument("--start", type=_date, help="Start date YYYY-MM-DD (overrides --weeks)")
    p.add_argument("--end", type=_date, help="As-of date YYYY-MM-DD (default: today)")
    p.add_argument("--sample", action="store_true", help="Use generated sample data instead of the Toast API")
    p.add_argument("--output-dir", help="Override output directory")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _synced_at_label() -> str:
    """Human-readable 'last synced' stamp in San Diego time. This reflects when
    the report was built (every 4 hours in CI), which is when the current-week
    figures last refreshed."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        now = datetime.now(timezone.utc)
    # e.g. "Aug 5, 2026 at 2:05 PM PDT" (strip leading zeros for readability).
    stamp = now.strftime("%b %d, %Y at %I:%M %p %Z")
    return stamp.replace(" 0", " ").replace("at 0", "at ")


def _date_range_label(lo: date, hi: date) -> str:
    if hi < lo:
        return ""
    if lo == hi:
        return f"{_MONTHS[lo.month - 1]} {lo.day}"
    return f"{_MONTHS[lo.month - 1]} {lo.day} – {_MONTHS[hi.month - 1]} {hi.day}"


def _split_datasets(datasets: list[LocationDataset], keep) -> list[LocationDataset]:
    """Return copies of the datasets keeping only orders/time entries whose
    business date satisfies `keep(business_date)`."""
    out: list[LocationDataset] = []
    for ds in datasets:
        nd = LocationDataset(location=ds.location)
        nd.orders = [o for o in ds.orders if keep(o.business_date)]
        nd.time_entries = [t for t in ds.time_entries if keep(t.business_date)]
        out.append(nd)
    return out


def _window_totals(ds: LocationDataset, lo: date, hi: date) -> dict:
    """Sum sales + labor for one location over [lo, hi] (inclusive)."""
    net = cost = hours = 0.0
    txns = 0
    for o in ds.orders:
        if not o.voided and lo <= o.business_date <= hi:
            net += o.net_sales
            txns += 1
    for t in ds.time_entries:
        if lo <= t.business_date <= hi:
            cost += t.labor_cost
            hours += t.total_hours
    return {"netSales": net, "transactions": txns, "laborCost": cost, "laborHours": hours}


def _apply_role_exclusions(datasets: list[LocationDataset], exclude_roles: list[str]) -> None:
    """Drop time entries whose job title is in the exclusion list (case-insensitive),
    so excluded roles (e.g. Register) count toward no labor figure anywhere."""
    excluded = {r.strip().lower() for r in exclude_roles if r.strip()}
    if not excluded:
        return
    for ds in datasets:
        before = len(ds.time_entries)
        ds.time_entries = [te for te in ds.time_entries if te.job.strip().lower() not in excluded]
        removed = before - len(ds.time_entries)
        if removed:
            log.info("Excluded %d time entries (%s) for %s",
                     removed, ", ".join(sorted(excluded)), ds.location.name)


def _log_job_titles(datasets: list[LocationDataset]) -> None:
    """Log distinct job titles and their total hours — used to define the
    title -> role bucket mapping for the labor-by-role board."""
    totals: dict[str, float] = {}
    for ds in datasets:
        for te in ds.time_entries:
            totals[te.job] = totals.get(te.job, 0.0) + te.total_hours
    if totals:
        log.info("Job titles found (title: total hours):")
        for title, hrs in sorted(totals.items(), key=lambda kv: -kv[1]):
            log.info("  %-28s %.1f", title, hrs)


def _resolve_window(args: argparse.Namespace, week_start: str) -> tuple[date, date]:
    """Pull window ends at `as_of` (today) so the in-progress week is included;
    the split into completed vs current weeks happens downstream. Start reaches
    back `--weeks` completed weeks before the current week."""
    as_of = args.end or date.today()
    if args.start:
        return args.start, as_of
    current_week_start = week_start_of(as_of, week_start)
    start = current_week_start - timedelta(days=args.weeks * 7)
    return start, as_of


def _pull_live(config: AppConfig, start: date, end: date) -> list[LocationDataset]:
    if not config.credentials.is_complete:
        log.error("Missing Toast credentials. Set TOAST_CLIENT_ID / TOAST_CLIENT_SECRET "
                  "(see .env.example), or run with --sample.")
        sys.exit(2)
    if not config.locations:
        log.error("No locations configured. Add them to config.yaml or set "
                  "TOAST_RESTAURANT_GUIDS.")
        sys.exit(2)

    client = ToastClient(config.credentials)
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.max)

    datasets: list[LocationDataset] = []
    for loc in config.locations:
        # If the location only has an auto-generated placeholder name (e.g. from
        # TOAST_RESTAURANT_GUIDS), ask Toast for the real restaurant name.
        if _is_placeholder_name(loc):
            real_name = client.get_restaurant_name(loc)
            if real_name:
                loc = Location(guid=loc.guid, name=real_name, timezone=loc.timezone)
        log.info("Pulling %s (%s)…", loc.name, loc.guid)
        ds = LocationDataset(location=loc)
        ds.orders = client.get_orders(loc, start_dt, end_dt)
        ds.time_entries = client.get_time_entries(loc, start_dt, end_dt)
        log.info("  %d orders, %d time entries", len(ds.orders), len(ds.time_entries))
        datasets.append(ds)
    return datasets


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    config = load_config(args.config)
    output_dir = args.output_dir or config.report.output_dir
    start, end = _resolve_window(args, config.report.week_start)
    log.info("Reporting window: %s -> %s", start, end)

    if args.sample:
        log.info("Using generated sample data (no API calls).")
        weeks = max(args.weeks, ((end - start).days // 7) + 2)
        datasets = build_sample_datasets(config.locations or None, weeks=weeks, end=end)
        # Trim generated data to the exact window (the generator overshoots).
        datasets = _split_datasets(datasets, lambda bd: start <= bd <= end)
    else:
        datasets = _pull_live(config, start, end)

    _apply_role_exclusions(datasets, config.report.exclude_roles)
    _log_job_titles(datasets)

    ws = config.report.week_start
    cws = week_start_of(end, ws)                 # current week start (Monday)
    cur_hi = end                                 # current week through today (running)
    day = timedelta(days=1)

    # Completed weeks feed all the weekly boards; the current (in-progress) week
    # feeds the current-week table + top KPI row.
    completed = _split_datasets(datasets, lambda bd: bd < cws)
    current = _split_datasets(datasets, lambda bd: cws <= bd <= cur_hi)

    metrics = aggregate_weekly(completed, week_start=ws)
    if not metrics:
        log.warning("No data in the reporting window — nothing to write.")
        return 1
    current_daily = aggregate_daily(current)
    labor_by_role = labor_by_role_last_week(completed, ws, config.report.role_groups)

    # Two-row KPI inputs per location: current week (vs same days prior week) and
    # prior completed week (vs the week before it).
    kpi_by_loc = {
        ds.location.name: {
            "current": _window_totals(ds, cws, cur_hi),
            "priorSame": _window_totals(ds, cws - 7 * day, cur_hi - 7 * day),
            "prior": _window_totals(ds, cws - 7 * day, cws - day),
            "priorPrev": _window_totals(ds, cws - 14 * day, cws - 8 * day),
        }
        for ds in datasets
    }
    kpi_dates = {
        "current": _date_range_label(cws, cur_hi),
        "prior": _date_range_label(cws - 7 * day, cws - day),
    }
    has_current = cur_hi >= cws

    stamp = end.isoformat()
    xlsx_path = render_workbook(metrics, f"{output_dir}/weekly-report-{stamp}.xlsx", config.report.title)
    html_path = render_dashboard(
        metrics, f"{output_dir}/index.html", config.report.title,
        current_daily=current_daily, labor_by_role=labor_by_role,
        kpi_by_loc=kpi_by_loc, kpi_dates=kpi_dates, has_current=has_current,
        synced_at=_synced_at_label(),
    )

    log.info("Wrote %s", xlsx_path)
    log.info("Wrote %s", html_path)
    print(f"\nExcel workbook : {xlsx_path}")
    print(f"HTML dashboard : {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
