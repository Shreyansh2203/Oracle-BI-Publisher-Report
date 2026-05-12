from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime, timezone

import requests

from bip_api.config import Settings

log = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_TS_RE = re.compile(r"_(\d{8}_\d{6})\.csv$")


def get_latest_report_from_github(
    stem: str,
    settings: Settings,
    session: requests.Session,
) -> tuple[str, bytes] | None:
    """
    Check GitHub for an existing fresh CSV for this report stem.

    Returns (filename, csv_bytes) if a file exists and its embedded timestamp
    is within file_age_threshold_hours. Returns None otherwise.
    """
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
        return None  # directory not yet created
    if not resp.ok:
        log.warning("GitHub dir listing failed: %s", resp.status_code)
        return None

    prefix = stem + "_"
    matches = [
        f for f in resp.json()
        if isinstance(f, dict)
        and f.get("name", "").startswith(prefix)
        and f["name"].endswith(".csv")
    ]
    if not matches:
        return None

    # Filenames embed the timestamp — sort lexicographically to find the latest.
    latest = max(matches, key=lambda f: f["name"])
    filename = latest["name"]

    ts_match = _TS_RE.search(filename)
    if not ts_match:
        return None
    try:
        file_dt = datetime.strptime(ts_match.group(1), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None

    age_hours = (datetime.now(timezone.utc) - file_dt).total_seconds() / 3600
    if age_hours >= settings.file_age_threshold_hours:
        log.info(
            "GitHub file %s is stale (%.1fh >= %.1fh threshold) — will refresh",
            filename, age_hours, settings.file_age_threshold_hours,
        )
        return None

    download_url = latest.get("download_url")
    if not download_url:
        return None

    dl = session.get(
        download_url,
        headers={"Authorization": f"Bearer {settings.github_token}"},
        timeout=30,
    )
    if not dl.ok:
        log.warning("Failed to download %s from GitHub: %s", filename, dl.status_code)
        return None

    log.info("GitHub cache hit: %s (age: %.1fh)", filename, age_hours)
    return filename, dl.content


def commit_report(
    filename: str,
    csv_bytes: bytes,
    settings: Settings,
    session: requests.Session,
) -> None:
    """Push a CSV file to GitHub. No-op if GITHUB_TOKEN or GITHUB_REPO is not set."""
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

    # Optimistic PUT: filenames are timestamped so collisions are rare.
    # On 422 (file already exists), fetch SHA and retry once.
    # On transient 5xx, retry up to 2 more times with backoff (the session-level
    # Retry adapter only covers POST; background tasks need explicit retry here).
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
            return

        if resp.status_code == 422 and sha is None:
            # File already exists; retrieve its SHA and retry immediately.
            check = session.get(url, headers=headers, timeout=15)
            if check.status_code == 200:
                sha = check.json().get("sha")
            continue

        if resp.status_code in (500, 502, 503, 504) and attempt < 2:
            wait = 2 ** attempt
            log.warning(
                "GitHub commit transient error %d for %s — retrying in %ds",
                resp.status_code, path, wait,
            )
            time.sleep(wait)
            continue

        log.error("GitHub commit failed for %s: %s %s", path, resp.status_code, resp.text[:300])
        return
    else:
        # All attempts used continue (422 + SHA fetch kept failing) — log so it's not silent.
        log.error(
            "GitHub commit failed for %s: exhausted retries, could not retrieve file SHA", path
        )
