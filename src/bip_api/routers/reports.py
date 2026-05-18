from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from typing import Literal

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from bip_api.cache import ReportCache
from bip_api.client import fetch_report_csv, report_name, report_stem
from bip_api.config import Settings, get_settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.github import commit_report, get_latest_report_from_github
from bip_api.models import (
    DownloadRequest,
    ReportItem,
    ReportListResponse,
    ReportRequest,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])
_CACHE_TIER: dict[str, int] = {"memory": 0, "github": 1, "oracle": 2}


@dataclass
class _FetchResult:
    filename: str
    csv_bytes: bytes
    commit_to_github: bool
    source: Literal["memory", "github", "oracle"] = field(default="oracle")


def _get_oracle_session(request: Request) -> requests.Session:
    return request.app.state.oracle_session  # type: ignore[no-any-return]


def _get_github_session(request: Request) -> requests.Session:
    return request.app.state.github_session  # type: ignore[no-any-return]


def _get_cache(request: Request) -> ReportCache | None:
    return request.app.state.report_cache  # type: ignore[no-any-return]


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


def _check_memory_cache(
    item: DownloadRequest, cache: ReportCache | None
) -> _FetchResult | None:
    if cache:
        hit = cache.get(item)
        if hit:
            log.info("Memory cache hit: %s", item.report_path)
            return _FetchResult(*hit, commit_to_github=False, source="memory")
    return None


async def _check_github_cache(
    item: DownloadRequest,
    settings: Settings,
    github_session: requests.Session,
    cache: ReportCache | None,
) -> _FetchResult | None:
    stem = report_stem(item.report_path)
    github_hit = await asyncio.to_thread(
        get_latest_report_from_github, stem, settings, github_session
    )
    if github_hit:
        if cache:
            cache.set(item, *github_hit)
        return _FetchResult(*github_hit, commit_to_github=False, source="github")
    return None


async def _fetch_from_oracle(
    item: DownloadRequest,
    settings: Settings,
    oracle_session: requests.Session,
    cache: ReportCache | None,
) -> _FetchResult | AuthError | ReportError:
    try:
        result = await asyncio.to_thread(fetch_report_csv, item, settings, oracle_session)
        if cache:
            cache.set(item, *result)
        return _FetchResult(*result, commit_to_github=not item.has_filters, source="oracle")
    except AuthError as exc:
        return exc
    except ReportError as exc:
        return exc


async def _fetch(
    item: DownloadRequest,
    settings: Settings,
    oracle_session: requests.Session,
    github_session: requests.Session,
    cache: ReportCache | None,
) -> _FetchResult | AuthError | ReportError:
    result = _check_memory_cache(item, cache)
    if result:
        return result
    if not item.has_filters:
        result = await _check_github_cache(item, settings, github_session, cache)
        if result:
            return result
    return await _fetch_from_oracle(item, settings, oracle_session, cache)


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
    cache: ReportCache | None = Depends(_get_cache),
) -> Response:
    if not req.reports:
        raise HTTPException(status_code=400, detail="No reports specified")
    if len(req.reports) > settings.max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(req.reports)} exceeds limit of {settings.max_batch_size}",
        )
    outcomes = await asyncio.gather(
        *[_fetch(item, settings, oracle_session, github_session, cache) for item in req.reports]
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
    zip_sources: list[Literal["memory", "github", "oracle"]] = []
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
                download_filename = f"{report_name(item.report_path)}.csv"
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
        headers["X-Failed-Reports"] = "; ".join(fetch_errors)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers=headers)
