"""Diagnostic: are there day/time cells where ticket times are constantly >20 min?

For each location with ready data (Oceanside), group tickets by local
(weekday, hour) over ~8 weeks and flag cells that are consistently slow:
median >= 20 min OR >50% of tickets over 20, requiring enough volume
(>=10 tickets) across >=3 different weeks so it's a pattern, not a fluke.
Structural summary only, no PII. Delete after reading.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from statistics import median

try:
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001
    ZoneInfo = None  # type: ignore

from toast_reports.client import ToastClient
from toast_reports.config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("probe")
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _tz(n):
    try:
        return ZoneInfo(n) if ZoneInfo else None
    except Exception:  # noqa: BLE001
        return None


def _local(dt, z):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(z) if z else dt


def _share_over(vals, thresh=20.0):
    return 100.0 * sum(1 for x in vals if x > thresh) / len(vals)


def main() -> int:
    cfg = load_config("config.yaml")
    client = ToastClient(cfg.credentials)
    end = datetime.combine(date.today(), time.max)
    start = datetime.combine(date.today() - timedelta(days=56), time.min)

    for loc in cfg.locations:
        orders = client.get_orders(loc, start, end)
        tk = [o for o in orders if o.ticket_ready_minutes is not None and not o.voided]
        if not tk:
            continue
        z = _tz(loc.timezone)
        cells: dict = defaultdict(list)
        weeks: dict = defaultdict(set)
        byday: dict = defaultdict(list)
        byhour: dict = defaultdict(list)
        for o in tk:
            t = _local(o.opened_at, z)
            if not t:
                continue
            key = (t.weekday(), t.hour)
            cells[key].append(o.ticket_ready_minutes)
            iso = t.isocalendar()
            weeks[key].add((iso[0], iso[1]))
            byday[t.weekday()].append(o.ticket_ready_minutes)
            byhour[t.hour].append(o.ticket_ready_minutes)

        log.info("=== %s : %d timed tickets over ~8 weeks ===", loc.name, len(tk))

        hot = []
        for key, v in cells.items():
            if len(v) >= 10 and len(weeks[key]) >= 3:
                md = median(v)
                over = _share_over(v)
                if md >= 20 or over >= 50:
                    hot.append((md, over, len(v), len(weeks[key]), key))
        hot.sort(reverse=True)
        if hot:
            log.info(" CONSTANTLY-SLOW cells (median>=20 OR >50%% over 20; >=10 tickets, >=3 weeks):")
            for md, over, n, w, (wd, h) in hot:
                log.info("   %-3s %2d:00-%2d:00  median=%4.1fm  %%>20=%3.0f%%  n=%d  weeks=%d",
                         _WD[wd], h, h + 1, md, over, n, w)
        else:
            log.info(" No (day, hour) cell is constantly over 20 min by the threshold.")

        log.info(" By weekday (median · %%>20):")
        for d in sorted(byday):
            v = byday[d]
            log.info("   %-3s  %4.1fm  %3.0f%%>20  (n=%d)", _WD[d], median(v), _share_over(v), len(v))
        log.info(" By hour (median · %%>20):")
        for h in sorted(byhour):
            v = byhour[h]
            log.info("   %2d:00  %4.1fm  %3.0f%%>20  (n=%d)", h, median(v), _share_over(v), len(v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
