from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from bip_api.config import Settings, get_settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.main import app
from bip_api.routers.reports import _get_session

FAKE_SETTINGS = Settings(
    oracle_username="testuser",
    oracle_password="testpass",
    oracle_base_url="https://fake.oracle.com",
)
FAKE_SESSION = MagicMock(spec=requests.Session)

app.dependency_overrides[get_settings] = lambda: FAKE_SETTINGS
app.dependency_overrides[_get_session] = lambda: FAKE_SESSION


CSV_BYTES = b"col1,col2\nval1,val2\n"
FAKE_REPORT_PATH = "/Custom/Finance/AR_Report.xdo"


@pytest.fixture(name="client")
def client_fixture() -> TestClient:
    # Use context manager so lifespan (session init) runs correctly.
    with TestClient(app) as c:
        return c


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_reports_no_file(client: TestClient, tmp_path: Path) -> None:
    settings = Settings(
        oracle_username="u",
        oracle_password="p",
        oracle_base_url="https://x.com",
        reports_file=tmp_path / "missing.txt",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    resp = client.get("/reports")
    assert resp.status_code == 200
    assert resp.json() == {"reports": []}
    app.dependency_overrides[get_settings] = lambda: FAKE_SETTINGS


def test_list_reports_with_file(client: TestClient, tmp_path: Path) -> None:
    reports_file = tmp_path / "reports.txt"
    reports_file.write_text("# comment\n/Custom/Finance/AR_Report.xdo\n")
    settings = Settings(
        oracle_username="u",
        oracle_password="p",
        oracle_base_url="https://x.com",
        reports_file=reports_file,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    resp = client.get("/reports")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["reports"]) == 1
    assert data["reports"][0]["name"] == "AR_Report"
    app.dependency_overrides[get_settings] = lambda: FAKE_SETTINGS


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_report_success(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.return_value = ("AR_Report.csv", CSV_BYTES)
    resp = client.post("/reports/download", json={"report_path": FAKE_REPORT_PATH})
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert resp.content == CSV_BYTES


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_report_auth_error(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = AuthError("Bad credentials")
    resp = client.post("/reports/download", json={"report_path": FAKE_REPORT_PATH})
    assert resp.status_code == 401
    assert "Bad credentials" in resp.json()["detail"]


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_report_report_error(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = ReportError("Report not found")
    resp = client.post("/reports/download", json={"report_path": FAKE_REPORT_PATH})
    assert resp.status_code == 502
    assert "Report not found" in resp.json()["detail"]


def test_download_batch_empty(client: TestClient) -> None:
    resp = client.post("/reports/download-batch", json={"reports": []})
    assert resp.status_code == 400


def test_download_batch_exceeds_limit(client: TestClient) -> None:
    reports = [{"report_path": FAKE_REPORT_PATH}] * 21
    resp = client.post("/reports/download-batch", json={"reports": reports})
    assert resp.status_code == 400
    assert "exceeds limit" in resp.json()["detail"]


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_batch_success(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.return_value = ("AR_Report.csv", CSV_BYTES)
    resp = client.post(
        "/reports/download-batch",
        json={"reports": [{"report_path": FAKE_REPORT_PATH}]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert "AR_Report.csv" in zf.namelist()
        assert zf.read("AR_Report.csv") == CSV_BYTES


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_batch_auth_error(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = AuthError("Unauthorized")
    resp = client.post(
        "/reports/download-batch",
        json={"reports": [{"report_path": FAKE_REPORT_PATH}]},
    )
    assert resp.status_code == 401


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_batch_all_errors_returns_502(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = ReportError("not found")
    resp = client.post(
        "/reports/download-batch",
        json={"reports": [{"report_path": FAKE_REPORT_PATH}]},
    )
    assert resp.status_code == 502


@patch("bip_api.routers.reports.commit_report")
@patch("bip_api.routers.reports.get_latest_report_from_github")
@patch("bip_api.routers.reports.fetch_report_csv")
def test_filtered_request_bypasses_github(
    mock_fetch: MagicMock,
    mock_github: MagicMock,
    mock_commit: MagicMock,
    client: TestClient,
) -> None:
    """A request with filter params must skip the GitHub cache (read and write)."""
    mock_github.return_value = ("stale_AR_Report.csv", b"WRONG_FILTERED_DATA")
    mock_fetch.return_value = ("AR_Report.csv", CSV_BYTES)

    resp = client.post(
        "/reports/download",
        json={"report_path": FAKE_REPORT_PATH, "customer_name": "Acme Corp"},
    )

    assert resp.status_code == 200
    assert resp.content == CSV_BYTES
    mock_github.assert_not_called()
    mock_fetch.assert_called_once()
    mock_commit.assert_not_called()


@patch("bip_api.routers.reports.commit_report")
@patch("bip_api.routers.reports.get_latest_report_from_github")
@patch("bip_api.routers.reports.fetch_report_csv")
def test_unfiltered_request_uses_github_cache(
    mock_fetch: MagicMock,
    mock_github: MagicMock,
    mock_commit: MagicMock,
    client: TestClient,
) -> None:
    """An unfiltered request should serve from GitHub when a fresh file exists."""
    mock_github.return_value = ("AR_Report_20250101_120000.csv", CSV_BYTES)

    resp = client.post("/reports/download", json={"report_path": FAKE_REPORT_PATH})

    assert resp.status_code == 200
    assert resp.content == CSV_BYTES
    mock_github.assert_called_once()
    mock_fetch.assert_not_called()


