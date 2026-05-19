from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
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
from bip_api.routers.reports import _get_github_session, _get_oracle_session

FAKE_SETTINGS = Settings(
    oracle_username="testuser",
    oracle_password="testpass",
    oracle_base_url="https://fake.oracle.com",
    reports_file=Path("/nonexistent/reports.txt"),
)
FAKE_SESSION = MagicMock(spec=requests.Session)
app.dependency_overrides[get_settings] = lambda: FAKE_SETTINGS
app.dependency_overrides[_get_oracle_session] = lambda: FAKE_SESSION
app.dependency_overrides[_get_github_session] = lambda: FAKE_SESSION
CSV_BYTES = b"col1,col2\nval1,val2\n"
FAKE_REPORT_PATH = "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo"


@pytest.fixture(autouse=True)
def reset_fake_session() -> None:
    FAKE_SESSION.reset_mock()


@pytest.fixture(name="client")
def client_fixture() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


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
    mock_fetch.side_effect = [("AR_Report.csv", CSV_BYTES), ("AP_Report.csv", b"col3,col4\n")]
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
        assert sorted(zf.namelist()) == ["AP_Report.csv", "Invoice Details Report.csv"]
        assert zf.read("Invoice Details Report.csv") == CSV_BYTES


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




def test_fetch_report_csv_sanitizes_http_error_body() -> None:
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
    assert "500" in msg


def test_fetch_report_csv_sanitizes_soap_fault() -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock()
    response.status_code = 200
    response.ok = True
    response.text = "<soapenv:Envelope><faultstring>java.io.FileNotFoundException: /opt/oracle/internal/path</faultstring></soapenv:Envelope>"  # noqa: E501
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
    mock_fetch: MagicMock, mock_github: MagicMock, mock_commit: MagicMock, client: TestClient
) -> None:
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
    mock_fetch: MagicMock, mock_github: MagicMock, mock_commit: MagicMock, client: TestClient
) -> None:
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
    mock_fetch.side_effect = [("AR_Report.csv", CSV_BYTES), ReportError("not found")]
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
        assert zf.namelist() == ["Invoice Details Report.csv"]
        assert zf.read("Invoice Details Report.csv") == CSV_BYTES


