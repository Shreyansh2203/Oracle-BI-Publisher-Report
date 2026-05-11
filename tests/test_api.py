from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from bip_api.client import fetch_report_csv
from bip_api.config import Settings, get_settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.main import app
from bip_api.models import DownloadRequest
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


def _single_body(report_path: str = FAKE_REPORT_PATH, **extra: str) -> dict[str, object]:
    return {"reports": [{"report_path": report_path, **extra}]}


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_report_success(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.return_value = ("AR_Report.csv", CSV_BYTES)
    resp = client.post("/reports/download", json=_single_body())
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert resp.content == CSV_BYTES


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_report_auth_error(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = AuthError("Bad credentials")
    resp = client.post("/reports/download", json=_single_body())
    assert resp.status_code == 401
    assert "Bad credentials" in resp.json()["detail"]


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_report_report_error(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = ReportError("Report not found")
    resp = client.post("/reports/download", json=_single_body())
    assert resp.status_code == 502
    assert "Report not found" in resp.json()["detail"]


def test_download_rejects_bare_single_shape(client: TestClient) -> None:
    """The endpoint requires `{"reports": [...]}`; a bare DownloadRequest is invalid."""
    resp = client.post("/reports/download", json={"report_path": FAKE_REPORT_PATH})
    assert resp.status_code == 422


def test_download_batch_empty(client: TestClient) -> None:
    resp = client.post("/reports/download", json={"reports": []})
    assert resp.status_code == 400


def test_download_batch_exceeds_limit(client: TestClient) -> None:
    reports = [{"report_path": FAKE_REPORT_PATH}] * 21
    resp = client.post("/reports/download", json={"reports": reports})
    assert resp.status_code == 400
    assert "exceeds limit" in resp.json()["detail"]


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_multi_returns_zip(mock_fetch: MagicMock, client: TestClient) -> None:
    """A request with two reports should return a ZIP archive."""
    mock_fetch.side_effect = [
        ("AR_Report.csv", CSV_BYTES),
        ("AP_Report.csv", b"col3,col4\n"),
    ]
    resp = client.post(
        "/reports/download",
        json={
            "reports": [
                {"report_path": FAKE_REPORT_PATH},
                {"report_path": "/Custom/Finance/AP_Report.xdo"},
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert sorted(zf.namelist()) == ["AP_Report.csv", "AR_Report.csv"]
        assert zf.read("AR_Report.csv") == CSV_BYTES


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_batch_auth_error(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = AuthError("Unauthorized")
    resp = client.post(
        "/reports/download",
        json={
            "reports": [
                {"report_path": FAKE_REPORT_PATH},
                {"report_path": "/Custom/Finance/AP_Report.xdo"},
            ]
        },
    )
    assert resp.status_code == 401


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_batch_all_errors_returns_502(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = ReportError("not found")
    resp = client.post(
        "/reports/download",
        json={
            "reports": [
                {"report_path": FAKE_REPORT_PATH},
                {"report_path": "/Custom/Finance/AP_Report.xdo"},
            ]
        },
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
        json=_single_body(customer_name="Acme Corp"),
    )

    assert resp.status_code == 200
    assert resp.content == CSV_BYTES
    mock_github.assert_not_called()
    mock_fetch.assert_called_once()
    mock_commit.assert_not_called()


def test_fetch_report_csv_sanitizes_http_error_body() -> None:
    """A non-2xx response from Oracle must not be echoed back in the raised error."""
    session = MagicMock(spec=requests.Session)
    response = MagicMock()
    response.status_code = 500
    response.ok = False
    response.text = "Internal Oracle error: /opt/oracle/secrets/keystore exposed"
    session.post.return_value = response

    req = DownloadRequest(report_path="/Custom/X.xdo")
    with pytest.raises(ReportError) as exc:
        fetch_report_csv(req, FAKE_SETTINGS, session)

    msg = str(exc.value)
    assert "/opt/oracle" not in msg
    assert "keystore" not in msg
    assert "500" in msg  # status code is fine to expose


def test_fetch_report_csv_sanitizes_soap_fault() -> None:
    """A SOAP <faultstring> must be logged but not surfaced in the error message."""
    session = MagicMock(spec=requests.Session)
    response = MagicMock()
    response.status_code = 200
    response.ok = True
    response.text = (
        "<soapenv:Envelope><faultstring>"
        "java.io.FileNotFoundException: /opt/oracle/internal/path"
        "</faultstring></soapenv:Envelope>"
    )
    session.post.return_value = response

    req = DownloadRequest(report_path="/Custom/X.xdo")
    with pytest.raises(ReportError) as exc:
        fetch_report_csv(req, FAKE_SETTINGS, session)
    assert "FileNotFoundException" not in str(exc.value)
    assert "/opt/oracle" not in str(exc.value)


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

    resp = client.post("/reports/download", json=_single_body())

    assert resp.status_code == 200
    assert resp.content == CSV_BYTES
    mock_github.assert_called_once()
    mock_fetch.assert_not_called()


@patch("bip_api.routers.reports.commit_report")
@patch("bip_api.routers.reports.get_latest_report_from_github")
@patch("bip_api.routers.reports.fetch_report_csv")
def test_fresh_oracle_fetch_triggers_github_commit(
    mock_fetch: MagicMock,
    mock_github: MagicMock,
    mock_commit: MagicMock,
    client: TestClient,
) -> None:
    """When GitHub has no fresh file, Oracle is fetched and a GitHub commit is scheduled."""
    mock_github.return_value = None
    mock_fetch.return_value = ("AR_Report.csv", CSV_BYTES)

    resp = client.post("/reports/download", json=_single_body())

    assert resp.status_code == 200
    mock_fetch.assert_called_once()
    mock_commit.assert_called_once()


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_batch_partial_failure_returns_zip_with_headers(
    mock_fetch: MagicMock, client: TestClient
) -> None:
    """If one report in a batch fails, successful ones are returned in ZIP with count headers."""
    mock_fetch.side_effect = [
        ("AR_Report.csv", CSV_BYTES),
        ReportError("not found"),
    ]
    resp = client.post(
        "/reports/download",
        json={
            "reports": [
                {"report_path": FAKE_REPORT_PATH},
                {"report_path": "/Custom/Finance/AP_Report.xdo"},
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.headers["x-succeeded-count"] == "1"
    assert resp.headers["x-failed-count"] == "1"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["AR_Report.csv"]
        assert zf.read("AR_Report.csv") == CSV_BYTES


