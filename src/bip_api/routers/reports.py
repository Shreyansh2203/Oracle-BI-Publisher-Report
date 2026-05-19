from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from bip_api.client import fetch_report_csv, report_name, report_stem
from bip_api.config import Settings, get_settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.github import commit_report, get_latest_report_from_github
from bip_api.models import (
    DownloadRequest,
    FusedInvoiceItem,
    InvoiceItem,
    MatchedRecord,
    ReceiptRecord,
    ReportItem,
    ReportListResponse,
    ReportRequest,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])
_CACHE_TIER: dict[str, int] = {"github": 0, "oracle": 1}


@dataclass
class _FetchResult:
    filename: str
    csv_bytes: bytes
    commit_to_github: bool
    source: Literal["github", "oracle"] = field(default="oracle")


def _get_oracle_session(request: Request) -> requests.Session:
    return request.app.state.oracle_session  # type: ignore[no-any-return]


def _get_github_session(request: Request) -> requests.Session:
    return request.app.state.github_session  # type: ignore[no-any-return]


def _filter_by_receipt_number(csv_bytes: bytes, receipt_number: str) -> bytes:
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return csv_bytes
    matching = [
        row
        for row in reader
        if row.get("RECEIPT_NUMBER", "").strip().lower() == receipt_number.strip().lower()
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(matching)
    return out.getvalue().encode("utf-8")


async def _check_github_cache(
    item: DownloadRequest,
    settings: Settings,
    github_session: requests.Session,
) -> _FetchResult | None:
    stem = report_stem(item.report_path)
    github_hit = await asyncio.to_thread(
        get_latest_report_from_github, stem, settings, github_session
    )
    if github_hit:
        return _FetchResult(*github_hit, commit_to_github=False, source="github")
    return None


async def _fetch_from_oracle(
    item: DownloadRequest,
    settings: Settings,
    oracle_session: requests.Session,
) -> _FetchResult | AuthError | ReportError:
    try:
        result = await asyncio.to_thread(fetch_report_csv, item, settings, oracle_session)
        return _FetchResult(*result, commit_to_github=True, source="oracle")
    except AuthError as exc:
        return exc
    except ReportError as exc:
        return exc


async def _fetch(
    item: DownloadRequest,
    settings: Settings,
    oracle_session: requests.Session,
    github_session: requests.Session,
) -> _FetchResult | AuthError | ReportError:
    result = await _check_github_cache(item, settings, github_session)
    if result:
        return result
    return await _fetch_from_oracle(item, settings, oracle_session)


def _schedule_github_commit(
    background_tasks: BackgroundTasks,
    outcome: _FetchResult,
    settings: Settings,
    github_session: requests.Session,
) -> None:
    if outcome.commit_to_github:
        background_tasks.add_task(
            commit_report, outcome.filename, outcome.csv_bytes, settings, github_session
        )


def _apply_receipt_filter(outcome: _FetchResult, receipt_number: str | None) -> _FetchResult:
    if not receipt_number:
        return outcome
    filtered = _filter_by_receipt_number(outcome.csv_bytes, receipt_number)
    log.info(
        "Filtered by RECEIPT_NUMBER=%r: %d bytes → %d bytes",
        receipt_number,
        len(outcome.csv_bytes),
        len(filtered),
    )
    return _FetchResult(
        filename=outcome.filename, csv_bytes=filtered, commit_to_github=False, source=outcome.source
    )


@router.get("", response_model=ReportListResponse)
async def list_reports(settings: Settings = Depends(get_settings)) -> ReportListResponse:
    items = [ReportItem(path=p, name=report_name(p)) for p in settings.load_report_paths()]
    return ReportListResponse(reports=items)


@router.post("/download")
async def download(
    req: ReportRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    oracle_session: requests.Session = Depends(_get_oracle_session),
    github_session: requests.Session = Depends(_get_github_session),
) -> Response:
    if not req.reports:
        raise HTTPException(status_code=400, detail="No reports specified")
    if len(req.reports) > settings.max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(req.reports)} exceeds limit of {settings.max_batch_size}",
        )
    outcomes = await asyncio.gather(
        *[_fetch(item, settings, oracle_session, github_session) for item in req.reports]
    )
    if len(req.reports) == 1:
        outcome = outcomes[0]
        if isinstance(outcome, AuthError):
            raise HTTPException(status_code=401, detail=str(outcome))
        if isinstance(outcome, ReportError):
            raise HTTPException(status_code=502, detail=str(outcome))
        cache_source = outcome.source
        _schedule_github_commit(background_tasks, outcome, settings, github_session)
        outcome = _apply_receipt_filter(outcome, req.reports[0].receipt_number)
        download_filename = f"{report_name(req.reports[0].report_path)}.csv"
        return Response(
            content=outcome.csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{download_filename}"',
                "Content-Length": str(len(outcome.csv_bytes)),
                "X-Cache": cache_source,
            },
        )
    buf = io.BytesIO()
    fetch_errors: list[str] = []
    success_count = 0
    zip_sources: list[Literal["github", "oracle"]] = []
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item, outcome in zip(req.reports, outcomes, strict=True):
            if isinstance(outcome, AuthError):
                raise HTTPException(status_code=401, detail=str(outcome))
            if isinstance(outcome, ReportError):
                fetch_errors.append(f"{item.report_path}: {outcome}")
            else:
                zip_sources.append(outcome.source)
                _schedule_github_commit(background_tasks, outcome, settings, github_session)
                outcome = _apply_receipt_filter(outcome, item.receipt_number)
                base_name = f"{report_name(item.report_path)}.csv"
                if base_name in used_names:
                    used_names[base_name] += 1
                    stem, ext = base_name.rsplit(".", 1)
                    download_filename = f"{stem}_{used_names[base_name]}.{ext}"
                else:
                    used_names[base_name] = 0
                    download_filename = base_name
                zf.writestr(download_filename, outcome.csv_bytes)
                success_count += 1
    if success_count == 0:
        raise HTTPException(status_code=502, detail="; ".join(fetch_errors))
    if fetch_errors:
        log.warning("%d/%d reports failed: %s", len(fetch_errors), len(req.reports), fetch_errors)
    zip_cache_source = max(zip_sources, key=lambda s: _CACHE_TIER[s])
    headers: dict[str, str] = {
        "Content-Disposition": 'attachment; filename="reports.zip"',
        "X-Succeeded-Count": str(success_count),
        "X-Failed-Count": str(len(fetch_errors)),
        "X-Cache": zip_cache_source,
    }
    if fetch_errors:
        headers["X-Failed-Reports"] = "; ".join(fetch_errors)[:4096]
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