@patch("bip_api.routers.reports.fetch_report_csv")
def test_x_cache_oracle(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.return_value = ("AR_Report.csv", CSV_BYTES)
    resp = client.post("/reports/download", json=_single_body())
    assert resp.status_code == 200
    assert resp.headers["x-cache"] == "oracle"


@patch("bip_api.routers.reports.fetch_report_csv")
@patch("bip_api.routers.reports.get_latest_report_from_github")
def test_x_cache_github(mock_github: MagicMock, mock_fetch: MagicMock, client: TestClient) -> None:
    mock_github.return_value = ("AR_Report_20250101_120000.csv", CSV_BYTES)
    resp = client.post("/reports/download", json=_single_body())
    assert resp.status_code == 200
    assert resp.headers["x-cache"] == "github"
    mock_fetch.assert_not_called()


@patch("bip_api.routers.reports.fetch_report_csv")
def test_x_cache_zip_uses_highest_cost_tier(mock_fetch: MagicMock, client: TestClient) -> None:
    mock_fetch.side_effect = [("AR_Report.csv", CSV_BYTES), ("AP_Report.csv", b"col3,col4\n")]
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
    assert resp.headers["x-cache"] == "oracle"


def test_github_cache_ignores_files_with_longer_stem() -> None:
    from unittest.mock import MagicMock

    from bip_api.github import get_latest_report_from_github

    settings = Settings(
        oracle_username="u",
        oracle_password="p",
        oracle_base_url="https://x.com",
        github_token="tok",
        github_repo="owner/repo",
    )
    session = MagicMock(spec=requests.Session)
    dir_listing = MagicMock()
    dir_listing.status_code = 200
    dir_listing.ok = True
    dir_listing.json.return_value = [
        {"name": "AR_Aging_20250101_120000.csv", "download_url": "https://example.com/file.csv"}
    ]
    file_resp = MagicMock()
    file_resp.ok = True
    file_resp.content = CSV_BYTES
    session.get.side_effect = [dir_listing, file_resp]
    result = get_latest_report_from_github("AR", settings, session)
    assert result is None
    assert session.get.call_count == 1



@patch("bip_api.routers.reports.fetch_report_csv")
def test_receipt_number_filter_case_insensitive(mock_fetch: MagicMock, client: TestClient) -> None:
    csv_data = b"RECEIPT_NUMBER,NAME\r\nREC001 ,Acme Corp\r\n"
    mock_fetch.return_value = ("Receipt.csv", csv_data)
    resp = client.post(
        "/reports/download",
        json={"reports": [{"report_path": FAKE_REPORT_PATH, "receipt_number": "rec001"}]},
    )
    assert resp.status_code == 200
    assert b"Acme Corp" in resp.content


@patch("bip_api.routers.reports.fetch_report_csv")
def test_download_non_utf8_csv_does_not_crash(mock_fetch: MagicMock, client: TestClient) -> None:
    latin1_csv = b"RECEIPT_NUMBER,NAME\r\n18-19/Jan/JV0899,Caf\x96 Corp\r\n"
    mock_fetch.return_value = ("Receipt.csv", latin1_csv)
    resp = client.post(
        "/reports/download",
        json={"reports": [{"report_path": FAKE_REPORT_PATH, "receipt_number": "18-19/Jan/JV0899"}]},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]


RECEIPT_CSV = "BILL_CUSTOMER_NAME,RECEIPT_NUMBER,RECEIPT_DATE,RECEIPT_AMOUNT\nAcme Corp,REC001,15-01-2024,1000.00\nAcme Corp,REC002,20-01-2024,500.50\nOther Co,REC003,10-02-2024,250.00\n"  # noqa: E501
MATCH_SETTINGS = Settings(
    oracle_username="testuser",
    oracle_password="testpass",
    oracle_base_url="https://fake.oracle.com",
    receipt_report_path="/Custom/Receipts/Receipt_Details.xdo",
    reports_file=Path("/nonexistent/reports.txt"),
)


@pytest.fixture()
def match_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: MATCH_SETTINGS
    app.dependency_overrides[_get_oracle_session] = lambda: FAKE_SESSION
    app.dependency_overrides[_get_github_session] = lambda: FAKE_SESSION
    with TestClient(app) as c:
        yield c
    app.dependency_overrides[get_settings] = lambda: FAKE_SETTINGS


@patch("bip_api.routers.reports.fetch_report_csv")
def test_match_by_receipt_number(mock_fetch: MagicMock, match_client: TestClient) -> None:
    mock_fetch.return_value = ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode())
    resp = match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "payment_date": "2024/01/15",
            "total_amount": 1000.0,
            "invoices": [],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fusion_receipt_number"] == "REC001"
    assert data["fusion_customer_name"] == "Acme Corp"
    assert data["fusion_receipt_date"] == "2024/01/15"


@patch("bip_api.routers.reports.fetch_report_csv")
def test_match_step1_wrong_amount_returns_null(
    mock_fetch: MagicMock, match_client: TestClient
) -> None:
    mock_fetch.return_value = ("Receipt_Details_20240115_140000.csv", RECEIPT_CSV.encode())
    resp = match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 999.0,
            "invoices": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["fusion_receipt_number"] is None


@patch("bip_api.routers.reports.fetch_report_csv")
def test_match_by_amount_and_date(mock_fetch: MagicMock, match_client: TestClient) -> None:
    mock_fetch.return_value = ("Receipt_Details_20240120_120000.csv", RECEIPT_CSV.encode())
    resp = match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_date": "2024/01/20",
            "total_amount": 500.5,
            "invoices": [],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fusion_receipt_number"] == "REC002"
    assert data["fusion_receipt_date"] == "2024/01/20"


