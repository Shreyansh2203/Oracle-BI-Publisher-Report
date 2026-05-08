from __future__ import annotations

import asyncio
import logging

import requests

from bip_api.client import fetch_report_csv, report_stem
from bip_api.config import Settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.github import commit_report, get_latest_report_from_github
from bip_api.models import DownloadRequest

log = logging.getLogger(__name__)


async def refresh_all_reports(settings: Settings, session: requests.Session) -> None:
    """
    Apply the file-age check for every report in reports.txt.

    For each report:
      - GitHub file exists and age < file_age_threshold_hours → skip
      - File missing or stale → fetch from Oracle and commit to GitHub
    """
    paths = settings.load_report_paths()
    if not paths:
        log.info("Scheduler: no reports configured in %s", settings.reports_file)
        return

    log.info("Scheduler: checking %d report(s)", len(paths))
    for report_path in paths:
        stem = report_stem(report_path)
        existing = await asyncio.to_thread(get_latest_report_from_github, stem, settings, session)
        if existing:
            log.info("Scheduler: %s is fresh — skipping", stem)
            continue

        log.info("Scheduler: %s is missing or stale — refreshing", stem)
        item = DownloadRequest(report_path=report_path)
        try:
            filename, csv_bytes = await asyncio.to_thread(fetch_report_csv, item, settings, session)
            await asyncio.to_thread(commit_report, filename, csv_bytes, settings, session)
            log.info("Scheduler: committed %s", filename)
        except AuthError as exc:
            log.error("Scheduler: auth error for %s — %s", report_path, exc)
        except ReportError as exc:
            log.error("Scheduler: report error for %s — %s", report_path, exc)


async def run_scheduler(settings: Settings, session: requests.Session) -> None:
    """
    Background loop: run refresh_all_reports immediately on startup,
    then repeat every schedule_interval_hours.
    """
    interval = settings.schedule_interval_hours * 3600
    while True:
        try:
            await refresh_all_reports(settings, session)
        except Exception:
            log.exception("Scheduler: unexpected error during refresh")
        await asyncio.sleep(interval)
