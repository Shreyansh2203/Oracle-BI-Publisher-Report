from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from dataclasses import dataclass

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from bip_api.cache import ReportCache
from bip_api.client import fetch_report_csv, report_name, report_stem
from bip_api.config import Settings, get_settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.github import commit_report, get_latest_report_from_github
from bip_api.models import (
    BatchDownloadRequest,
    DownloadRequest,
    ReportItem,
    ReportListResponse,
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


async def _fetch(
    item: DownloadRequest,
    settings: Settings,
    session: requests.Session,
    cache: ReportCache | None,
) -> _FetchResult | AuthError | ReportError:
    """
    Fetch one report; return _FetchResult or exception as a value (never raises).

    Priority order:
      1. In-memory cache       → commit_to_github=False  (cache key is filter-aware)
      2. GitHub file-age check → commit_to_github=False  (only when no filters — see below)
      3. Oracle SOAP fetch     → commit_to_github=True   (only when no filters)

    GitHub stores files keyed on report stem alone, with no encoding of
    customer_name / from_date / to_date. Filtered requests therefore bypass
    GitHub entirely on both read and write — otherwise a filtered fetch
    would poison the cache and serve wrong data to unfiltered callers.
    """
    # 1. In-memory cache (filter-aware)
    if cache:
        hit = cache.get(item)
        if hit:
            log.info("Cache hit: %s", item.report_path)
            return _FetchResult(*hit, commit_to_github=False)

    # 2. GitHub file-age check — only safe for unfiltered requests.
    if not item.has_filters:
        stem = report_stem(item.report_path)
        github_hit = await asyncio.to_thread(
            get_latest_report_from_github, stem, settings, session
        )
        if github_hit:
            if cache:
                cache.set(item, *github_hit)
            return _FetchResult(*github_hit, commit_to_github=False)

    # 3. Fetch from Oracle. Only commit unfiltered fetches to GitHub —
    # filtered output is per-request, not a shared resource.
    try:
        result = await asyncio.to_thread(fetch_report_csv, item, settings, session)
        if cache:
            cache.set(item, *result)
        return _FetchResult(*result, commit_to_github=not item.has_filters)
    except AuthError as exc:
        return exc
    except ReportError as exc:
        return exc


@router.get("", response_model=ReportListResponse)
async def list_reports(settings: Settings = Depends(get_settings)) -> ReportListResponse:
    """List all reports configured in reports.txt."""
    items = [ReportItem(path=p, name=report_name(p)) for p in settings.load_report_paths()]
    return ReportListResponse(reports=items)


@router.post("/download")
async def download_report(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    session: requests.Session = Depends(_get_session),
    cache: ReportCache | None = Depends(_get_cache),
) -> Response:
    """Download a single report. Saves to GitHub in the background if configured."""
    outcome = await _fetch(req, settings, session, cache)
    if isinstance(outcome, AuthError):
        raise HTTPException(status_code=401, detail=str(outcome))
    if isinstance(outcome, ReportError):
        raise HTTPException(status_code=502, detail=str(outcome))

    if outcome.commit_to_github:
        background_tasks.add_task(
            commit_report, outcome.filename, outcome.csv_bytes, settings, session
        )

    return Response(
        content=outcome.csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{outcome.filename}"',
            "Content-Length": str(len(outcome.csv_bytes)),
        },
    )


@router.post("/download-batch")
async def download_batch(
    req: BatchDownloadRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    session: requests.Session = Depends(_get_session),
    cache: ReportCache | None = Depends(_get_cache),
) -> StreamingResponse:
    """Download multiple reports in parallel and return them as a ZIP archive.
    Each successful report is saved to GitHub in the background if configured."""
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
                zf.writestr(outcome.filename, outcome.csv_bytes)
                if outcome.commit_to_github:
                    background_tasks.add_task(
                        commit_report, outcome.filename, outcome.csv_bytes, settings, session
                    )
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
