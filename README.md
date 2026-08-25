# Toast POS → Weekly Labor & Sales Reports

Pulls data from the [Toast POS API](https://doc.toasttab.com/) and builds
**weekly reports by location** covering labor, sales, and transaction volume.

Two outputs per run:

- **`output/weekly-report-<date>.xlsx`** — an Excel workbook with a `Summary`
  tab (every week × location, plus a company total per week), one tab per
  location, and a `Revenue Centers` tab of daily sales and transaction counts.
- **`output/index.html`** — a self-contained, interactive dashboard (no CDN, no
  network) that opens in any browser and can be published straight to GitHub
  Pages. Theme-aware, colorblind-safe, with hover tooltips and a full data table.

## Metrics

Per location, per week:

| Metric | Meaning |
|---|---|
| Net sales | Pre-tax revenue collected |
| Gross sales | Net sales + tax |
| Tax, Tips | Collected tax and tips |
| Transactions | Order/ticket count |
| Guests | Guest count |
| Avg check | Net sales ÷ transactions |
| Labor hours | Regular + overtime hours worked |
| Overtime hours | Overtime portion |
| Labor cost | Wage cost (overtime paid at 1.5×) |
| **Labor %** | Labor cost ÷ net sales — the efficiency number managers watch |
| **Sales / labor hour** | Net sales ÷ labor hours |

## Revenue centers

Sales and transactions are also broken out by **revenue center** — the areas a
location rings sales under in Toast (Dining Room, Bar, Patio, To-Go, …). Both
metrics appear together everywhere: net sales (pre-tax, as everywhere else in
the report) with the transaction count beneath it.

- **Each location's page** gets a *Sales & transactions per day by revenue
  center* grid — one row per business day (most recent first, last 28 days of
  trading), one column per revenue center, shaded by sales, with day totals, a
  center total row, and each center's share of the location's sales.
- **The overview page** gets a *Sales & transactions by revenue center* table —
  every location side by side, so you can compare one shop's patio against
  another's. Each location is measured over its own days of data, so a location
  that opened mid-window isn't understated.
- **The workbook** gets a `Revenue Centers` tab of tidy
  `Location | Business Date | Revenue Center | Transactions | Net Sales` rows,
  ready to pivot.

Note these boards cover the last 28 days of trading, which includes the running
current week — so they won't tie out against the `Summary` tab or the weekly
charts, which cover completed weeks only.

### Food vs. alcohol

Each location's page also carries a *Food vs. alcohol by revenue center* board,
built from the Toast **sales category** on every line item (Food, Liquor, Beer,
Wine, N/A Beverage, …), resolved via `/config/v2/salesCategories`.

Tickets routinely hold both a burger and a beer, so the transaction columns are
a four-way split — **alcohol only / food only / both / neither** — which adds to
100%. The dollar mix on the right is the actual food-vs-alcohol ratio. Those
sales are summed item prices by category, before order-level discounts and
service charges, so they're a mix, not a total, and won't equal net sales.

Category names differ by restaurant, so the Food/Alcohol mapping is
configurable under `report.sales_category_groups` (see `config.yaml`), with
built-in defaults plus keyword matching as a fallback. Non-alcoholic names are
explicitly ruled out before the keyword pass, so "Non-Alcoholic" and
"N/A Beverage" don't get counted as booze. Every run logs each category it found
and the bucket it landed in:

```
Sales categories for Oceanside: Food -> Food (6,734 orders / $162,112),
  Beer -> Alcohol (879 orders / $10,684), N/A Beverage -> Other (218 orders / $4,476)
```

If a house category reads wrong there (a "Beer Cheese Bites" food item, say),
pin it explicitly in `sales_category_groups` — an exact name match always beats
the keyword guess. A location whose items carry no sales categories is left off
this board entirely.

### Notes on the Toast lookups

Names come from Toast's config API (`/config/v2/revenueCenters`, falling back to
`/config/v1/`); orders reference a center by GUID only. Two things to know:

- A location that doesn't use revenue centers is **left off these boards**
  entirely — the breakdown would just restate its daily order count. Every run
  logs what it found per location (`Revenue centers for Pacific Beach: Dining
  Room (798 txns / $27,603), …`), so that log line is the first place to look if
  a board is missing.