@patch("bip_api.routers.reports.fetch_report_csv")
def test_match_no_match_returns_nulls(mock_fetch: MagicMock, match_client: TestClient) -> None:
    mock_fetch.return_value = ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode())
    resp = match_client.post(
        "/reports/match",
        json={"customer_name": "Unknown Corp", "payment_reference": "DOESNOTEXIST", "invoices": []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fusion_receipt_number"] is None
    assert data["fusion_customer_name"] is None
    assert data["fusion_receipt_date"] is None


@patch("bip_api.routers.reports.fetch_report_csv")
def test_match_ambiguous_returns_nulls(mock_fetch: MagicMock, match_client: TestClient) -> None:
    ambiguous_csv = "BILL_CUSTOMER_NAME,RECEIPT_NUMBER,RECEIPT_DATE,RECEIPT_AMOUNT\nAcme Corp,REC-A,15-01-2024,1000.00\nAcme Corp,REC-B,15-01-2024,1000.00\n"  # noqa: E501
    mock_fetch.return_value = ("Receipt_Details_20240115_130000.csv", ambiguous_csv.encode())
    resp = match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_date": "2024/01/15",
            "total_amount": 1000.0,
            "invoices": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["fusion_receipt_number"] is None


INVOICE_CSV = "TRANSACTION_NUMBER,TRANSACTION_DATE,TOTAL_AMOUNTS,DOCUMENT_NUMBER\nINV-001,15-01-2024,500.00,INV-001\n126125908454,20-01-2024,750.00,DOC-002\n6153004273089,22-01-2024,300.00,DOC-003\n"  # noqa: E501


@pytest.fixture()
def invoice_match_client(tmp_path: Path) -> Iterator[TestClient]:
    reports_file = tmp_path / "reports.txt"
    reports_file.write_text(
        "/Custom/Receipts/Receipt_Details.xdo\n/Custom/Invoices/Invoice_Details.xdo\n"
    )
    settings = Settings(
        oracle_username="testuser",
        oracle_password="testpass",
        oracle_base_url="https://fake.oracle.com",
        receipt_report_path="/Custom/Receipts/Receipt_Details.xdo",
        reports_file=reports_file,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_oracle_session] = lambda: FAKE_SESSION
    app.dependency_overrides[_get_github_session] = lambda: FAKE_SESSION
    with TestClient(app) as c:
        yield c
    app.dependency_overrides[get_settings] = lambda: FAKE_SETTINGS


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_step1_exact(mock_fetch: MagicMock, invoice_match_client: TestClient) -> None:
    mock_fetch.side_effect = [
        ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode()),
        ("Invoice_Details_20240115_120000.csv", INVOICE_CSV.encode()),
    ]
    resp = invoice_match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [
                {"invoice_number": "INV-001", "invoice_date": "15-01-2024", "invoice_amount": 500.0}
            ],
        },
    )
    assert resp.status_code == 200
    inv = resp.json()["invoices"][0]
    assert inv["fusion_invoice_number"] == "INV-001"
    assert inv["fusion_invoice_date"] == "2024/01/15"
    assert inv["fusion_invoice_amount"] == 500.0


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_step2_customer_invoice_number(
    mock_fetch: MagicMock, invoice_match_client: TestClient
) -> None:
    mock_fetch.side_effect = [
        ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode()),
        ("Invoice_Details_20240115_120001.csv", INVOICE_CSV.encode()),
    ]
    resp = invoice_match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [
                {
                    "invoice_number": "NOMATCH",
                    "customer_invoice_number": "INV-001",
                    "invoice_date": "15-01-2024",
                }
            ],
        },
    )
    assert resp.status_code == 200
    inv = resp.json()["invoices"][0]
    assert inv["fusion_invoice_number"] == "INV-001"


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_step3_substring(
    mock_fetch: MagicMock, invoice_match_client: TestClient
) -> None:
    mock_fetch.side_effect = [
        ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode()),
        ("Invoice_Details_20240115_120002.csv", INVOICE_CSV.encode()),
    ]
    resp = invoice_match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [{"invoice_number": "25908454", "invoice_date": "20-01-2024"}],
        },
    )
    assert resp.status_code == 200
    inv = resp.json()["invoices"][0]
    assert inv["fusion_invoice_number"] == "126125908454"


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_ambiguous_returns_nulls(
    mock_fetch: MagicMock, invoice_match_client: TestClient
) -> None:
    ambiguous_inv_csv = "TRANSACTION_NUMBER,TRANSACTION_DATE,TOTAL_AMOUNTS,DOCUMENT_NUMBER\n126125908454,20-01-2024,750.00,DOC-A\n999125908454,20-01-2024,200.00,DOC-B\n"  # noqa: E501
    mock_fetch.side_effect = [
        ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode()),
        ("Invoice_Details_20240115_120003.csv", ambiguous_inv_csv.encode()),
    ]
    resp = invoice_match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [{"invoice_number": "25908454", "invoice_date": "20-01-2024"}],
        },
    )
    assert resp.status_code == 200
    inv = resp.json()["invoices"][0]
    assert inv["fusion_invoice_number"] is None
    assert inv["fusion_invoice_date"] is None
    assert inv["fusion_invoice_amount"] is None


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_no_invoice_report_returns_null_fusion(
    mock_fetch: MagicMock, match_client: TestClient
) -> None:
    mock_fetch.return_value = ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode())
    resp = match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [{"invoice_number": "INV-001", "invoice_date": "15-01-2024"}],
        },
    )
    assert resp.status_code == 200
    inv = resp.json()["invoices"][0]
    assert inv["fusion_invoice_number"] is None
    assert inv["fusion_invoice_date"] is None
    assert inv["fusion_invoice_amount"] is None


