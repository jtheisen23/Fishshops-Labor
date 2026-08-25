"""Food vs alcohol mix, by revenue center.

Toast tags every menu item with a **sales category** (Food, Liquor, Beer, Wine,
N/A Beverage, …). This module maps those category names onto two buckets that
operators actually manage to — Food and Alcohol — and reports how each revenue
center's tickets split.

A ticket routinely holds both a burger and a beer, so "what share of bar
transactions are food vs alcohol" has no clean binary answer. We report the
honest four-way split (alcohol only / food only / both / neither) alongside the
dollar mix, which is the number that behaves like a percentage.

Category names vary by restaurant, so the mapping is config-driven
(``report.sales_category_groups``) with a keyword fallback for anything not
listed. Every run logs which name landed in which bucket, so the mapping is
auditable against real data rather than assumed.
"""

from __future__ import annotations

from .aggregate import recent_order_days
from .models import LocationDataset

FOOD = "Food"
ALCOHOL = "Alcohol"
OTHER = "Other"

# Default name -> bucket mapping. Matched case-insensitively on the whole name;
# anything unlisted falls through to the keyword rules below.
DEFAULT_CATEGORY_GROUPS: dict[str, list[str]] = {
    ALCOHOL: ["Liquor", "Beer", "Wine", "Spirits", "Cocktails", "Draft", "Draft Beer",
              "Bottle Beer", "Bottled Beer", "Specialty Cocktails", "Alcohol", "Bar"],
    FOOD: ["Food", "Kitchen", "Entrees", "Appetizers", "Desserts"],
}

# "Non-Alcoholic" and "N/A Beverage" contain an alcohol keyword, so they have to
# be ruled out before the keyword pass or they'd be counted as booze.
_NON_ALCOHOLIC = ("n/a", "n a bev", "na bev", "non-alc", "nonalc", "non alc",
                  "no alcohol", "zero proof", "mocktail", "soft drink")
# "draft" earns its place: on a restaurant menu a category called "Draft" (or
# "HH Draft" for happy hour) is draft beer — the word never means anything else
# where a separate "Bottle Beer" category exists.
_ALCOHOL_KEYWORDS = ("liquor", "beer", "wine", "spirit", "cocktail", "alcohol",
                     "draft", "tequila", "whiskey", "whisky", "vodka", "gin",
                     "rum", "seltzer", "sake", "cider", "mezcal")
_FOOD_KEYWORDS = ("food", "kitchen", "entree", "appetizer", "dessert", "side",
                  "salad", "taco", "sandwich", "plate")


def classify_category(name: str, groups: dict | None = None) -> str:
    """Map one Toast sales-category name to FOOD / ALCOHOL / OTHER.

    An empty name (item carried no sales category) is OTHER — it can't be
    claimed as either without guessing.
    """
    label = (name or "").strip()
    if not label:
        return OTHER
    lower = label.lower()

    # An explicit config mapping always wins over the keyword heuristics.
    for bucket, names in (groups or DEFAULT_CATEGORY_GROUPS).items():
        for n in names:
            if lower == str(n).strip().lower():
                return bucket

    if any(k in lower for k in _NON_ALCOHOLIC):
        return OTHER
    if any(k in lower for k in _ALCOHOL_KEYWORDS):
        return ALCOHOL
    if any(k in lower for k in _FOOD_KEYWORDS):
        return FOOD
    return OTHER


def category_buckets(datasets: list[LocationDataset], groups: dict | None = None) -> dict:
    """Every sales-category name seen per location, with its bucket, item sales
    and how many orders carried it. Feeds the run log so the Food/Alcohol
    mapping can be checked against the restaurant's real category names.

    Returns ``{location: {name: {"bucket", "sales", "orders"}}}``; the empty
    name is reported under the key "" (uncategorized items).
    """
    out: dict = {}
    for ds in datasets:
        per: dict = {}
        for o in ds.orders:
            if o.voided:
                continue
            for name, amount in o.sales_by_category.items():
                row = per.setdefault(
                    name, {"bucket": classify_category(name, groups), "sales": 0.0, "orders": 0}
                )
                row["sales"] += amount
                row["orders"] += 1
        if per:
            out[ds.location.name] = per
    return out


def food_alcohol_by_revenue_center(
    datasets: list[LocationDataset], days: int = 28, groups: dict | None = None
) -> dict | None:
    """Per location, per revenue center: how tickets split between food and
    alcohol over the most recent ``days`` trading days.

    The window matches the revenue-center sales board, so the two read together.
    Only locations whose items carry sales categories appear — without them
    there is nothing to split.

    Returns ``{location: {"centers": [...], "rows": {center: {...}}}}`` or None.
    Each row carries transaction counts (``alcoholOnly`` / ``foodOnly`` /
    ``both`` / ``neither``, which sum to ``txns``) and item sales by bucket.
    """
    from .aggregate import UNASSIGNED_CENTER

    out: dict = {}
    for ds in datasets:
        window = set(recent_order_days(ds, days))
        if not window:
            continue

        rows: dict = {}
        categorized = False
        for o in ds.orders:
            if o.voided or o.business_date not in window:
                continue
            center = (o.revenue_center or "").strip() or UNASSIGNED_CENTER
            row = rows.setdefault(center, {
                "txns": 0, "alcoholOnly": 0, "foodOnly": 0, "both": 0, "neither": 0,
                "foodSales": 0.0, "alcoholSales": 0.0, "otherSales": 0.0,
            })
            row["txns"] += 1

            has_food = has_alcohol = False
            for name, amount in o.sales_by_category.items():
                bucket = classify_category(name, groups)
                if bucket == FOOD:
                    has_food = True
                    row["foodSales"] += amount
                elif bucket == ALCOHOL:
                    has_alcohol = True
                    row["alcoholSales"] += amount
                else:
                    row["otherSales"] += amount
                if name.strip():
                    categorized = True

            if has_food and has_alcohol:
                row["both"] += 1
            elif has_alcohol:
                row["alcoholOnly"] += 1
            elif has_food:
                row["foodOnly"] += 1
            else:
                row["neither"] += 1

        # Nothing to split when no item at this location carries a category.
        if not rows or not categorized:
            continue

        for row in rows.values():
            for key in ("foodSales", "alcoholSales", "otherSales"):
                row[key] = round(row[key], 2)
            item_sales = row["foodSales"] + row["alcoholSales"] + row["otherSales"]
            row["itemSales"] = round(item_sales, 2)

        # Busiest center first by transactions, Unassigned always last.
        centers = sorted(
            rows,
            key=lambda c: (c == UNASSIGNED_CENTER, -rows[c]["txns"], c),
        )
        out[ds.location.name] = {"centers": centers, "rows": rows}
    return out or None