- Orders at a location that *does* use revenue centers but that weren't assigned
  one land in an **Unassigned** column, which always sorts last. It's there so
  the daily totals reconcile with the sales and transaction counts elsewhere in
  the report rather than quietly going missing.

## Quick start (see it now, no credentials)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Generate reports from built-in sample data:
PYTHONPATH=src python -m toast_reports --sample --weeks 6
```

Then open `output/index.html` in a browser and `output/weekly-report-*.xlsx` in
Excel.

## Running against the real Toast API

1. **Credentials.** Copy `.env.example` → `.env` and fill in your Toast machine
   client ID/secret:

   ```
   TOAST_API_HOST=https://ws-api.toasttab.com
   TOAST_CLIENT_ID=...
   TOAST_CLIENT_SECRET=...
   TOAST_USER_ACCESS_TYPE=TOAST_MACHINE_CLIENT
   ```

   `.env` is git-ignored — secrets never get committed.

2. **Locations.** Copy `config.example.yaml` → `config.yaml` and list each
   restaurant's GUID and a friendly name:

   ```yaml
   locations:
     - name: "Fish Shop — Downtown"
       guid: "your-restaurant-guid"
       timezone: "America/New_York"
   report:
     week_start: "monday"   # or "sunday"
     output_dir: "output"
     title: "Weekly Labor & Sales Report"
   ```

   (Alternatively set `TOAST_RESTAURANT_GUIDS=guid1,guid2` in the environment.
   Attach display names with a colon — `guid1:Pacific Beach,guid2:Encinitas` —
   and that name wins over Toast's own restaurant name, which is often the
   street address.)

### Branding: add a logo

Drop a logo file at **`assets/logo.png`** (also accepts `.svg`/`.jpg`/`.webp`, or
point `DASHBOARD_LOGO` at another path). It's embedded as a data URI and shown
centered at the top of every dashboard page. No file → no logo, no error.

3. **Run.**

   ```bash
   PYTHONPATH=src python -m toast_reports --weeks 4
   # or an explicit range:
   PYTHONPATH=src python -m toast_reports --start 2026-07-01 --end 2026-08-01
   ```

By default the window covers the last N **completed** weeks, so the "latest
week" is always a full week and week-over-week deltas stay meaningful. Pass
`--include-current-week` to include the in-progress week.

## Scheduling + web hosting (free)

`.github/workflows/weekly-report.yml` runs the report every Monday and on demand
(`workflow_dispatch`), uploads the workbook as a build artifact, and publishes
the HTML dashboard to **GitHub Pages** — both free.

To enable it:

1. In the repo: **Settings → Secrets and variables → Actions** — add
   `TOAST_CLIENT_ID`, `TOAST_CLIENT_SECRET`, `TOAST_API_HOST`,
   `TOAST_USER_ACCESS_TYPE`, and `TOAST_RESTAURANT_GUIDS`.
2. **Settings → Pages** — set **Source: GitHub Actions**.
3. Push to the default branch. The dashboard will be live at your Pages URL and
   refresh weekly.

## How it's put together

```
src/toast_reports/
  auth.py          Toast auth (client-credentials, token cache/refresh)
  client.py        API client: Labor (time entries) + Orders, pagination, retries
  models.py        Normalized records (TimeEntry, OrderRecord, ...)
  aggregate.py     Weekly roll-up by location + derived ratios
  sample_data.py   Seeded fixture data for the --sample mode
  reports/excel.py Excel workbook renderer
  reports/html.py  Self-contained HTML dashboard renderer
  cli.py           Entry point
tests/             Aggregation tests (the numbers that must be correct)
```

**A note on Toast field names.** Toast's JSON varies slightly by API version and
by what your integration is provisioned for. All raw-JSON → model mapping lives
in `client.py` (`_map_time_entry`, `_map_order`); every field access is
defensive (a missing field degrades to zero rather than crashing). If a number
looks off against your Toast reporting, that mapping is the one place to adjust —
each line is commented with the field it reads.

## Tests

```bash
pip install pytest
PYTHONPATH=src pytest
```