@patch("bip_api.routers.reports.fetch_report_csv")
def test_match_receipt_number_case_insensitive(
    mock_fetch: MagicMock, match_client: TestClient
) -> None:
    mock_fetch.return_value = ("Receipt_Details_20240115_150000.csv", RECEIPT_CSV.encode())
    resp = match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "rec001",
            "total_amount": 1000.0,
            "invoices": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["fusion_receipt_number"] == "REC001"


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_step1_case_insensitive(
    mock_fetch: MagicMock, invoice_match_client: TestClient
) -> None:
    mock_fetch.side_effect = [
        ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode()),
        ("Invoice_Details_20240115_120010.csv", INVOICE_CSV.encode()),
    ]
    resp = invoice_match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [{"invoice_number": "inv-001", "invoice_date": "15-01-2024"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["invoices"][0]["fusion_invoice_number"] == "INV-001"


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_step2_case_insensitive(
    mock_fetch: MagicMock, invoice_match_client: TestClient
) -> None:
    mock_fetch.side_effect = [
        ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode()),
        ("Invoice_Details_20240115_120011.csv", INVOICE_CSV.encode()),
    ]
    resp = invoice_match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [
                {
                    "invoice_number": "NOMATCH",
                    "customer_invoice_number": "inv-001",
                    "invoice_date": "15-01-2024",
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["invoices"][0]["fusion_invoice_number"] == "INV-001"


@patch("bip_api.routers.reports.fetch_report_csv")
def test_invoice_match_step3_substring_case_insensitive(
    mock_fetch: MagicMock, invoice_match_client: TestClient
) -> None:
    upper_inv_csv = "TRANSACTION_NUMBER,TRANSACTION_DATE,TOTAL_AMOUNTS,DOCUMENT_NUMBER\nABCDEF123456,20-01-2024,750.00,DOC-001\n"  # noqa: E501
    mock_fetch.side_effect = [
        ("Receipt_Details_20240115_120000.csv", RECEIPT_CSV.encode()),
        ("Invoice_Details_20240115_120012.csv", upper_inv_csv.encode()),
    ]
    resp = invoice_match_client.post(
        "/reports/match",
        json={
            "customer_name": "Acme Corp",
            "payment_reference": "REC001",
            "total_amount": 1000.0,
            "invoices": [{"invoice_number": "def123", "invoice_date": "20-01-2024"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["invoices"][0]["fusion_invoice_number"] == "ABCDEF123456"


def test_empty_invoice_number_rejected() -> None:
    from pydantic import ValidationError

    from bip_api.models import InvoiceItem

    with pytest.raises(ValidationError, match="invoice_number cannot be empty"):
        InvoiceItem(invoice_number="")

    with pytest.raises(ValidationError, match="invoice_number cannot be empty"):
        InvoiceItem(invoice_number="   ")


def test_report_stem_strips_unsafe_characters() -> None:
    from bip_api.client import report_stem

    assert report_stem('/Custom/Finance/AR"; evil=true.xdo') == "AR_eviltrue"
    assert report_stem("/Custom/Finance/Normal Report.xdo") == "Normal_Report"
    assert report_stem("/Custom/Finance/Report-2024.xdo") == "Report-2024"



def test_match_no_receipt_path_configured(client: TestClient, tmp_path: Path) -> None:
    empty_settings = Settings(
        oracle_username="u",
        oracle_password="p",
        oracle_base_url="https://x.com",
        reports_file=tmp_path / "empty.txt",
        receipt_report_path="",
    )
    (tmp_path / "empty.txt").write_text("")
    app.dependency_overrides[get_settings] = lambda: empty_settings
    resp = client.post("/reports/match", json={"customer_name": "Acme", "invoices": []})
    assert resp.status_code == 500
    assert "receipt report path" in resp.json()["detail"].lower()
    app.dependency_overrides[get_settings] = lambda: FAKE_SETTINGS
