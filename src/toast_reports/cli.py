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
import sys
from datetime import date, datetime, time, timedelta

from .aggregate import aggregate_weekly, week_start_of
from .client import ToastClient
from .config import AppConfig, load_config
from .models import LocationDataset
from .reports.excel import render_workbook
from .reports.html import render_dashboard
from .sample_data import build_sample_datasets

log = logging.getLogger("toast_reports")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="toast_reports", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    p.add_argument("--weeks", type=int, default=4, help="Number of trailing weeks to report (default: 4)")
    p.add_argument("--start", type=_date, help="Start date YYYY-MM-DD (overrides --weeks)")
    p.add_argument("--end", type=_date, help="End date YYYY-MM-DD (default: end of last completed week)")
    p.add_argument("--include-current-week", action="store_true",
                   help="Include the current, in-progress week (default: only completed weeks)")
    p.add_argument("--sample", action="store_true", help="Use generated sample data instead of the Toast API")
    p.add_argument("--output-dir", help="Override output directory")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _resolve_window(args: argparse.Namespace, week_start: str) -> tuple[date, date]:
    if args.start:
        return args.start, (args.end or date.today())

    # Anchor to whole weeks so the "latest week" is always a full week (deltas
    # stay meaningful). By default the window ends at the last *completed* week.
    today = args.end or date.today()
    this_week_start = week_start_of(today, week_start)
    if args.end:
        end = args.end
    elif args.include_current_week:
        end = today
    else:
        end = this_week_start - timedelta(days=1)  # last day of the prior week
    start = week_start_of(end, week_start) - timedelta(days=(args.weeks - 1) * 7)
    return start, end


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
        weeks = max(args.weeks, ((end - start).days // 7) + 1)
        datasets = build_sample_datasets(config.locations or None, weeks=weeks, end=end)
    else:
        datasets = _pull_live(config, start, end)

    metrics = aggregate_weekly(datasets, week_start=config.report.week_start)
    if not metrics:
        log.warning("No data in the reporting window — nothing to write.")
        return 1

    stamp = end.isoformat()
    xlsx_path = render_workbook(metrics, f"{output_dir}/weekly-report-{stamp}.xlsx", config.report.title)
    html_path = render_dashboard(metrics, f"{output_dir}/index.html", config.report.title)

    log.info("Wrote %s", xlsx_path)
    log.info("Wrote %s", html_path)
    print(f"\nExcel workbook : {xlsx_path}")
    print(f"HTML dashboard : {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
