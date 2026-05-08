# BIP Downloader — FastAPI Service

## Overview
FastAPI service that downloads Oracle BI Publisher reports via SOAP, with in-memory caching, parallel batch downloads, and optional auto-commit to GitHub after each successful download.

---

## Project Structure

```
FastAPI-Service/
├── src/bip_api/
│   ├── __init__.py          # __version__ = "0.1.0"
│   ├── main.py              # App factory, lifespan, CORS, request-logging middleware
│   ├── config.py            # pydantic-settings Settings class + load_report_paths()
│   ├── client.py            # SOAP: _build_envelope, fetch_report_csv, make_session, report_name, report_stem
│   ├── cache.py             # ReportCache — TTL-based in-memory cache
│   ├── exceptions.py        # AuthError, ReportError
│   ├── github.py            # commit_report, get_latest_report_from_github — GitHub Contents API
│   ├── scheduler.py         # Background loop: refresh_all_reports, run_scheduler
│   ├── models.py            # DownloadRequest, BatchDownloadRequest, ReportItem, ReportListResponse, HealthResponse
│   └── routers/
│       ├── __init__.py
│       └── reports.py       # /reports, /reports/download, /reports/download-batch
├── tests/
│   ├── __init__.py
│   └── test_api.py
├── reports.txt              # One report path per line; lines starting with # are comments
├── pyproject.toml           # hatchling build, project deps, ruff/mypy/pytest config
├── render.yaml              # Render.com one-click deployment config
├── .env                     # Real credentials — gitignored
└── .env.example             # Placeholder template
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ORACLE_USERNAME` | yes | — | Oracle BIP username |
| `ORACLE_PASSWORD` | yes | — | Oracle BIP password |
| `ORACLE_BASE_URL` | yes | — | e.g. `https://fa-epxp-test-saasfaprod1.fa.ocs.oraclecloud.com` |
| `REPORTS_FILE` | no | `reports.txt` | Path to file listing report XDO paths |
| `MAX_BATCH_SIZE` | no | `20` | Max reports per batch request |
| `REQUEST_TIMEOUT` | no | `120` | Seconds per Oracle BIP SOAP call |
| `HTTP_POOL_SIZE` | no | `10` | `requests.Session` connection pool size |
| `CACHE_TTL` | no | `300` | Seconds to cache results; `0` = disabled |
| `DEBUG` | no | `false` | Enables `/docs`, `/redoc`, uvicorn reload |
| `GITHUB_TOKEN` | no | `""` | PAT with `repo` scope; leave empty to disable auto-commit |
| `GITHUB_REPO` | no | `""` | `owner/repo` format |
| `GITHUB_BRANCH` | no | `main` | Branch to commit to |
| `GITHUB_REPORTS_DIR` | no | `reports` | Directory inside the repo |
| `FILE_AGE_THRESHOLD_HOURS` | no | `4.0` | Skip Oracle if a GitHub file for this report is younger than this |
| `SCHEDULE_ENABLED` | no | `false` | Enable background auto-refresh scheduler |
| `SCHEDULE_INTERVAL_HOURS` | no | `1.0` | How often the scheduler checks for stale reports |

---

## API Endpoints

### `GET /health`
Returns `{"status": "ok", "version": "0.1.0"}`.

### `GET /reports`
Lists all reports configured in `reports.txt`.

### `POST /reports/download`
Downloads a single report. Returns CSV file as attachment.

**Request body:**
```json
{
  "report_path": "/Custom/Finance/AR_Aging_Report.xdo",
  "customer_name": "Acme Corp",
  "from_date": "01-01-2024",
  "to_date": "31-03-2024"
}
```
- `customer_name`, `from_date`, `to_date` are optional
- Dates must be `DD-MM-YYYY`
- **Fetch priority**: in-memory cache → GitHub file-age check → Oracle SOAP
- GitHub commit only happens when the file is fetched fresh from Oracle (not when served from cache or GitHub)

### `POST /reports/download-batch`
Downloads multiple reports in parallel via `asyncio.gather`. Returns a ZIP archive.

**Request body:**
```json
{
  "reports": [
    {"report_path": "/Custom/Finance/AR_Aging_Report.xdo"},
    {"report_path": "/Custom/Finance/AP_Report.xdo", "from_date": "01-01-2024", "to_date": "31-03-2024"}
  ]
}
```
- Partial failures: if some reports fail with `ReportError`, they are skipped and logged; the ZIP still returns with successful reports
- If all fail, returns HTTP 502
- If any fail with `AuthError`, returns HTTP 401 immediately
- Response headers: `X-Succeeded-Count`, `X-Failed-Count`, `X-Failed-Reports`

---

## Key Implementation Details

### SOAP Client (`client.py`)
- Endpoint: `POST /xmlpserver/services/PublicReportService`
- Protocol: SOAP 1.1 (`text/xml; charset=utf-8`, `SOAPAction: "runReport"`)
- Credentials go as `<pub:userID>` and `<pub:password>` **before** `<pub:reportRequest>` in the body
- All user input XML-escaped with `xml.sax.saxutils.escape` to prevent SOAP injection
- Response parsed with compiled regexes (`_RE_FAULT`, `_RE_REPORT_BYTES`)
- Filenames are timestamped: `{report_stem}_{YYYYMMDD_HHMMSS}.csv`
- Session has auto-retry on 502/503/504 (3 retries, backoff factor 1)

