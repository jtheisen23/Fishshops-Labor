"""One-off diagnostic: what order/kitchen timing does Toast return, and how is it
split by dining option / source?

Runs in CI (needs Toast credentials). Prints a STRUCTURAL summary only — it never
prints customer names/phones/emails. The goal is to learn, for these restaurants:
  * which *Date fields exist on orders and on selections (line items),
  * the fulfillmentStatus values in use,
  * the order `source` values (in-store vs online/API),
  * the dining-option behaviors (DINE_IN / TAKE_OUT / DELIVERY),
  * and how often a fired->ready duration is actually computable, per bucket.

Delete this script + .github/workflows/probe.yml once we've read the results.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from toast_reports.client import ToastClient
from toast_reports.config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("probe")

_DAYS_BACK = 3          # small recent window per location
_MAX_EXAMPLES = 5       # full (PII-free) timing examples to print


def _date_fields(d: dict) -> dict:
    """Keys ending in 'Date' with a truthy value — Toast's timestamps."""
    return {k: v for k, v in d.items() if k.endswith("Date") and v}


def _parse(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    cfg = load_config("config.yaml")
    if not cfg.credentials.is_complete or not cfg.locations:
        log.error("Missing credentials or locations.")
        return 2
    client = ToastClient(cfg.credentials)
    loc = cfg.locations[0]
    log.info("Probing %s (last ~%d days)", loc.name, _DAYS_BACK)

    end = datetime.combine(date.today(), time.max)
    start = datetime.combine(date.today() - timedelta(days=_DAYS_BACK), time.min)
    orders = client._get_paginated_ranged("/orders/v2/ordersBulk", loc.guid, start, end)
    log.info("Fetched %d raw orders", len(orders))

    # Dining-option config: guid -> (name, behavior).
    do_map: dict[str, tuple] = {}
    for path in ("/config/v2/diningOptions", "/config/v1/diningOptions"):
        try:
            raw = client._get(path, loc.guid)
            for o in raw if isinstance(raw, list) else []:
                do_map[o.get("guid")] = (o.get("name"), o.get("behavior"))
            if do_map:
                log.info("Dining options (%s): %s", path,
                         {v[0]: v[1] for v in do_map.values()})
                break
        except Exception as e:  # noqa: BLE001
            log.info("diningOptions config %s not available: %s", path, e)

    order_date_keys: Counter = Counter()
    sel_date_keys: Counter = Counter()
    sel_all_keys: Counter = Counter()
    sources: Counter = Counter()
    behaviors: Counter = Counter()
    statuses: Counter = Counter()
    # bucket "BEHAVIOR/SOURCE" -> counts + fired->ready durations (minutes)
    stat: dict = defaultdict(lambda: {"orders": 0, "sel": 0, "fired": 0, "ready": 0, "durs": []})
    examples = 0

    for o in orders:
        src = o.get("source")
        sources[src] += 1
        do = o.get("diningOption") or {}
        name, behavior = do_map.get(do.get("guid"), (None, None))
        behaviors[behavior] += 1
        bucket = f"{behavior or '?'}/{src or '?'}"
        st = stat[bucket]
        st["orders"] += 1
        for k in _date_fields(o):
            order_date_keys[k] += 1

        for chk in o.get("checks") or []:
            for sel in chk.get("selections") or []:
                st["sel"] += 1
                for k in sel.keys():
                    sel_all_keys[k] += 1
                for k in _date_fields(sel):
                    sel_date_keys[k] += 1
                statuses[sel.get("fulfillmentStatus")] += 1
                fired = sel.get("firedDate")
                ready = sel.get("fulfilledDate") or sel.get("readyDate")
                if fired:
                    st["fired"] += 1
                if ready:
                    st["ready"] += 1
                f, r = _parse(fired), _parse(ready)
                if f and r:
                    st["durs"].append((r - f).total_seconds() / 60.0)

        if examples < _MAX_EXAMPLES:
            examples += 1
            log.info("--- example order [%s] ---", bucket)
            log.info("  order *Date: %s", _date_fields(o))
            log.info("  promisedDate=%s estimatedFulfillmentDate=%s",
                     o.get("promisedDate"), o.get("estimatedFulfillmentDate"))
            for chk in (o.get("checks") or [])[:1]:
                for sel in (chk.get("selections") or [])[:2]:
                    log.info("    selection keys: %s", sorted(sel.keys()))
                    log.info("    displayName=%s status=%s *Date=%s",
                             sel.get("displayName"), sel.get("fulfillmentStatus"),
                             _date_fields(sel))

    log.info("================ SUMMARY ================")
    log.info("sources:    %s", dict(sources))
    log.info("behaviors:  %s", dict(behaviors))
    log.info("statuses:   %s", dict(statuses))
    log.info("order  *Date fields (count): %s", dict(order_date_keys))
    log.info("select *Date fields (count): %s", dict(sel_date_keys))
    log.info("select all keys: %s", sorted(sel_all_keys))
    log.info("---- fired->ready by bucket (BEHAVIOR/SOURCE) ----")
    for bucket, st in sorted(stat.items()):
        durs = st["durs"]
        avg = sum(durs) / len(durs) if durs else None
        log.info("  %-22s orders=%-5d sel=%-6d fired=%-6d ready=%-6d computable=%-5d avg=%s min",
                 bucket, st["orders"], st["sel"], st["fired"], st["ready"], len(durs),
                 f"{avg:.1f}" if avg is not None else "n/a")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
