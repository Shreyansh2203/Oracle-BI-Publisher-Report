from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from dataclasses import dataclass

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from bip_api.cache import ReportCache
from bip_api.client import fetch_report_csv, report_name, report_stem
from bip_api.config import Settings, get_settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.github import commit_report, get_latest_report_from_github
from bip_api.models import (
    DownloadRequest,
    FusedInvoiceItem,
    MatchedRecord,
    ReceiptRecord,
    ReportItem,
    ReportListResponse,
    ReportRequest,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])


@dataclass
class _FetchResult:
    filename: str
    csv_bytes: bytes
    commit_to_github: bool  # True only when fetched fresh from Oracle


def _get_session(request: Request) -> requests.Session:
    return request.app.state.http_session  # type: ignore[no-any-return]


def _get_cache(request: Request) -> ReportCache | None:
    return request.app.state.report_cache  # type: ignore[no-any-return]


def _filter_by_receipt_number(csv_bytes: bytes, receipt_number: str) -> bytes:
    """Return only rows whose RECEIPT_NUMBER column matches the given value."""
    text = csv_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return csv_bytes
    matching = [row for row in reader if row.get("RECEIPT_NUMBER", "") == receipt_number]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(matching)
    return out.getvalue().encode("utf-8")


async def _check_memory_cache(
    item: DownloadRequest,
    cache: ReportCache | None,
) -> _FetchResult | None:
    if cache:
        hit = cache.get(item)
        if hit:
            log.info("Memory cache hit: %s", item.report_path)
            return _FetchResult(*hit, commit_to_github=False)
    return None


async def _check_github_cache(
    item: DownloadRequest,
    settings: Settings,
    session: requests.Session,
    cache: ReportCache | None,
) -> _FetchResult | None:
    stem = report_stem(item.report_path)
    github_hit = await asyncio.to_thread(
        get_latest_report_from_github, stem, settings, session
    )
    if github_hit:
        if cache:
            cache.set(item, *github_hit)
        return _FetchResult(*github_hit, commit_to_github=False)
    return None


async def _fetch_from_oracle(
    item: DownloadRequest,
    settings: Settings,
    session: requests.Session,
    cache: ReportCache | None,
) -> _FetchResult | AuthError | ReportError:
    try:
        result = await asyncio.to_thread(fetch_report_csv, item, settings, session)
        if cache:
            cache.set(item, *result)
        return _FetchResult(*result, commit_to_github=not item.has_filters)
    except AuthError as exc:
        return exc
    except ReportError as exc:
        return exc


async def _fetch(
    item: DownloadRequest,
    settings: Settings,
    session: requests.Session,
    cache: ReportCache | None,
) -> _FetchResult | AuthError | ReportError:
    """
    Fetch one report. Priority order:
      1. In-memory cache       (filter-aware cache key)
      2. GitHub file-age check (only for unfiltered Oracle requests)
      3. Oracle SOAP fetch

    receipt_number is a Python post-filter and does not affect which tier is used —
    the full report is always fetched/cached, then filtered after.

    Filtered Oracle requests (customer_name / from_date / to_date) bypass GitHub
    on both read and write to prevent poisoning the unfiltered cache.
    """
    result = await _check_memory_cache(item, cache)
    if result:
        return result

    if not item.has_filters:
        result = await _check_github_cache(item, settings, session, cache)
        if result:
            return result

    return await _fetch_from_oracle(item, settings, session, cache)


def _schedule_github_commit(
    background_tasks: BackgroundTasks,
    outcome: _FetchResult,
    settings: Settings,
    session: requests.Session,
) -> None:
    if outcome.commit_to_github:
        background_tasks.add_task(
            commit_report, outcome.filename, outcome.csv_bytes, settings, session
        )


def _apply_receipt_filter(
    outcome: _FetchResult,
    receipt_number: str | None,
) -> _FetchResult:
    """Filter CSV rows by RECEIPT_NUMBER after fetching. Never commits filtered results."""
    if not receipt_number:
        return outcome
    filtered = _filter_by_receipt_number(outcome.csv_bytes, receipt_number)
    log.info(
        "Filtered by RECEIPT_NUMBER=%r: %d bytes → %d bytes",
        receipt_number, len(outcome.csv_bytes), len(filtered),
    )
    return _FetchResult(filename=outcome.filename, csv_bytes=filtered, commit_to_github=False)


