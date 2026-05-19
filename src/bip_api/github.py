from __future__ import annotations

import base64
import logging
import re
import time
from datetime import UTC, datetime

import requests

from bip_api.config import Settings

log = logging.getLogger(__name__)
_API_BASE = "https://api.github.com"
_TS_RE = re.compile("_(\\d{8}_\\d{6})\\.csv$")


def get_latest_report_from_github(
    stem: str, settings: Settings, session: requests.Session
) -> tuple[str, bytes] | None:
    if not settings.github_token or not settings.github_repo:
        return None
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{_API_BASE}/repos/{settings.github_repo}/contents/{settings.github_reports_dir}"
    resp = session.get(url, headers=headers, timeout=15)
    if resp.status_code == 404:
        return None
    if not resp.ok:
        log.warning("GitHub dir listing failed: %s", resp.status_code)
        return None
    file_re = re.compile(f"^{re.escape(stem)}_\\d{{8}}_\\d{{6}}\\.csv$")
    matches = [f for f in resp.json() if isinstance(f, dict) and file_re.match(f.get("name", ""))]
    if not matches:
        return None
    latest = max(matches, key=lambda f: f["name"])
    filename = latest["name"]
    ts_match = _TS_RE.search(filename)
    if not ts_match:
        return None
    try:
        file_dt = datetime.strptime(ts_match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    age_hours = (datetime.now(UTC) - file_dt).total_seconds() / 3600
    if age_hours >= settings.file_age_threshold_hours:
        log.info(
            "GitHub file %s is stale (%.1fh >= %.1fh threshold) — will refresh",
            filename,
            age_hours,
            settings.file_age_threshold_hours,
        )
        return None
    download_url = latest.get("download_url")
    if not download_url:
        return None
    dl = session.get(
        download_url, headers={"Authorization": f"Bearer {settings.github_token}"}, timeout=30
    )
    if not dl.ok:
        log.warning("Failed to download %s from GitHub: %s", filename, dl.status_code)
        return None
    log.info("GitHub cache hit: %s (age: %.1fh)", filename, age_hours)
    return (filename, dl.content)


def _cleanup_old_reports(
    stem: str,
    new_filename: str,
    settings: Settings,
    session: requests.Session,
    headers: dict[str, str],
) -> None:
    dir_url = f"{_API_BASE}/repos/{settings.github_repo}/contents/{settings.github_reports_dir}"
    resp = session.get(dir_url, headers=headers, timeout=15)
    if not resp.ok:
        log.warning("GitHub dir listing for cleanup failed: %s", resp.status_code)
        return
    file_re = re.compile(f"^{re.escape(stem)}_\\d{{8}}_\\d{{6}}\\.csv$")
    for f in resp.json():
        if not isinstance(f, dict):
            continue
        name = f.get("name", "")
        if name == new_filename or not file_re.match(name):
            continue
        sha = f.get("sha")
        if not sha:
            continue
        del_url = f"{_API_BASE}/repos/{settings.github_repo}/contents/{settings.github_reports_dir}/{name}"  # noqa: E501
        del_resp = session.delete(
            del_url,
            json={
                "message": f"report: remove stale {name}",
                "sha": sha,
                "branch": settings.github_branch,
            },
            headers=headers,
            timeout=15,
        )
        if del_resp.ok:
            log.info("Deleted stale report %s", name)
        else:
            log.warning("Failed to delete stale report %s: %s", name, del_resp.status_code)


def commit_report(
    filename: str, csv_bytes: bytes, settings: Settings, session: requests.Session
) -> None:
    if not settings.github_token or not settings.github_repo:
        return
    path = f"{settings.github_reports_dir}/{filename}"
    url = f"{_API_BASE}/repos/{settings.github_repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    content = base64.b64encode(csv_bytes).decode()
    sha: str | None = None
    for attempt in range(3):
        payload: dict[str, str] = {
            "message": f"report: add {filename}",
            "content": content,
            "branch": settings.github_branch,
        }
        if sha:
            payload["sha"] = sha
        resp = session.put(url, json=payload, headers=headers, timeout=30)
        if resp.ok:
            commit_url = resp.json().get("commit", {}).get("html_url", "")
            log.info("Committed %s → %s", path, commit_url)
            stem = _TS_RE.sub("", filename)
            _cleanup_old_reports(stem, filename, settings, session, headers)
            return
        if resp.status_code == 422:
            check = session.get(url, headers=headers, timeout=15)
            if check.status_code == 200:
                sha = check.json().get("sha")
            continue
        if resp.status_code in (500, 502, 503, 504) and attempt < 2:
            wait = 2**attempt
            log.warning(
                "GitHub commit transient error %d for %s — retrying in %ds",
                resp.status_code,
                path,
                wait,
            )
            time.sleep(wait)
            continue
        log.error("GitHub commit failed for %s: %s %s", path, resp.status_code, resp.text[:300])
        return
    log.error(
        "GitHub commit failed for %s: exhausted retries, could not retrieve file SHA", path
    )
