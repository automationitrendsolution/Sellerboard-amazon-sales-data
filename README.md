# Sellerboard Amazon Sales Data

Automated export of the **Sellerboard Profit & Loss report** for the `premiumcarproduct` account into **Power BI**.

Every hour the job signs in to Sellerboard with a saved session, downloads the P&L for the
**last 30 days, by day** for each Amazon marketplace, and replaces those 30 days in the Power BI database.
It is the automated version of the Scribe guide *"How to Export Amazon Sales Data from Sellerboard"* and replaces the
manual `profit_and_loss_upload.py` script.

| Marketplace | Power BI table (`powerbidb`) |
| --- | --- |
| Amazon.com | `combined_transposed_file_US` |
| Amazon.ca | `combined_transposed_file_CA` |

---

## Contents

- [How it works](#how-it-works)
- [Schedule](#schedule)
- [Requirements](#requirements)
- [Setup on a PC](#setup-on-a-pc)
- [Deploy to the cron server](#deploy-to-the-cron-server)
- [Configuration](#configuration)
- [Commands](#commands)
- [Monitoring](#monitoring)
- [Troubleshooting](#troubleshooting)
- [Data rules](#data-rules)
- [Project structure](#project-structure)
- [Development](#development)
- [Security](#security)

---

## How it works

```
 Sellerboard  ── Profit › P&L › "Last 30 days, by day" › Amazon.com / Amazon.ca › Filter › Download › .CSV
      │
      ▼
 data/incoming/   US_ProfitAndLoss-….csv   CA_ProfitAndLoss-….csv
      │
      ▼  one row per day; delete those days + insert, in one transaction
 Power BI         combined_transposed_file_US   combined_transposed_file_CA
      │
      ▼
 data/processed/  (uploaded)          data/failed/  (unusable export)
```

1. **Download.** A headless Chrome opens Sellerboard with the saved session, selects the account, marketplace and
   period, and checks that the page shows exactly those before it downloads the CSV.
2. **Read.** The CSV is read row by row by name (never by position) and turned into one record per day.
3. **Upload.** The days in the export replace the same days in Power BI, in one transaction. The row count is
   checked before the commit, and days outside the export are never touched.
4. **File.** The CSV moves to `data/processed/` after a successful upload, or to `data/failed/` if it cannot be read.

Amazon.com and Amazon.ca run independently: if one fails, the other still completes.

---

## Schedule

The job runs **every hour at :45 (UTC)**, 23 times a day.

| | |
| --- | --- |
| Runs at | 00:45 – 06:45 and 08:45 – 23:45 UTC (06:15 – 12:15 and 14:15 – 05:15 IST) |
| Skipped | 07:45 UTC: the other Sellerboard jobs (UK 07:30, US breakdown 08:00) share the same Sellerboard login and switch its account |
| Duration | about 3 minutes; a run that hangs is stopped after 20 minutes |
| Overlap | a second run that starts while one is still going stops immediately (exit 11) |
| Cleanup | once a day at 09:15 UTC, uploaded exports older than 30 days are deleted |

Each run refreshes all 30 days, so today's figures stay current during the day and Sellerboard's later corrections
reach Power BI automatically.

---

## Requirements

- **Python 3.13+** and [**uv**](https://docs.astral.sh/uv/)
- **Chromium for Playwright** (installed by the setup steps below)
- **Power BI database** user with `SELECT`, `INSERT` and `DELETE` on the two tables
- **Sellerboard** login with access to the `premiumcarproduct@gmail.com` account

---

## Setup on a PC

Use a PC (Windows or Mac) to create the Sellerboard session, test the job, and copy the session to the server.

```bash
git clone https://github.com/automationitrendsolution/Sellerboard-amazon-sales-data.git
cd Sellerboard-amazon-sales-data

uv sync                                    # create .venv from uv.lock
uv run playwright install chromium

cp .env.example .env                       # then fill in the passwords (see Configuration)
```

**Sign in to Sellerboard** (one time):

```bash
uv run python run.py --login
```

A Chrome window opens. Sign in, tick **Keep me logged in**, wait for the dashboard to load, then **close Chrome**.
The session is saved to `data/browser/session.json`.

> Use the Chrome sign-in. A session made with `--login --terminal` may fail to download Amazon.ca.

**Test without changing Power BI:**

```bash
uv run python run.py --fetch-only          # download both CSVs into data/incoming/
uv run python run.py --dry-run             # show what would be replaced; writes nothing
```

Both should finish with exit code `0`. Then do a real run with `uv run python run.py`.

---

## Deploy to the cron server

### 1. Copy the project

Clone the repository to `/data/cron/sellerboard-amazon-sales-data`, then copy these two private files from the PC.
They are not in Git:

| File | Contains |
| --- | --- |
| `.env` | database and Sellerboard passwords |
| `data/browser/session.json` | the signed-in Sellerboard session |

Do **not** copy `.venv/`, `logs/`, `data/incoming/`, `data/processed/` or `data/browser/chrome-profile/`.

### 2. Install

```bash
cd /data/cron/sellerboard-amazon-sales-data
uv sync
uv run playwright install --with-deps chromium
chmod 600 .env data/browser/session.json
```

### 3. Test

```bash
uv run python run.py --fetch-only && uv run python run.py --dry-run
```

Both must finish with exit code `0`. If you get exit code `3` (sign-in needed), sign in again on the PC and copy
`session.json` over again.

### 4. Install the cron job

Find the full path of `uv`, then open the crontab:

```bash
which uv
crontab -e
```

Paste the lines from [`deploy/crontab.example`](deploy/crontab.example). If `uv` is not at
`/root/.local/bin/uv`, change the path:

```cron
MAILTO=""
#<<<<<<<<<<<<<<<<<<<<< Sellerboard-Amazon-Sales-Data Start >>>>>>>>>>>>>>>>>>>>>>
45 0-6,8-23 * * * cd /data/cron/sellerboard-amazon-sales-data && timeout -k 1m 20m /root/.local/bin/uv run --no-sync python run.py --quiet >> logs/cron-output.log 2>&1
15 9 * * * find /data/cron/sellerboard-amazon-sales-data/data/processed -type f -name '*.csv' -mtime +30 -delete
#<<<<<<<<<<<<<<<<<<<< Sellerboard-Amazon-Sales-Data End >>>>>>>>>>>>>>>>>>>>>>
```

> **Important:** remove the old `profit_and_loss_upload.py` job from the crontab, so the two scripts do not both
> write to these tables.

### 5. Confirm

After the next :45, check:

```bash
tail logs/cron.log
```

You should see a new line ending in `exit=0`.

---

## Configuration

Settings are read from `.env` in the project folder. Copy [`.env.example`](.env.example) to start.

| Setting | Required | Description |
| --- | :---: | --- |
| `POWERBI_DB_HOST` | ✔ | Power BI MySQL server |
| `POWERBI_DB_PORT` | | Default `3306` |
| `POWERBI_DB_NAME` | ✔ | `powerbidb` |
| `POWERBI_DB_USER` | ✔ | Needs `SELECT`, `INSERT`, `DELETE` on the two tables |
| `POWERBI_DB_PASSWORD` | ✔ | |
| `POWERBI_DB_SSL_CA` | | Path to a CA certificate, to require TLS |
| `SELLERBOARD_ACCOUNT` | | `premiumcarproduct@gmail.com` (the default, from `config/selectors.json`); the job switches to this account if another is selected |
| `SELLERBOARD_LOGIN_EMAIL` | | Used to sign in again automatically if the session expires (one attempt, then waits 24 hours) |
| `SELLERBOARD_LOGIN_PASSWORD` | | As above |
| `SELLERBOARD_SESSION_FILE` | | Saved session. Default `data/browser/session.json`; relative paths start from the project folder |
| `SELLERBOARD_TIMEZONE` | | The browser's timezone. Default `UTC`; keep it. In a timezone west of UTC (for example a server on US time) Sellerboard names the export a day early, and the file is rejected |
| `SALES_MARKETS` | | Default `US,CA` |
| `US_MARKETPLACE`, `US_POWERBI_TABLE` | | Default `Amazon.com`, `combined_transposed_file_US` |
| `CA_MARKETPLACE`, `CA_POWERBI_TABLE` | | Default `Amazon.ca`, `combined_transposed_file_CA` |
| `LOG_LEVEL` | | `INFO` (default) or `DEBUG` |

To add another marketplace (for example Mexico), add `MX` to `SALES_MARKETS` and set `MX_MARKETPLACE` and
`MX_POWERBI_TABLE`.

---

## Commands

All commands run from the project folder.

| Command | What it does |
| --- | --- |
| `uv run python run.py` | **Full run** (what cron runs): download US and CA, update Power BI |
| `uv run python run.py --market CA` | Only one marketplace (`US` or `CA`) |
| `uv run python run.py --fetch-only` | Download the CSVs into `data/incoming/` and stop |
| `uv run python run.py --no-fetch` | Upload what is already in `data/incoming/`, without downloading |
| `uv run python run.py --dry-run` | Show what would be replaced. Reads Power BI, writes nothing, moves no files |
| `uv run python run.py --login` | Sign in to Sellerboard in a Chrome window and save the session |
| `uv run python run.py --inspect-page` | Save a screenshot and a list of the controls on the P&L page |
| `uv run python run.py --help` | All options |

Useful extra options: `--headed` (show the browser), `--log-level DEBUG` (more detail), `--quiet` (warnings only).

---

## Monitoring

| Log | What it shows |
| --- | --- |
| `logs/cron.log` | One line per run: time, exit code and a summary |
| `logs/importer.log` | Full details of every run (a new file each day, kept for 30 days) |
| `logs/cron-output.log` | Warnings and errors printed during cron runs |

A healthy run looks like this in `logs/cron.log`:

```
2026-10-28 12:24:21 exit=0  | US: combined_transposed_file_US -31/+31 | CA: combined_transposed_file_CA -31/+31
```

`-31/+31` means 31 days were deleted and replaced with 31 days from the export.

---

## Troubleshooting

When the exit code is not `0`, look it up here. If US and CA both fail, the exit code is the more serious of the two.

| Exit | Meaning | What to do |
| :---: | --- | --- |
| `0` | Success (including "no files waiting") | Nothing |
| `1` | Unexpected error | Read the error in `logs/importer.log` |
| `2` | Configuration error | Check `.env` against [Configuration](#configuration) |
| `3` | **Sellerboard sign-in needed** | On the PC run `uv run python run.py --login`, then copy `data/browser/session.json` to the server |
| `4` | Sellerboard page not as expected | See the screenshot in `data/screenshots/`. Try one more run, as Sellerboard is sometimes slow. If it keeps failing, run `--inspect-page` and update `config/selectors.json` |
| `6` | Download failed or timed out | Usually temporary; the next hourly run tries again |
| `7` | Export was malformed | The file is in `data/failed/`. Open it to see what Sellerboard changed |
| `9` | Database error | Nothing was changed (rolled back). The file stays in `data/incoming/` and is uploaded by the next run |
| `11` | Another run is still going | Normal if a run overlaps; it clears itself |

---

## Data rules

**Marketplace check.** The CSV does not say which marketplace it belongs to. Before downloading, the job checks that
the page shows exactly the right account, marketplace and period, then saves the file as `US_…` or `CA_…`.

**Rows matched by name.** Sub-rows that appear under both Sales and Units are stored as `Organic_Sales`,
`Organic_Units` and so on. Rows that Sellerboard has renamed are mapped back to the existing columns
(`RENAMED` in [`app/pnl.py`](app/pnl.py)):

| Sellerboard row | Power BI column | Renamed in |
| --- | --- | --- |
| `Refund cost` | `Refund сost` (with a Cyrillic "с") | Jan 2025 |
| `Indirect expenses` | `Expenses` | May 2025 |
| `TACOS` | `Real ACOS` | Jul 2026 |

Sellerboard rows that have no column in Power BI (for example `Sponsored Television` or `AWD storage fee`) are not
stored. Run with `--log-level DEBUG` to list them.

**Value types.** Each value is converted to its column's type as the table defines it (the US and CA tables differ):

| Column type | Stored as |
| --- | --- |
| Decimal | the number |
| Whole number | rounded |
| Text | as Sellerboard shows it, e.g. `"32.01%"` |
| Empty cell | `NULL`, or `0` in a whole-number column |

**Today's figures.** Sellerboard's newest day is still in progress when it is exported. The next run replaces it with
the updated figures. Sellerboard also revises recent days, which is why all 30 days are replaced on every run.

---

## Project structure

```
sellerboard-amazon-sales-data/
├── run.py                   Command-line entry point (the only command)
├── app/
│   ├── job.py               One run: download, then upload, per marketplace
│   ├── sellerboard.py       Browser automation: sign-in, account, filters, download
│   ├── pnl.py               Reads the P&L CSV into one record per day
│   ├── upload.py            Replaces the days in Power BI
│   ├── config.py            Reads .env
│   ├── db.py                Database connection
│   ├── lock.py              Stops two runs from overlapping
│   ├── exceptions.py        Errors and their exit codes
│   └── logging_config.py    Log files
├── config/selectors.json    The page elements the browser clicks in Sellerboard
├── data/
│   ├── browser/             Saved Sellerboard session (private)
│   ├── incoming/            Downloaded, waiting to upload
│   ├── processed/           Uploaded
│   ├── failed/              Could not be read
│   └── screenshots/         Taken when the Sellerboard page was not as expected
├── deploy/crontab.example   The cron lines for the server
├── logs/                    Run logs
├── tests/                   Automated tests
├── .env.example             Settings template
└── pyproject.toml           Dependencies
```

---

## Development

Run the tests:

```bash
uv run python -B -m pytest
```

If Sellerboard changes its P&L page, run `uv run python run.py --inspect-page`. It saves a screenshot and a list of
the page controls to `data/screenshots/`; update [`config/selectors.json`](config/selectors.json) to match.

---

## Security

- `.env` holds database and Sellerboard passwords. `data/browser/session.json` gives full access to the Sellerboard
  account. **Never commit or share either file.** Both are already excluded in `.gitignore`.
- On the server, keep both files readable only by the cron user (`chmod 600`).
- Downloaded reports contain business data and are also excluded from Git.
