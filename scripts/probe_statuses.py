"""Diagnostic: per-location item fulfillment-status + timing breakdown.

Answers: does any location (Oceanside in particular) record item "READY"/bump
events and ready timestamps, or is everything stuck at "SENT" like Pacific Beach?

Runs in CI (needs Toast creds). Structural summary only, no customer PII.
Delete this + .github/workflows/probe.yml once we've read the results.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, time, timedelta

from toast_reports.client import ToastClient
from toast_reports.config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("probe")

_DAYS_BACK = 4
_NON_TIMING = {"createdDate", "modifiedDate", "voidDate", "voidBusinessDate"}


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
        orders = client._get_paginated_ranged("/orders/v2/ordersBulk", loc.guid, start, end)
        statuses: Counter = Counter()
        order_statuses: Counter = Counter()
        interesting_paths: Counter = Counter()
        sel_total = 0
        for o in orders:
            order_statuses[o.get("fulfillmentStatus")] += 1
            for chk in o.get("checks") or []:
                for sel in chk.get("selections") or []:
                    sel_total += 1
                    statuses[sel.get("fulfillmentStatus")] += 1
                    for p in _nested_date_paths(sel):
                        base = p.split(".")[-1]
                        if base not in _NON_TIMING:
                            interesting_paths[p] += 1
        log.info("=== %s ===", loc.name)
        log.info("  orders=%d selections=%d", len(orders), sel_total)
        log.info("  selection fulfillmentStatus: %s", dict(statuses))
        log.info("  order     fulfillmentStatus: %s", dict(order_statuses))
        log.info("  extra timing date paths (non created/modified/void): %s",
                 dict(interesting_paths) or "NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