@router.get("", response_model=ReportListResponse)
async def list_reports(settings: Settings = Depends(get_settings)) -> ReportListResponse:
    """List all reports configured in reports.txt."""
    items = [ReportItem(path=p, name=report_name(p)) for p in settings.load_report_paths()]
    return ReportListResponse(reports=items)


@router.post("/download")
async def download(
    req: ReportRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    session: requests.Session = Depends(_get_session),
    cache: ReportCache | None = Depends(_get_cache),
) -> Response:
    """
    Download one or more reports.

    Returns a CSV for a single report, a ZIP for multiple.
    Fresh Oracle fetches are committed to GitHub in the background (if configured).

    Oracle filters: customer_name, from_date, to_date (passed to SOAP).
    Post-filter:    receipt_number (matched against RECEIPT_NUMBER column in CSV).
    """
    if not req.reports:
        raise HTTPException(status_code=400, detail="No reports specified")
    if len(req.reports) > settings.max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(req.reports)} exceeds limit of {settings.max_batch_size}",
        )

    outcomes = await asyncio.gather(
        *[_fetch(item, settings, session, cache) for item in req.reports]
    )

    # Single report → return raw CSV
    if len(req.reports) == 1:
        outcome = outcomes[0]
        if isinstance(outcome, AuthError):
            raise HTTPException(status_code=401, detail=str(outcome))
        if isinstance(outcome, ReportError):
            raise HTTPException(status_code=502, detail=str(outcome))

        _schedule_github_commit(background_tasks, outcome, settings, session)
        outcome = _apply_receipt_filter(outcome, req.reports[0].receipt_number)

        return Response(
            content=outcome.csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{outcome.filename}"',
                "Content-Length": str(len(outcome.csv_bytes)),
            },
        )

    # Multiple reports → bundle into a ZIP
    buf = io.BytesIO()
    fetch_errors: list[str] = []
    success_count = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item, outcome in zip(req.reports, outcomes, strict=True):
            if isinstance(outcome, AuthError):
                raise HTTPException(status_code=401, detail=str(outcome))
            if isinstance(outcome, ReportError):
                fetch_errors.append(f"{item.report_path}: {outcome}")
            else:
                _schedule_github_commit(background_tasks, outcome, settings, session)
                outcome = _apply_receipt_filter(outcome, item.receipt_number)
                zf.writestr(outcome.filename, outcome.csv_bytes)
                success_count += 1

    if success_count == 0:
        raise HTTPException(status_code=502, detail="; ".join(fetch_errors))

    if fetch_errors:
        log.warning("%d/%d reports failed: %s", len(fetch_errors), len(req.reports), fetch_errors)

    headers: dict[str, str] = {
        "Content-Disposition": 'attachment; filename="reports.zip"',
        "X-Succeeded-Count": str(success_count),
        "X-Failed-Count": str(len(fetch_errors)),
    }
    if fetch_errors:
        headers["X-Failed-Reports"] = "; ".join(fetch_errors)

    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


# ---------------------------------------------------------------------------
# Match helpers
# ---------------------------------------------------------------------------