### Cache (`cache.py`)
- Keyed by `(report_path, customer_name, from_date, to_date)`
- TTL checked on read using `time.monotonic()`
- Set `CACHE_TTL=0` to disable; first request ~15s, cached repeat ~0.2s
- Settings exposes `load_report_paths()` — used by both the scheduler and `GET /reports` to read `reports.txt`

### Fetch priority in `_fetch` (`routers/reports.py`)
```
1. In-memory cache (ReportCache)  → fastest, no network            (filter-aware key)
2. GitHub file-age check           → list GitHub dir, parse timestamp from filename,
                                     download if age < FILE_AGE_THRESHOLD_HOURS
                                     (only when DownloadRequest.has_filters is False)
3. Oracle SOAP fetch               → calls BIP; commits result to GitHub as background task
                                     (only when DownloadRequest.has_filters is False)
```
- `_FetchResult.commit_to_github` flag ensures GitHub commits only happen for fresh
  Oracle fetches **without filters**.
- Filtered requests (`customer_name`, `from_date`, or `to_date` set) bypass GitHub on
  both read and write — GitHub keys files on report stem alone, so a filtered file
  served to an unfiltered caller would return wrong data. They still benefit from
  the in-memory cache (whose key includes all filters).

### File-age decision flow

`FILE_AGE_THRESHOLD_HOURS` (default `4`) drives this branching, applied identically by
`_fetch` (on-demand) and `refresh_all_reports` (scheduler):

```
                   ┌────────────────────────┐
                   │ Output files exist on  │
                   │ GitHub for this stem?  │
                   └──────────┬─────────────┘
                  No          │           Yes
            ┌─────────────────┘           └────────────────┐
            ▼                                              ▼
   ┌───────────────┐                       ┌────────────────────────────┐
   │   Run report  │                       │ last_modified (from        │
   │ (Oracle SOAP) │                       │ filename timestamp) older  │
   └───────┬───────┘                       │ than FILE_AGE_THRESHOLD_   │
           │                               │ HOURS?                     │
           ▼                               └────────────┬───────────────┘
   ┌───────────────┐                          Yes       │       No
   │   Generate    │                  ┌─────────────────┘       └────────────────┐
   │ output files  │                  ▼                                          ▼
   │ (commit to    │          ┌───────────────┐                        ┌──────────────────┐
   │ GitHub)       │          │   Run report  │                        │ Use existing     │
   └───────────────┘          │ (Oracle SOAP) │                        │ files            │
                              └───────┬───────┘                        │ (no Oracle call, │
                                      ▼                                │  no commit)      │
                              ┌───────────────┐                        └──────────────────┘
                              │  Regenerate   │
                              │ output files  │
                              │ (commit new   │
                              │ timestamped   │
                              │ file)         │
                              └───────────────┘
```

### GitHub (`github.py`)
- `commit_report(filename, csv_bytes, settings, session)` — no-op if token/repo not set
- GETs the file first to retrieve its SHA (required for updates to existing files)
- PUTs base64-encoded content with commit message `"report: add {filename}"`
- `get_latest_report_from_github(stem, settings, session)` — lists `github_reports_dir`,
  finds `{stem}_YYYYMMDD_HHMMSS.csv` files, parses timestamp from filename,
  returns `(filename, bytes)` if fresh, `None` if stale/missing

### Scheduler (`scheduler.py`)
- `run_scheduler(settings, session)` — async loop started in lifespan when `SCHEDULE_ENABLED=true`
- Runs `refresh_all_reports` immediately on startup, then every `SCHEDULE_INTERVAL_HOURS`
- `refresh_all_reports` iterates every path in `reports.txt`, applies the same file-age logic:
  - GitHub file fresh → skip
  - Missing or stale → fetch from Oracle + commit to GitHub

### App startup (`main.py`)
- Shared `requests.Session` created once in lifespan, closed on shutdown
- `ReportCache` initialized from `CACHE_TTL`; set to `None` if disabled
- Request-logging middleware adds `X-Request-Id` header to every response
- `/docs` and `/redoc` only available when `DEBUG=true`
- CORS: currently `allow_origins=["*"]` — restrict when API auth is added

---

## Running Locally

```bash
cd FastAPI-Service
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
# Copy and fill in .env:
copy .env.example .env
# Set DEBUG=true for /docs
uvicorn bip_api.main:app --reload
```

Open `http://localhost:8000/docs`.

---

## Deployment (Render)

`render.yaml` is configured for Render free tier:
- Build: `pip install -e .`
- Start: `uvicorn bip_api.main:app --host 0.0.0.0 --port $PORT`
- Set all environment variables in the Render dashboard (do not commit `.env`)

---

## Pending / Not Yet Implemented
- API key authentication (explicitly deferred by user — "will add it later")
- CORS origins should be restricted once auth is added
