# BIP Downloader API — Full Documentation

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Tech Stack](#3-tech-stack)
4. [Project Structure](#4-project-structure)
5. [Configuration](#5-configuration)
6. [API Reference](#6-api-reference)
   - [GET /health](#get-health)
   - [GET /reports](#get-reports)
   - [POST /reports/download](#post-reportsdownload)
   - [POST /reports/match](#post-reportsmatch)
7. [Data Models](#7-data-models)
8. [Three-Tier Fetch Pipeline](#8-three-tier-fetch-pipeline)
9. [Receipt & Invoice Matching Logic](#9-receipt--invoice-matching-logic)
10. [GitHub Cache Layer](#10-github-cache-layer)
11. [Error Handling](#11-error-handling)
12. [Security](#12-security)
13. [Deployment](#13-deployment)
14. [Local Development](#14-local-development)
15. [Testing](#15-testing)
16. [Quick Start](#16-quick-start)
17. [Glossary](#17-glossary)

---

## 1. Overview

### Executive Summary

Oracle Fusion generates financial reports (receipts, invoices, transactions) that are slow to fetch on demand — each SOAP call to Oracle can take 30–120 seconds and must be authenticated. When an upstream system (such as an AI document parser) needs to cross-reference many documents against Oracle data in real time, calling Oracle directly for every request is not practical.

**BIP Downloader API** solves this by sitting between your application and Oracle. It fetches reports once, caches them intelligently (in memory and optionally in a shared GitHub repository), and serves subsequent requests instantly. It also exposes a dedicated matching endpoint that accepts a structured receipt payload and automatically cross-references it against Oracle receipt and invoice data — returning the original data enriched with Oracle-verified values, with no manual Oracle access required.

The service is production-ready: authenticated, containerised, retrying, observable, and tested.

---

**BIP Downloader API** is a FastAPI service that acts as a smart gateway to **Oracle BI Publisher (BIP)** — Oracle's enterprise reporting platform. It exposes three capabilities:

1. **Report Discovery** — lists all configured Oracle BIP report paths.
2. **Report Download** — fetches one or more Oracle BIP reports as CSV files (or a ZIP for batch requests), with a built-in three-tier caching system to avoid redundant calls to Oracle.
3. **Receipt & Invoice Matching** — accepts a structured receipt payload (typically extracted from a PDF by an upstream AI/OCR system), fetches the corresponding Oracle receipt and invoice reports, and returns a fused response enriched with Oracle-sourced values (`fusion_*` fields) alongside the original input data.

The service is built for production: connection pooling, retry logic, thread-safe caching, structured JSON logging, background GitHub commits, and comprehensive test coverage are all included.

---

## 2. Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full interactive diagram.

**Summary of layers:**

```
HTTP Client
    │
    ▼
CORS Middleware + Request Logger (X-Request-Id)
    │
    ▼
Endpoints: /health · /reports · /reports/download · /reports/match
    │
    ▼
Three-Tier Fetch Pipeline
  ① In-Memory Cache  (LRU + TTL, thread-safe)
  ② GitHub API Cache (persistent CSV files, age-gated)
  ③ Oracle BIP       (SOAP runReport, live fetch)
    │
    ▼ (POST /reports/match only)
Match Pipeline
  ParsedCSVCache → Receipt Matching → Invoice Matching (3-step fallback)
    │
    ▼ (async, non-blocking)
Background Task: commit CSV to GitHub → clean up stale files
```

---

## 3. Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11 |
| Web framework | FastAPI 0.115+ |
| ASGI server | Uvicorn with standard extras |
| Data validation | Pydantic v2 + pydantic-settings |
| HTTP client | requests 2.33+ with urllib3 retry adapter |
| Oracle integration | SOAP XML (PublicReportService) |
| GitHub integration | GitHub Contents API v3 |
| Containerisation | Docker (python:3.11-slim) |
| Dependency manager | uv |
| Linter | Ruff (E, F, I, N, UP, B, C4 rules) |
| Type checker | mypy (strict mode) |
| Test framework | pytest + pytest-asyncio + FastAPI TestClient |

---

## 4. Project Structure

```
Oracle-BI-Publisher-Report/
├── src/bip_api/
│   ├── __init__.py          # version string
│   ├── main.py              # FastAPI app factory, lifespan, logging, middleware
│   ├── config.py            # Pydantic settings (reads .env)
│   ├── models.py            # All request/response Pydantic models
│   ├── exceptions.py        # BIPError, AuthError, ReportError
│   ├── client.py            # Oracle BIP SOAP client
│   ├── github.py            # GitHub cache read/write/cleanup
│   ├── cache.py             # ReportCache (LRU+TTL) + ParsedCSVCache (LRU)
│   └── routers/
│       └── reports.py       # All /reports endpoints + fetch/match logic
├── tests/
│   └── test_api.py          # Full test suite (40+ tests)
├── reports.txt              # One Oracle BIP report path per line
├── report_processing_rules.md  # Matching rules reference document
├── ARCHITECTURE.md          # Mermaid architecture diagram
├── DOCUMENTATION.md         # This file
├── .env                     # Local secrets (not committed)
├── .env.example             # Template for .env
├── Dockerfile               # Production Docker image
├── docker-compose.yml       # Local Docker stack
├── render.yaml              # Render.com deployment config
├── pyproject.toml           # Project metadata, dependencies, ruff/mypy config
└── Makefile                 # Developer shortcuts
```

---

## 5. Configuration

All settings are loaded from environment variables (or a `.env` file). The application validates settings on startup and will refuse to start if required values are missing or invalid.

### Required

| Variable | Description |
|---|---|
| `ORACLE_USERNAME` | Oracle Fusion username (email) |
| `ORACLE_PASSWORD` | Oracle Fusion password |
| `ORACLE_BASE_URL` | Oracle base URL. **Must be HTTPS.** Example: `https://fa-epxp-test-saasfaprod1.fa.ocs.oraclecloud.com` |

### Optional — GitHub Cache

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | *(empty)* | GitHub personal access token. Leave empty to disable the GitHub cache layer entirely. |
| `GITHUB_REPO` | *(empty)* | Repository in `owner/repo` format where CSV files are stored. |
| `GITHUB_BRANCH` | `main` | Branch to commit reports to. |
| `GITHUB_REPORTS_DIR` | `reports` | Folder inside the repo where CSV files are written. |
| `FILE_AGE_THRESHOLD_HOURS` | `0.6` | A cached GitHub file older than this value (in hours) is considered stale and triggers a fresh Oracle fetch. |

### Optional — Tuning

| Variable | Default | Description |
|---|---|---|
| `CACHE_TTL` | `300` | In-memory cache TTL in seconds. Set to `0` to disable in-memory caching. |
| `CACHE_MAXSIZE` | `128` | Maximum number of entries in the in-memory LRU cache. |
| `MAX_BATCH_SIZE` | `20` | Maximum number of reports allowed in a single `/download` batch request. |
| `REQUEST_TIMEOUT` | `120` | Oracle SOAP request timeout in seconds. |
| `HTTP_POOL_SIZE` | `10` | Connection pool size for both Oracle and GitHub HTTP sessions. |
| `RECEIPT_REPORT_PATH` | *(empty)* | Explicit Oracle path for the receipt report used by `/match`. If empty, the first path containing `"receipt"` (case-insensitive) in `reports.txt` is used. |
| `CORS_ORIGINS` | *(empty)* | Comma-separated allowed origins, or `*` to allow all. Empty means no CORS. |
| `DEBUG` | `false` | Enables debug logging and Uvicorn auto-reload. |

### `reports.txt`

A plain-text file listing all Oracle BIP report paths the service exposes. One path per line. Lines starting with `#` are ignored.

```
# Financials
/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo
/Custom/Finacials/Receivables/Receipt Details Report.xdo
```

---

## 6. API Reference

Base URL: `http://localhost:8000` (local) or your deployed host.

Interactive docs: `/docs` (Swagger UI) · `/redoc` (ReDoc)

---

### GET /health

Health check. Returns immediately without touching Oracle or GitHub.

**Response `200`**
```json
{
  "status": "ok",
  "version": "0.1.0"
}
```

---

### GET /reports

Returns all report paths configured in `reports.txt`, with their display names.

**Response `200`**
```json
{
  "reports": [
    {
      "path": "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo",
      "name": "Invoice Details Report"
    },
    {
      "path": "/Custom/Finacials/Receivables/Receipt Details Report.xdo",
      "name": "Receipt Details Report"
    }
  ]
}
```

---

### POST /reports/download

Fetches one or more Oracle BIP reports as CSV. Returns a single `text/csv` for one report, or `application/zip` for a batch.

**Request body**

```json
{
  "reports": [
    {
      "report_path": "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo",
      "customer_name": "Acme Corp",
      "from_date": "01-01-2024",
      "to_date": "31-03-2024",
      "receipt_number": "REC001"
    }
  ]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `report_path` | string | Yes | Full Oracle BIP report path ending in `.xdo`. |
| `customer_name` | string | No | Passed as `P_CUSTOMER_NAME` to BIP. |
| `from_date` | string | No | Format: `DD-MM-YYYY`. Passed as `P_FROM_DATE`. |
| `to_date` | string | No | Format: `DD-MM-YYYY`. Passed as `P_TO_DATE`. |
| `receipt_number` | string | No | Post-fetch client-side filter. Rows where `RECEIPT_NUMBER` does not match are dropped from the returned CSV. Does not affect BIP parameters. |

> **Note on caching:** If any of `customer_name`, `from_date`, or `to_date` are provided (`has_filters = true`), the GitHub cache tier is bypassed and Oracle is called directly. The result is also **not** committed back to GitHub. This ensures filtered results are always fresh and never pollute the shared cache.

**Single report — Response `200`**

- `Content-Type: text/csv`
- `Content-Disposition: attachment; filename="Invoice Details Report.csv"`
- `Content-Length: <bytes>`
- `X-Cache: memory | github | oracle` — which cache tier served the response.

**Batch report — Response `200`**

- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename="reports.zip"`
- `X-Cache: memory | github | oracle` — the cache tier used for the most expensive individual fetch in the batch (`oracle` > `github` > `memory`).
- `X-Succeeded-Count: <n>` — number of reports successfully fetched.
- `X-Failed-Count: <n>` — number of reports that failed (partial success is allowed).
- `X-Failed-Reports: <path>: <error>; ...` — present only if some reports failed.

**Error responses**

| Status | Condition |
|---|---|
| `400` | `reports` array is empty, or batch size exceeds `MAX_BATCH_SIZE`. |
| `401` | Oracle authentication failed (bad username/password). |
| `422` | Request body fails validation (e.g. malformed date format). |
| `502` | Oracle SOAP error, network timeout, or all reports in a batch failed. |

---

### POST /reports/match

Accepts a structured receipt record (typically the output of an AI document parser), fetches the Oracle receipt and invoice reports, and returns the original data enriched with matched Oracle values.

**Request body (`ReceiptRecord`)**

```json
{
  "customer_name": "New Horizon Foods",
  "payment_reference": "RECEIPT005",
  "payment_date": "2026/05/10",
  "header_id": 9879879798,
  "total_amount": 2300.0,
  "confidence_score": 0.83,
  "confidence_label": "HIGH",
  "invoice_count": 1,
  "invoices": [
    {
      "Line_ID": 86979797,
      "invoice_number": "126129803472",
      "invoice_date": "2026/10/05",
      "invoice_amount": 3424.0,
      "customer_invoice_number": null,
      "storeNo": null
    }
  ],
  "_meta": {
    "filename": "144.98 W.pdf",
    "confidence_score": 0.83,
    "api_calls": 2
  }
}
```

| Field | Type | Description |
|---|---|---|
| `customer_name` | string | Required. Customer name as extracted from the document. |
| `payment_reference` | string \| null | Receipt/payment reference number. Used as the primary matching key against Oracle. |
| `payment_date` | string \| null | Format: `YYYY/MM/DD`. Used for date-based matching when `payment_reference` is absent. The API converts this to Oracle's internal format (`DD-MM-YYYY`) automatically. |
| `header_id` | integer \| null | Internal header ID. Passed through unchanged to the output. |
| `total_amount` | float \| null | Total receipt amount. Used as a matching guard (±0.005 tolerance). |
| `confidence_score` | float \| null | AI confidence score from the upstream parser. Passed through. |
| `confidence_label` | string \| null | e.g. `"HIGH"`, `"MEDIUM"`, `"LOW"`. Passed through. |
| `invoice_count` | integer \| null | Number of invoices as extracted. Passed through. |
| `invoices` | array | List of invoice line items (see below). |
| `_meta` | object \| null | Upstream parser metadata. Passed through unchanged. |

**Invoice item fields**

| Field | Type | Description |
|---|---|---|
| `Line_ID` | integer \| null | Internal line ID. Passed through unchanged. |
| `invoice_number` | string | Required. Invoice number as extracted. Primary matching key. |
| `invoice_date` | string \| null | Format: `YYYY/MM/DD`. Used in matching. The API converts this to Oracle's internal format automatically. |
| `invoice_amount` | float \| null | Extracted invoice amount. Passed through. |
| `customer_invoice_number` | string \| null | Customer's own invoice reference. Used as a secondary matching key. |
| `storeNo` | string \| null | Store number. Passed through unchanged. |

**Response `200` (`MatchedRecord`)**

```json
{
  "customer_name": "New Horizon Foods",
  "fusion_customer_name": "New Horizon Foods",
  "payment_reference": "RECEIPT005",
  "fusion_receipt_number": "RECEIPT005",
  "payment_date": "2026/05/10",
  "fusion_receipt_date": "2026/05/10",
  "header_id": 9879879798,
  "total_amount": 2300.0,
  "confidence_score": 0.83,
  "confidence_label": "HIGH",
  "invoice_count": 1,
  "invoices": [
    {
      "Line_ID": 86979797,
      "invoice_number": "126129803472",
      "fusion_invoice_number": "126129803472",
      "invoice_date": "2026/10/05",
      "fusion_invoice_date": "2026/10/05",
      "invoice_amount": 3424.0,
      "fusion_invoice_amount": 3424.0,
      "description": null,
      "customer_invoice_number": null,
      "storeNo": null
    }
  ],
  "_meta": {
    "filename": "144.98 W.pdf",
    "confidence_score": 0.83,
    "api_calls": 2
  }
}
```

`fusion_*` fields are populated when a match is found in Oracle. If no match is found (or if the match is ambiguous — 0 or 2+ rows), the `fusion_*` field is set to `null`. The original input fields are always preserved unchanged.

**Error responses**

| Status | Condition |
|---|---|
| `401` | Oracle authentication failed. |
| `500` | No receipt report path is configured (neither `RECEIPT_REPORT_PATH` env var nor a path containing `"receipt"` in `reports.txt`). |
| `502` | Oracle SOAP error or network failure on the receipt report fetch. Invoice report failure is non-fatal (logged as a warning; invoice `fusion_*` fields return `null`). |

---

## 7. Data Models

### `DownloadRequest`

Used inside `ReportRequest.reports[]` for the download endpoint.

```python
class DownloadRequest:
    report_path: str           # required, must be non-empty
    customer_name: str | None
    from_date: str | None      # validated: DD-MM-YYYY
    to_date: str | None        # validated: DD-MM-YYYY
    receipt_number: str | None # post-fetch filter only
```

### `ReceiptRecord`

Input to `POST /reports/match`.

```python
class ReceiptRecord:
    customer_name: str
    payment_reference: str | None
    payment_date: str | None
    header_id: int | None
    invoices: list[InvoiceItem]
    total_amount: float | None
    confidence_score: float | None
    confidence_label: str | None
    invoice_count: int | None
    meta: dict | None          # JSON key: "_meta"
```

### `InvoiceItem`

Individual invoice line inside a `ReceiptRecord`.

```python
class InvoiceItem:
    line_id: int | None        # JSON key: "Line_ID"
    invoice_number: str
    invoice_date: str | None   # format: YYYY/MM/DD (converted internally for Oracle matching)
    invoice_amount: float | None
    description: str | None    # passed through unchanged; not sourced from Oracle
    customer_invoice_number: str | None
    store_no: str | None       # JSON key: "storeNo"; passed through unchanged
```

### `MatchedRecord` / `FusedInvoiceItem`

Output of `POST /reports/match`. Mirrors `ReceiptRecord` and `InvoiceItem` but adds `fusion_*` fields for each matched value from Oracle.

All aliased fields (`Line_ID`, `storeNo`, `_meta`) are preserved in the JSON output via `by_alias=True`.

---

## 8. Three-Tier Fetch Pipeline

Every report fetch — whether from `/download` or `/match` — passes through the same three-tier pipeline in order. Each tier is only reached on a miss from the previous one.

### Tier 1 — In-Memory Cache (`ReportCache`)

- **Type:** `OrderedDict`-based LRU with per-entry TTL.
- **Key:** `(report_path, customer_name, from_date, to_date)`. `receipt_number` is excluded because it is a post-fetch filter, not a BIP parameter.
- **TTL:** `CACHE_TTL` seconds (default 5 minutes).
- **Max entries:** `CACHE_MAXSIZE` (default 128).
- **Thread safety:** Protected by `threading.Lock`.
- **Eviction:** LRU eviction when `maxsize` is exceeded; expired entries are evicted on read.
- **Behaviour:** A hit warms nothing upstream — it returns immediately. A miss falls through to Tier 2.

### Tier 2 — GitHub Cache (`get_latest_report_from_github`)

- **Skipped entirely** when `has_filters=True` (i.e. `customer_name`, `from_date`, or `to_date` is set), because filtered results are request-specific and must not be cached globally.
- **Skipped** if `GITHUB_TOKEN` or `GITHUB_REPO` are not configured.
- **Read flow:**
  1. List the `GITHUB_REPORTS_DIR` directory via GitHub Contents API.
  2. Find files matching the pattern `{stem}_YYYYMMDD_HHMMSS.csv` (where `stem` is the sanitised report name).
  3. Pick the latest file by filename (lexicographic sort on the timestamp).
  4. Check the file's age. If age ≥ `FILE_AGE_THRESHOLD_HOURS`, treat as stale and fall through to Tier 3.
  5. Download the raw CSV bytes via `download_url`.
  6. On hit: warm the in-memory cache and return. No Oracle call made.

### Tier 3 — Oracle BIP SOAP (`fetch_report_csv`)

- Constructs a SOAP envelope for the `runReport` operation.
- BIP parameters sent: `P_CUSTOMER_NAME`, `P_FROM_DATE`, `P_TO_DATE` (only when non-null).
- All values are XML-escaped before embedding in the envelope.
- `attributeFormat` is always `csv`.
- Requests use `Content-Type: text/xml` and `SOAPAction: "runReport"`.
- Timeout: `REQUEST_TIMEOUT` seconds (default 120s).
- Retries: 3 attempts on HTTP 502/503/504 with exponential backoff (via `urllib3.Retry`).
- Response parsing:
  - HTTP 401 → `AuthError`
  - SOAP `<faultstring>` containing auth keywords → `AuthError`
  - Any other `<faultstring>` → `ReportError`
  - Missing `<reportBytes>` → `ReportError`
  - Success: base64-decode `reportBytes` → raw CSV bytes.
- On success: filename is generated as `{stem}_{YYYYMMDD_HHMMSS}.csv`, result is stored in Tier 1.
- **Background commit:** If `has_filters=False` and GitHub is configured, a background task commits the CSV to GitHub after the response is sent (non-blocking).

---

## 9. Receipt & Invoice Matching Logic

The full matching rules are documented in [report_processing_rules.md](report_processing_rules.md). The implementation in `POST /reports/match` follows these rules exactly.

### How the endpoint works

1. Reads `RECEIPT_REPORT_PATH` (or auto-detects from `reports.txt`).
2. Auto-detects the invoice report path (first path containing `"invoice"` in `reports.txt`, excluding the receipt path).
3. Fetches both reports **in parallel** via `asyncio.gather` using the same three-tier pipeline.
4. Invoice report failure is non-fatal — a warning is logged and invoice `fusion_*` fields return `null`.
5. Parsed CSV rows are cached in `ParsedCSVCache` (LRU, maxsize 8, keyed by timestamped filename).

### Receipt Matching

**If `payment_reference` is present:**

Find rows in the Receipt report where:
- `RECEIPT_NUMBER` == `payment_reference` (case-insensitive)
- `RECEIPT_AMOUNT` ≈ `total_amount` (tolerance: ±0.005)

**If `payment_reference` is absent:**

Find rows where:
- `BILL_CUSTOMER_NAME` == `customer_name` (case-insensitive)
- `RECEIPT_DATE` == `payment_date` (date converted from `YYYY/MM/DD` to `DD-MM-YYYY`)
- `RECEIPT_AMOUNT` ≈ `total_amount` (tolerance: ±0.005)

**Rule:** Exactly 1 match → populate `fusion_receipt_number`, `fusion_customer_name`, `fusion_receipt_date`. 0 or 2+ matches → all three fields are `null`.

> `fusion_receipt_date` is returned in `YYYY/MM/DD` format (converted from Oracle's internal `DD-MM-YYYY`).

### Invoice Matching (per invoice line, 3-step fallback)

Matching stops at the first step that yields exactly 1 result.

**Step 1 — Exact match**

Find rows where:
- `TRANSACTION_NUMBER` == `invoice_number` (case-insensitive)
- `TRANSACTION_DATE` == `invoice_date` (case-insensitive)

**Step 2 — Customer invoice number**

Find rows where:
- `DOCUMENT_NUMBER` == `customer_invoice_number` (case-insensitive)
- `TRANSACTION_DATE` == `invoice_date`

**Step 3 — Substring fallback**

Find rows where:
- `TRANSACTION_NUMBER` **contains** `invoice_number` as a substring (case-insensitive)
- `TRANSACTION_DATE` == `invoice_date`

*Example: input `25908454` matches report row `126125908454`.*

**Rule:** Exactly 1 match at any step → populate `fusion_invoice_number`, `fusion_invoice_date`, `fusion_invoice_amount`. 0 or 2+ matches → all three fields are `null`. Original input fields are always preserved.

> `fusion_invoice_date` is returned in `YYYY/MM/DD` format (converted from Oracle's internal `DD-MM-YYYY`). `description` and `storeNo` are always passed through from the input — they are not sourced from Oracle.

---

## 10. GitHub Cache Layer

When `GITHUB_TOKEN` and `GITHUB_REPO` are configured, fetched CSV files are committed to GitHub as a persistent cache that survives process restarts and can be shared across instances.

### File naming

Files are stored as:
```
{GITHUB_REPORTS_DIR}/{stem}_{YYYYMMDD_HHMMSS}.csv
```

Where `stem` is the report name with spaces replaced by underscores and non-alphanumeric/hyphen characters removed.

Example: `reports/Invoice_Details_Report_20240115_120000.csv`

### Write flow (`commit_report`)

1. PUT to GitHub Contents API with base64-encoded CSV content.
2. If a file already exists at that path (HTTP 422), fetch its SHA and retry with the SHA included.
3. Retries up to 3 times on transient server errors (500/502/503/504) with exponential backoff.
4. On success: runs `_cleanup_old_reports` to delete all other `stem_*.csv` files in the directory, keeping only the newly committed file.

### Disabling the GitHub layer

Leave `GITHUB_TOKEN` or `GITHUB_REPO` empty in `.env`. All GitHub calls are skipped and the service runs on in-memory cache + Oracle only.

---

## 11. Error Handling

| HTTP Status | Cause | Details |
|---|---|---|
| `400 Bad Request` | Empty `reports` array, or batch exceeds `MAX_BATCH_SIZE` | Returned from `/download` |
| `401 Unauthorized` | Oracle credentials invalid (HTTP 401 or SOAP auth fault) | Stops the entire request, even in batch mode |
| `422 Unprocessable Entity` | Request body fails Pydantic validation | e.g. wrong date format, missing required fields |
| `500 Internal Server Error` | No receipt report path configured | Returned from `/match` only |
| `502 Bad Gateway` | Oracle SOAP fault, network error, or all batch reports failed | Partial batch success (some succeed, some fail) returns `200` with `X-Failed-Count` header |

**Error messages from Oracle are sanitised** — internal Oracle file paths, stack traces, and credential hints are never forwarded to the client. The client receives a generic message; the full fault is logged server-side.

---

## 12. Security

| Control | Implementation |
|---|---|
| HTTPS enforced for Oracle | `oracle_base_url` validator rejects any non-HTTPS URL at startup |
| Credential isolation | Oracle username/password only travel in SOAP envelopes, never in response bodies or logs |
| XML injection prevention | All values embedded in SOAP envelopes are passed through `xml.sax.saxutils.escape` |
| Error sanitisation | Oracle fault messages and internal paths are never forwarded to API clients |
| Secrets in environment | All credentials loaded from `.env` / environment variables, never committed |
| CORS | Configurable via `CORS_ORIGINS`; defaults to no allowed origins |
| Request logging | Every request logged with method, path, status, duration, and a random 8-char `X-Request-Id` for traceability |

---

## 13. Deployment

### Docker (recommended)

```bash
# Build image
docker build -t bip-api .

# Run with .env file
docker run --rm -p 8000:8000 --env-file .env bip-api
```

### Docker Compose

```bash
docker compose up
```

Includes health check (`GET /health` every 30s, 3 retries, 5s timeout).

### Render.com

A `render.yaml` is included for one-click deployment to Render. Set `ORACLE_USERNAME`, `ORACLE_PASSWORD`, `ORACLE_BASE_URL`, `GITHUB_TOKEN`, and `GITHUB_REPO` in the Render dashboard environment variables (marked `sync: false` so they are never stored in the YAML).

Build command: `pip install uv && uv sync --frozen --no-dev`
Start command: `uv run uvicorn bip_api.main:app --host 0.0.0.0 --port $PORT`

### Scaling

The application is stateless apart from the in-memory cache. To scale horizontally, run multiple container replicas (the GitHub cache layer provides shared persistence across instances). The `Dockerfile` uses a single Uvicorn worker — scale via container replicas, not workers, to avoid shared-memory conflicts.

---

## 14. Local Development

### Setup

```bash
# Install dependencies (including dev tools)
make install
# or
uv pip install -e ".[dev]"
```

### Run development server

```bash
make dev
# or
uvicorn bip_api.main:app --reload --host 0.0.0.0 --port 8000
```

Swagger UI: `http://localhost:8000/docs`

### Makefile commands

| Command | Action |
|---|---|
| `make install` | Install all dependencies including dev tools |
| `make dev` | Start development server with auto-reload |
| `make lint` | Run Ruff linter |
| `make format` | Auto-format code with Ruff |
| `make typecheck` | Run mypy type checker (strict) |
| `make test` | Run full test suite with verbose output |
| `make all` | Run lint + typecheck + test |
| `make docker-run` | Build and run Docker image with `.env` |

### `.env` for local development

```env
ORACLE_BASE_URL=https://your-instance.fa.ocs.oraclecloud.com
ORACLE_USERNAME=your.email@company.com
ORACLE_PASSWORD=your_password

# Leave empty to disable GitHub cache locally
GITHUB_TOKEN=
GITHUB_REPO=
```

---

## 15. Testing

The test suite is in `tests/test_api.py` and covers 40+ test cases using `pytest` and FastAPI's `TestClient`. Oracle and GitHub HTTP calls are always mocked — no live Oracle or GitHub connection is needed to run tests.

### Run tests

```bash
make test
# or
pytest -v
```

### Coverage areas

| Area | Tests |
|---|---|
| Health endpoint | Startup and version response |
| Report listing | Empty file, missing file, comment-only file, valid paths |
| Single download | Success, AuthError (401), ReportError (502), schema validation (422) |
| Batch download | ZIP output, partial failure headers, all-fail 502, AuthError propagation |
| Caching | GitHub cache hit (skips Oracle), memory cache TTL expiry, LRU eviction, `X-Cache` header values |
| Filter bypass | `has_filters=True` skips GitHub and blocks commit |
| GitHub commit | Fresh Oracle fetch triggers background commit |
| Receipt matching | Match by `payment_reference`, match by customer+date+amount, wrong amount → null, ambiguous → null, case-insensitive |
| Invoice matching | Step 1 exact, Step 2 customer invoice number, Step 3 substring, ambiguous → null, no invoice report → null, case-insensitive at each step |
| Security | SOAP fault sanitisation, HTTP error body sanitisation, internal paths not leaked |
| Edge cases | Non-UTF-8 CSV, `receipt_number` case-insensitive filter, GitHub stem prefix collision guard |
| Configuration | No receipt path → 500, date format validation, `report_stem` unsafe character stripping |

---

## 16. Quick Start

This section walks through the two most common integration scenarios end-to-end.

### Scenario A — Download a report as CSV

**Goal:** Fetch the Invoice Details report for a specific customer and date range.

#### Step 1 — Discover available report paths

```bash
curl http://localhost:8000/reports
```

```json
{
  "reports": [
    {
      "path": "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo",
      "name": "Invoice Details Report"
    },
    {
      "path": "/Custom/Finacials/Receivables/Receipt Details Report.xdo",
      "name": "Receipt Details Report"
    }
  ]
}
```

#### Step 2 — Download the report

```bash
curl -X POST http://localhost:8000/reports/download \
  -H "Content-Type: application/json" \
  -d '{
    "reports": [{
      "report_path": "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo",
      "customer_name": "Acme Corp",
      "from_date": "01-01-2024",
      "to_date": "31-03-2024"
    }]
  }' \
  --output invoice_report.csv
```

The response will be a CSV file. Check the `X-Cache` header in the response to see which tier served it (`memory`, `github`, or `oracle`).

> Because `customer_name` is provided, `has_filters=True` — Oracle is called directly and the result is not written to GitHub cache.

---

### Scenario B — Match a receipt from an AI parser against Oracle

**Goal:** An AI/OCR system extracted a payment from a PDF. Cross-reference it against Oracle to get verified receipt and invoice numbers.

```bash
curl -X POST http://localhost:8000/reports/match \
  -H "Content-Type: application/json" \
  -d '{
    "customer_name": "New Horizon Foods",
    "payment_reference": "RECEIPT005",
    "payment_date": "2026/05/10",
    "total_amount": 2300.00,
    "invoices": [
      {
        "invoice_number": "126129803472",
        "invoice_date": "2026/10/05",
        "invoice_amount": 3424.00
      }
    ],
    "_meta": { "filename": "payment_may.pdf" }
  }'
```

The response returns the same payload with additional `fusion_*` fields populated from Oracle:

```json
{
  "customer_name": "New Horizon Foods",
  "fusion_customer_name": "New Horizon Foods",
  "fusion_receipt_number": "RECEIPT005",
  "fusion_receipt_date": "2026/05/10",
  "invoices": [
    {
      "invoice_number": "126129803472",
      "fusion_invoice_number": "126129803472",
      "fusion_invoice_date": "2026/10/05",
      "fusion_invoice_amount": 3424.00
    }
  ]
}
```

If no Oracle match is found, `fusion_*` fields are `null` — the original data is always preserved and returned regardless.

---

## 17. Glossary

| Term | Meaning |
|---|---|
| **BIP** | Oracle BI Publisher — Oracle's enterprise reporting platform. Reports are defined in BIP and fetched via a SOAP API. |
| **SOAP** | Simple Object Access Protocol — an XML-based messaging protocol used to communicate with Oracle. The API handles all SOAP construction and parsing internally. |
| **LRU** | Least Recently Used — a cache eviction strategy where the least-recently-accessed entry is removed first when the cache is full. |
| **TTL** | Time To Live — the maximum age of a cached entry. After the TTL expires the next request fetches fresh data from Oracle. |
| **ASGI** | Asynchronous Server Gateway Interface — the Python standard for async web servers. Uvicorn is the ASGI server; FastAPI is the ASGI framework. |
| **`has_filters`** | Internal flag set to `true` when a download request includes `customer_name`, `from_date`, or `to_date`. Filtered requests bypass the GitHub cache and results are not committed back. |
| **`fusion_*` fields** | Fields in the `/match` response that are populated from Oracle data. If Oracle matching fails or is ambiguous, these fields are `null`; the original input fields are always returned unchanged. |
| **GitHub cache** | A persistent cache layer where fetched Oracle CSVs are stored as time-stamped files in a GitHub repository. Survives process restarts and is shared across all service instances. Requires `GITHUB_TOKEN` and `GITHUB_REPO` to be configured. |
| **Three-tier pipeline** | The fetch order: in-memory cache → GitHub cache → Oracle live fetch. Each tier is only reached if the previous one misses. |
| **SHA-aware PUT** | GitHub's Contents API requires the current file SHA when updating an existing file. The service fetches the SHA automatically on a 422 conflict and retries. |
| **`receipt_number` filter** | A post-fetch client-side CSV filter applied after the report is fetched. It does not affect which data Oracle returns — it only filters the rows returned to the client. Because it does not change the report itself, it does not set `has_filters`. |
| **`report_stem`** | The sanitised report name used as a filename prefix in GitHub (e.g. `Invoice_Details_Report`). Non-alphanumeric characters except hyphens are stripped; spaces become underscores. |
