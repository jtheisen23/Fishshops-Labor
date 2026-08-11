"""Diagnostic: per-location item fulfillment-status + timing breakdown.

Answers: does any location (Oceanside in particular) record item "READY"/bump
events and ready timestamps, or is everything stuck at "SENT" like Pacific Beach?

Runs in CI (needs Toast creds). Structural summary only, no customer PII.
Delete this + .github/workflows/probe.yml once we've read the results.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from statistics import mean, median

from toast_reports.client import ToastClient
from toast_reports.config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("probe")

_DAYS_BACK = 4
_NON_TIMING = {"createdDate", "modifiedDate", "voidDate", "voidBusinessDate"}


def _parse(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _nested_date_paths(obj, prefix="", out=None):
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("Date") and v and str(v)[:4] != "1970":
                out.add(prefix + k)
            _nested_date_paths(v, prefix + k + ".", out)
    elif isinstance(obj, list):
        for it in obj[:3]:
            _nested_date_paths(it, prefix, out)
    return out


def main() -> int:
    cfg = load_config("config.yaml")
    if not cfg.credentials.is_complete or not cfg.locations:
        log.error("Missing credentials or locations.")
        return 2
    client = ToastClient(cfg.credentials)

    end = datetime.combine(date.today(), time.max)
    start = datetime.combine(date.today() - timedelta(days=_DAYS_BACK), time.min)

    for loc in cfg.locations:
        # Dining-option guid -> behavior, fetched PER LOCATION (guids differ).
        do_map: dict[str, str] = {}
        try:
            raw = client._get("/config/v2/diningOptions", loc.guid)
            for o in raw if isinstance(raw, list) else []:
                do_map[o.get("guid")] = o.get("behavior")
        except Exception as e:  # noqa: BLE001
            log.info("diningOptions config unavailable for %s: %s", loc.name, e)

        orders = client._get_paginated_ranged("/orders/v2/ordersBulk", loc.guid, start, end)
        statuses: Counter = Counter()
        sel_total = 0
        # READY fired->ready durations (minutes), by dining behavior.
        durs: dict[str, list] = defaultdict(list)
        bad = 0
        for o in orders:
            behavior = do_map.get((o.get("diningOption") or {}).get("guid")) or "?"
            for chk in o.get("checks") or []:
                for sel in chk.get("selections") or []:
                    sel_total += 1
                    stt = sel.get("fulfillmentStatus")
                    statuses[stt] += 1
                    if stt == "READY" and not sel.get("voided"):
                        c, m = _parse(sel.get("createdDate")), _parse(sel.get("modifiedDate"))
                        if c and m:
                            mins = (m - c).total_seconds() / 60.0
                            if -1 < mins < 240:
                                durs[behavior].append(mins)
                            else:
                                bad += 1
        log.info("=== %s ===", loc.name)
        log.info("  orders=%d selections=%d  status=%s", len(orders), sel_total, dict(statuses))
        ready_total = sum(len(v) for v in durs.values())
        if ready_total:
            log.info("  fired->ready (createdDate->modifiedDate, READY items), out-of-range dropped=%d:", bad)
            for beh in sorted(durs):
                v = durs[beh]
                if not v:
                    continue
                v_sorted = sorted(v)
                p90 = v_sorted[min(len(v_sorted) - 1, int(len(v_sorted) * 0.9))]
                log.info("    %-10s n=%-5d mean=%5.1f  median=%5.1f  p90=%5.1f min",
                         beh, len(v), mean(v), median(v), p90)
        else:
            log.info("  no READY items -> no fired->ready timing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