_INV_NUMBER_COL = "TRANSACTION_NUMBER"
_INV_DATE_COL = "TRANSACTION_DATE"
_INV_AMOUNT_COL = "TOTAL_AMOUNTS"
_INV_DOC_NUMBER_COL = "DOCUMENT_NUMBER"
_AMOUNT_TOLERANCE = 0.005  # half-cent tolerance for floating-point currency comparisons


def _parse_csv_amount(val: str) -> float | None:
    try:
        return float(val.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _amounts_match(csv_val: str, expected: float | None) -> bool:
    if expected is None:
        return True
    parsed = _parse_csv_amount(csv_val)
    return parsed is not None and abs(parsed - expected) < _AMOUNT_TOLERANCE


_DATE_INPUT_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")


def _convert_json_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    for fmt in _DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return None


def _convert_csv_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y/%m/%d")
    except ValueError:
        return None


def _match_receipt(record: ReceiptRecord, rows: list[dict[str, str]]) -> dict[str, str] | None:
    if record.payment_reference:
        hits = [
            r
            for r in rows
            if r.get("RECEIPT_NUMBER", "").strip().lower()
            == record.payment_reference.strip().lower()
            and _amounts_match(r.get("RECEIPT_AMOUNT", ""), record.total_amount)
        ]
    else:
        expected_date = _convert_json_date(record.payment_date)
        customer = (record.customer_name or "").strip().lower()
        hits = [
            r
            for r in rows
            if r.get("BILL_CUSTOMER_NAME", "").strip().lower() == customer
            and (not expected_date or r.get("RECEIPT_DATE", "").strip() == expected_date)
            and _amounts_match(r.get("RECEIPT_AMOUNT", ""), record.total_amount)
        ] if customer else []
    return hits[0] if len(hits) == 1 else None


def _match_invoice_item(inv: InvoiceItem, invoice_rows: list[dict[str, str]]) -> FusedInvoiceItem:
    inv_num = inv.invoice_number.strip().lower()
    converted_date = _convert_json_date(inv.invoice_date)
    inv_date = (converted_date or "").strip().lower()
    cust_inv_num = (inv.customer_invoice_number or "").strip().lower()
    matched_row: dict[str, str] | None = None
    # Step 1a: exact match on invoice_number only
    hits = [r for r in invoice_rows if r.get(_INV_NUMBER_COL, "").strip().lower() == inv_num]
    if len(hits) == 1:
        matched_row = hits[0]
    elif inv_date:
        # Step 1b: exact match on invoice_number + invoice_date
        hits = [
            r
            for r in invoice_rows
            if r.get(_INV_NUMBER_COL, "").strip().lower() == inv_num
            and r.get(_INV_DATE_COL, "").strip().lower() == inv_date
        ]
        if len(hits) == 1:
            matched_row = hits[0]
    if matched_row is None and cust_inv_num and inv_date:
        hits = [
            r
            for r in invoice_rows
            if r.get(_INV_DOC_NUMBER_COL, "").strip().lower() == cust_inv_num
            and r.get(_INV_DATE_COL, "").strip().lower() == inv_date
        ]
        if len(hits) == 1:
            matched_row = hits[0]
            log.debug(
                "Invoice %r matched via customer_invoice_number %r",
                inv.invoice_number,
                cust_inv_num,
            )
    if matched_row is None and inv_num and inv_date:
        hits = [
            r
            for r in invoice_rows
            if inv_num in r.get(_INV_NUMBER_COL, "").strip().lower()
            and r.get(_INV_DATE_COL, "").strip().lower() == inv_date
        ]
        if len(hits) == 1:
            matched_row = hits[0]
            log.debug("Invoice %r matched via substring fallback", inv.invoice_number)
    if matched_row is None:
        log.debug("Invoice %r: no match found in %d rows", inv.invoice_number, len(invoice_rows))
        return FusedInvoiceItem(
            line_id=inv.line_id,
            invoice_number=inv.invoice_number,
            invoice_date=inv.invoice_date,
            invoice_amount=inv.invoice_amount,
            description=inv.description,
            customer_invoice_number=inv.customer_invoice_number,
            store_no=inv.store_no,
        )
    return FusedInvoiceItem(
        line_id=inv.line_id,
        invoice_number=inv.invoice_number,
        fusion_invoice_number=matched_row.get(_INV_NUMBER_COL, "").strip() or None,
        invoice_date=inv.invoice_date,
        fusion_invoice_date=_convert_csv_date(matched_row.get(_INV_DATE_COL, "").strip()) or None,
        invoice_amount=inv.invoice_amount,
        fusion_invoice_amount=_parse_csv_amount(matched_row.get(_INV_AMOUNT_COL, "")),
        description=inv.description,
        customer_invoice_number=inv.customer_invoice_number,
        store_no=inv.store_no,
    )


def _match_record(
    record: ReceiptRecord,
    receipt_bytes: bytes,
    invoice_bytes: bytes | None = None,
) -> MatchedRecord:
    receipt_rows = list(csv.DictReader(io.StringIO(receipt_bytes.decode("utf-8", errors="replace"))))
    invoice_rows = (
        list(csv.DictReader(io.StringIO(invoice_bytes.decode("utf-8", errors="replace"))))
        if invoice_bytes is not None
        else []
    )
    matched_row = _match_receipt(record, receipt_rows)
    fused_invoices = [_match_invoice_item(inv, invoice_rows) for inv in record.invoices]
    return MatchedRecord(
        customer_name=record.customer_name,
        fusion_customer_name=matched_row.get("BILL_CUSTOMER_NAME", "").strip() or None
        if matched_row
        else None,
        payment_reference=record.payment_reference,
        fusion_receipt_number=matched_row.get("RECEIPT_NUMBER", "").strip() or None
        if matched_row
        else None,
        payment_date=record.payment_date,
        fusion_receipt_date=_convert_csv_date(matched_row.get("RECEIPT_DATE", "").strip())
        if matched_row
        else None,
        header_id=record.header_id,
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
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    oracle_session: requests.Session = Depends(_get_oracle_session),
    github_session: requests.Session = Depends(_get_github_session),
) -> JSONResponse:
    all_paths = settings.load_report_paths()
    receipt_path = settings.receipt_report_path or next(
        (p for p in all_paths if "receipt" in p.lower()), None
    )
    if not receipt_path:
        raise HTTPException(
            status_code=500,
            detail="No receipt report path configured. Set RECEIPT_REPORT_PATH or add one to reports.txt.",  # noqa: E501
        )
    invoice_path = next(
        (p for p in all_paths if "invoice" in p.lower() and p != receipt_path), None
    )
    fetch_paths = [receipt_path] + ([invoice_path] if invoice_path else [])
    items = [DownloadRequest(report_path=p) for p in fetch_paths]
    outcomes = await asyncio.gather(
        *[_fetch(item, settings, oracle_session, github_session) for item in items]
    )
    receipt_outcome = outcomes[0]
    if isinstance(receipt_outcome, AuthError):
        raise HTTPException(status_code=401, detail=str(receipt_outcome))
    if isinstance(receipt_outcome, ReportError):
        raise HTTPException(status_code=502, detail=str(receipt_outcome))
    invoice_outcome: _FetchResult | None = None
    if invoice_path:
        inv_result = outcomes[1]
        if isinstance(inv_result, (AuthError, ReportError)):
            log.warning("Failed to fetch invoice report %s: %s", invoice_path, inv_result)
        else:
            invoice_outcome = inv_result
    for outcome in outcomes:
        if not isinstance(outcome, (AuthError, ReportError)):
            _schedule_github_commit(background_tasks, outcome, settings, github_session)
    matched = _match_record(
        record,
        receipt_outcome.csv_bytes,
        invoice_outcome.csv_bytes if invoice_outcome else None,
    )
    return JSONResponse(content=matched.model_dump(by_alias=True, mode="json"))
