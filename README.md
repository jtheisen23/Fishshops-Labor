# Toast POS → Weekly Labor & Sales Reports

Pulls data from the [Toast POS API](https://doc.toasttab.com/) and builds
**weekly reports by location** covering labor, sales, and transaction volume.

Two outputs per run:

- **`output/weekly-report-<date>.xlsx`** — an Excel workbook with a `Summary`
  tab (every week × location, plus a company total per week), one tab per
  location, and a `Revenue Centers` tab of daily order counts.
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

Orders are also broken out by **revenue center** — the areas a location rings
sales under in Toast (Dining Room, Bar, Patio, To-Go, …):

- **Each location's page** gets an *Orders per day by revenue center* grid — one
  row per business day (most recent first, last 28 days of trading), one column
  per revenue center, shaded by volume, with day and center totals.
- **The overview page** gets an *Orders by revenue center* table — average
  orders per day per center for every location side by side.
- **The workbook** gets a `Revenue Centers` tab of tidy
  `Location | Business Date | Revenue Center | Orders` rows, ready to pivot.

Names come from Toast's config API (`/config/v2/revenueCenters`, falling back to
`/config/v1/`); orders reference a center by GUID only. Two things to know:

- A location that doesn't use revenue centers is **left off these boards**
  entirely — the breakdown would just restate its daily order count. Every run
  logs what it found per location (`Revenue centers for Pacific Beach: …`), so
  that log line is the first place to look if a board is missing.
- Orders at a location that *does* use revenue centers but that weren't assigned
  one land in an **Unassigned** column, which always sorts last. It's there so
  the daily totals reconcile with transaction counts elsewhere in the report.

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
