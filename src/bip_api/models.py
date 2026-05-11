from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")


class DownloadRequest(BaseModel):
    report_path: str
    customer_name: str | None = None
    from_date: str | None = None      # DD-MM-YYYY — passed to Oracle as P_FROM_DATE
    to_date: str | None = None        # DD-MM-YYYY — passed to Oracle as P_TO_DATE
    receipt_number: str | None = None  # post-filter: matched against RECEIPT_NUMBER column in CSV

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "report_path": "/Custom/Finance/AR_Aging_Report.xdo",
                    "customer_name": "Acme Corp",
                    "from_date": "01-01-2024",
                    "to_date": "31-03-2024",
                },
                {
                    "report_path": "/Custom/Finacials/Receivables/Receipt Details Report.xdo",
                    "receipt_number": "18-19/Jan/JV0899",
                },
            ]
        }
    }

    @field_validator("report_path", mode="before")
    @classmethod
    def validate_report_path(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("report_path cannot be empty")
        return v

    @field_validator("from_date", "to_date", mode="before")
    @classmethod
    def validate_date_format(cls, v: str | None) -> str | None:
        if v is not None and not _DATE_RE.match(v):
            raise ValueError("Date must be in DD-MM-YYYY format")
        return v

    @property
    def has_filters(self) -> bool:
        # Only Oracle-level filters — receipt_number is a Python post-filter and
        # does not prevent GitHub caching of the full report.
        return bool(self.customer_name or self.from_date or self.to_date)


class ReportRequest(BaseModel):
    """
    Request body for `POST /reports/download`.

    Always a list of one or more `DownloadRequest`s. The endpoint returns a
    raw CSV when `len(reports) == 1` and a ZIP archive when `len(reports) > 1`.
    """

    reports: list[DownloadRequest]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"reports": [{"report_path": "/Custom/Finance/AR_Aging_Report.xdo"}]},
                {
                    "reports": [
                        {"report_path": "/Custom/Finance/AR_Aging_Report.xdo"},
                        {
                            "report_path": "/Custom/Finance/AP_Report.xdo",
                            "from_date": "01-01-2024",
                            "to_date": "31-03-2024",
                        },
                    ]
                },
            ]
        }
    }


class ReportItem(BaseModel):
    path: str
    name: str


class ReportListResponse(BaseModel):
    reports: list[ReportItem]


class HealthResponse(BaseModel):
    status: str
    version: str


# --- Match endpoint models ---

class InvoiceItem(BaseModel):
    invoice_number: str
    invoice_date: str | None = None
    invoice_amount: float | None = None
    description: str | None = None
    customer_invoice_number: str | None = None
    storeNo: str | None = None


class ReceiptRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    customer_name: str
    payment_reference: str | None = None
    payment_date: str | None = None  # YYYY/MM/DD
    total_amount: float | None = None
    invoices: list[InvoiceItem] = []
    confidence_score: float | None = None
    confidence_label: str | None = None
    invoice_count: int | None = None
    meta: dict | None = Field(None, alias="_meta")


class FusedInvoiceItem(BaseModel):
    invoice_number: str
    fusion_invoice_number: str | None = None
    invoice_date: str | None = None
    fusion_invoice_date: str | None = None
    invoice_amount: float | None = None
    fusion_invoice_amount: float | None = None
    description: str | None = None
    customer_invoice_number: str | None = None
    storeNo: str | None = None


class MatchedRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    customer_name: str
    fusion_customer_name: str | None = None
    payment_reference: str | None = None
    fusion_receipt_number: str | None = None
    payment_date: str | None = None
    fusion_receipt_date: str | None = None
    invoices: list[FusedInvoiceItem] = []
    total_amount: float | None = None
    confidence_score: float | None = None
    confidence_label: str | None = None
    invoice_count: int | None = None
    meta: dict | None = Field(None, alias="_meta")