def _parse_csv_amount(val: str) -> float | None:
    try:
        return float(val.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _convert_json_date(date_str: str | None) -> str | None:
    """YYYY/MM/DD → DD-MM-YYYY (for comparing against CSV RECEIPT_DATE)."""
    if not date_str:
        return None
    parts = date_str.split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None


def _convert_csv_date(date_str: str | None) -> str | None:
    """DD-MM-YYYY (CSV) → YYYY/MM/DD (response format matching input)."""
    if not date_str:
        return None
    parts = date_str.split("-")
    if len(parts) == 3:
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return None


def _build_fused_invoices(record: ReceiptRecord) -> list[FusedInvoiceItem]:
    """Echo invoice fields from the input — CSV has no invoice-level rows."""
    return [
        FusedInvoiceItem(
            invoice_number=inv.invoice_number,
            fusion_invoice_number=inv.invoice_number,
            invoice_date=inv.invoice_date,
            fusion_invoice_date=inv.invoice_date,
            invoice_amount=inv.invoice_amount,
            fusion_invoice_amount=inv.invoice_amount,
            description=inv.description,
            customer_invoice_number=inv.customer_invoice_number,
            storeNo=inv.storeNo,
        )
        for inv in record.invoices
    ]


def _match_record(record: ReceiptRecord, csv_bytes: bytes) -> MatchedRecord:
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    fused_invoices = _build_fused_invoices(record)

    customer_rows = [
        r for r in rows
        if r.get("BILL_CUSTOMER_NAME", "").strip().lower()
        == record.customer_name.strip().lower()
    ]

    matched_row: dict | None = None

    if record.payment_reference:
        # Priority 1: match by RECEIPT_NUMBER + BILL_CUSTOMER_NAME
        hits = [
            r for r in customer_rows
            if r.get("RECEIPT_NUMBER", "").strip() == record.payment_reference.strip()
        ]
        if hits:
            matched_row = hits[0]
    else:
        # Priority 2: match by BILL_CUSTOMER_NAME + RECEIPT_DATE + RECEIPT_AMOUNT.
        # Only accept when exactly one unique row is found.
        expected_date = _convert_json_date(record.payment_date)
        hits = [
            r for r in customer_rows
            if (
                record.total_amount is None
                or abs((_parse_csv_amount(r.get("RECEIPT_AMOUNT", "")) or -1) - record.total_amount) < 0.005
            )
            and (not expected_date or r.get("RECEIPT_DATE", "").strip() == expected_date)
        ]
        if len(hits) == 1:
            matched_row = hits[0]

    if matched_row is None:
        return MatchedRecord(
            customer_name=record.customer_name,
            payment_reference=record.payment_reference,
            payment_date=record.payment_date,
            invoices=fused_invoices,
            total_amount=record.total_amount,
            confidence_score=record.confidence_score,
            confidence_label=record.confidence_label,
            invoice_count=record.invoice_count,
            meta=record.meta,
        )

    return MatchedRecord(
        customer_name=record.customer_name,
        fusion_customer_name=matched_row.get("BILL_CUSTOMER_NAME", "").strip() or None,
        payment_reference=record.payment_reference,
        fusion_receipt_number=matched_row.get("RECEIPT_NUMBER", "").strip() or None,
        payment_date=record.payment_date,
        fusion_receipt_date=_convert_csv_date(matched_row.get("RECEIPT_DATE", "").strip()),
        invoices=fused_invoices,
        total_amount=record.total_amount,
        confidence_score=record.confidence_score,
        confidence_label=record.confidence_label,
        invoice_count=record.invoice_count,
        meta=record.meta,
    )


@router.post("/match")
async def match_record(
    record: ReceiptRecord,
    settings: Settings = Depends(get_settings),
    session: requests.Session = Depends(_get_session),
    cache: ReportCache | None = Depends(_get_cache),
) -> JSONResponse:
    """
    Match a single JSON payment record against the Receipt Details Report and
    return the enriched record with fusion_* fields populated from the CSV.

    Uses the same 3-tier fetch as /download (in-memory cache → GitHub file-age
    → Oracle SOAP). Re-fetches from Oracle only when the cached copy is older
    than FILE_AGE_THRESHOLD_HOURS. Report path is configured via RECEIPT_REPORT_PATH.

    Match priority:
      1. payment_reference present → match RECEIPT_NUMBER + BILL_CUSTOMER_NAME
      2. payment_reference null   → match BILL_CUSTOMER_NAME + RECEIPT_DATE + RECEIPT_AMOUNT
                                    (only when exactly one row matches)

    fusion_* fields are null when no match is found.
    """
    paths = settings.load_report_paths()
    receipt_path = next(
        (p for p in paths if "receipt" in p.lower()),
        paths[0] if paths else None,
    )
    if not receipt_path:
        raise HTTPException(status_code=500, detail="No report path configured in reports.txt")

    download_item = DownloadRequest(report_path=receipt_path)
    outcome = await _fetch(download_item, settings, session, cache)

    if isinstance(outcome, AuthError):
        raise HTTPException(status_code=401, detail=str(outcome))
    if isinstance(outcome, ReportError):
        raise HTTPException(status_code=502, detail=str(outcome))

    matched = _match_record(record, outcome.csv_bytes)
    return JSONResponse(content=matched.model_dump(by_alias=True, mode="json"))
