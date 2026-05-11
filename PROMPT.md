# BIP Downloader — FastAPI Service

## Overview
FastAPI service that downloads Oracle BI Publisher reports via SOAP, with in-memory caching, parallel batch downloads, and optional auto-commit to GitHub after each successful download. On-demand only — every request runs a `FILE_AGE_THRESHOLD_HOURS` check against the latest GitHub copy and only re-fetches from Oracle when it's stale or missing.

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
│   ├── models.py            # DownloadRequest, ReportRequest, ReportItem, ReportListResponse, HealthResponse
│   └── routers/
│       ├── __init__.py
│       └── reports.py       # /reports, /reports/download (handles single or batch)
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
| `CORS_ORIGINS` | no | `*` | Comma-separated list of allowed origins, or `*` for any |

---

## API Endpoints

### `GET /health`
Returns `{"status": "ok", "version": "0.1.0"}`.

### `GET /reports`
Lists all reports configured in `reports.txt`.

### `POST /reports/match`
Match a list of JSON payment records against the Receipt Details Report. The report
is fetched via the same 3-tier cache as `/download` — if the cached copy is younger
than `FILE_AGE_THRESHOLD_HOURS`, Oracle is not re-queried. Requires `RECEIPT_REPORT_PATH`
to be set in the environment.

**Body** — a single record (same structure as the PDF extraction output):
```json
{
  "customer_name": "New Horizon Foods",
  "payment_reference": "RECEIPT003",
  "payment_date": "2026/05/10",
  "total_amount": 451.2,
  "invoices": [ { "invoice_number": "...", ... } ],
  "confidence_score": 0.83,
  "confidence_label": "HIGH",
  "invoice_count": 1,
  "_meta": { "filename": "...", ... }
}
```

**Response** — same structure enriched with `fusion_*` fields from the matched CSV row:
- `fusion_customer_name`, `fusion_receipt_number`, `fusion_receipt_date` (top-level, from CSV)
- Per invoice: `fusion_invoice_number`, `fusion_invoice_date`, `fusion_invoice_amount` (echoed from input — CSV has no invoice-level rows)
- All `fusion_*` fields are `null` when no match is found.
- `_meta` is passed through from input unchanged.

**Match priority:**
1. `payment_reference` present → match `RECEIPT_NUMBER` + `BILL_CUSTOMER_NAME`
2. `payment_reference` null → match `BILL_CUSTOMER_NAME` + `RECEIPT_DATE` + `RECEIPT_AMOUNT` (picks the row only when exactly one matches)

---

### `POST /reports/download`
Downloads one or more reports. The body is **always** a list — single or
multi — under a `reports` key. Response type is determined by the count:

| `len(reports)` | Response |
|---|---|
| 1 | Raw CSV file (`Content-Type: text/csv`) |
| 2+ | ZIP archive (`Content-Type: application/zip`) |
| 0 | HTTP 400 |

**Body:**
```json
{
  "reports": [
    {
      "report_path": "/Custom/Finance/AR_Aging_Report.xdo",
      "customer_name": "Acme Corp",
      "from_date": "01-01-2024",
      "to_date": "31-03-2024"
    }
  ]
}
```

- `customer_name`, `from_date`, `to_date` are optional. Dates must be `DD-MM-YYYY`.
- A bare `{"report_path": "..."}` (no `reports` wrapper) is rejected with HTTP 422.
- Multi-report fetches run in parallel via `asyncio.gather` and respect `MAX_BATCH_SIZE`.
- **Fetch priority**: in-memory cache → GitHub file-age check → Oracle SOAP. GitHub commit only happens for fresh-from-Oracle fetches (and never for filtered requests — see filter-bypass note below).
- **Errors (multi-report)**: any `AuthError` short-circuits to HTTP 401; `ReportError`s are collected, the ZIP returns with whatever succeeded, and listed in `X-Failed-Reports`. If everything fails → HTTP 502.
- **Errors (single-report)**: `AuthError` → 401, `ReportError` → 502.
- **Multi-report response headers**: `X-Succeeded-Count`, `X-Failed-Count`, `X-Failed-Reports`.

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

`FILE_AGE_THRESHOLD_HOURS` (default `4`) drives this branching, applied on every
`/reports/download` and `/reports/download-batch` call (no scheduler — purely
on-demand):

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


### App startup (`main.py`)
- Shared `requests.Session` created once in lifespan, closed on shutdown
- `ReportCache` initialized from `CACHE_TTL`; set to `None` if disabled
- Request-logging middleware adds `X-Request-Id` header to every response
- `/docs` and `/redoc` only available when `DEBUG=true`
- CORS allowed origins read from `CORS_ORIGINS` env var (`*` by default; comma-separate to whitelist specific origins)

### Error sanitization (`client.py`)

- HTTP-level Oracle errors and SOAP `<faultstring>` bodies are **logged in full** but **never echoed in the response**. Callers see `Oracle BIP returned HTTP <code>` or `Oracle BIP returned an error (see server logs)`. Auth errors keep a clear `Oracle BIP authentication failed` message.
- Reason: Oracle responses can include filesystem paths, principal names, and stack hints. Don't surface those to API consumers — operators read the server log instead.

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

`render.yaml` is a Blueprint manifest configured for Render's free tier.

**Key fields:**
- `rootDir: FastAPI-Service` — Render builds from this subdirectory of the repo (the project lives in a sub-folder, not at repo root).
- `healthCheckPath: /health` — Render polls `/health` for zero-downtime deploys and instance health.
- `plan: free` — sleeps after 15 min idle, ~30–60 s cold start; 750 free hours/month.
- `buildCommand: pip install .` (non-editable; editable installs aren't intended for production runtimes)
- `startCommand: uvicorn bip_api.main:app --host 0.0.0.0 --port $PORT`

**Setup steps:**
1. Push the repo to GitHub.
2. Render → **New** → **Blueprint** → connect repo. Render reads `render.yaml`.
3. In the dashboard, set the `sync: false` secrets by hand:
   - `ORACLE_USERNAME`, `ORACLE_PASSWORD`, `ORACLE_BASE_URL`
   - `GITHUB_TOKEN` (fine-grained PAT with `Contents: Read & write` on the target repo), `GITHUB_REPO` (`owner/repo`).
   - Leave `GITHUB_TOKEN`/`GITHUB_REPO` empty to disable auto-commit.
4. Verify: `curl https://<service>.onrender.com/health` → `{"status":"ok",...}`.

**Free-tier caveats:**

- The in-memory `ReportCache` resets on every cold start (sleep ⇒ wake). Not a correctness issue — first request after wake just falls through to the GitHub-or-Oracle path.
- The first request after a sleep adds ~30–60 s while the dyno wakes. Subsequent calls are normal speed.
- No always-on background work happens — fine because the scheduler was removed; the service is purely on-demand.

---

## CI

`.github/workflows/ci-fastapi.yml` runs on push and PR when anything under `FastAPI-Service/` changes:

1. `pip install -e ".[dev]"`
2. `ruff check src tests`
3. `mypy src`
4. `pytest --tb=short`

The root CLI project has its own separate workflow (`ci.yml`) — they don't share state.

---

## Pending / Not Yet Implemented

- **API key authentication** (deferred by user — "will add it later"). When added, also tighten `CORS_ORIGINS` from `*` to the actual caller origins.
