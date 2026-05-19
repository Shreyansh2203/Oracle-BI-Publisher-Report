from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DownloadRequest(BaseModel):
    report_path: str
    receipt_number: str | None = None
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "report_path": "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo",  # noqa: E501
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


class ReportRequest(BaseModel):
    reports: list[DownloadRequest]
    model_config = {
        "json_schema_extra": {
            "examples": [
                {"reports": [{"report_path": "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo"}]},  # noqa: E501
                {
                    "reports": [
                        {"report_path": "/Custom/Finacials/Receivable Transactions/Invoice Details Report.xdo"},  # noqa: E501
                        {"report_path": "/Custom/Finacials/Receivables/Receipt Details Report.xdo"},
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


class InvoiceItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    line_id: int | None = Field(None, alias="Line_ID")
    invoice_number: str
    invoice_date: str | None = None
    invoice_amount: float | None = None
    description: str | None = None
    customer_invoice_number: str | None = None
    store_no: str | None = Field(None, alias="storeNo")

    @field_validator("invoice_number", mode="before")
    @classmethod
    def _require_invoice_number(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            raise ValueError("invoice_number cannot be empty")
        return v

    @field_validator("invoice_date", "description", "customer_invoice_number", "store_no", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        return None if v == "" else v


class ReceiptRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    customer_name: str
    payment_reference: str | None = None
    payment_date: str | None = None
    header_id: int | None = None
    invoices: list[InvoiceItem] = []
    total_amount: float | None = None
    confidence_score: float | None = None
    confidence_label: str | None = None
    invoice_count: int | None = None
    meta: dict[str, object] | None = Field(None, alias="_meta")

    @field_validator("payment_reference", "payment_date", "confidence_label", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        return None if v == "" else v


class FusedInvoiceItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    line_id: int | None = Field(None, alias="Line_ID")
    invoice_number: str
    fusion_invoice_number: str | None = None
    invoice_date: str | None = None
    fusion_invoice_date: str | None = None
    invoice_amount: float | None = None
    fusion_invoice_amount: float | None = None
    description: str | None = None
    customer_invoice_number: str | None = None
    store_no: str | None = Field(None, alias="storeNo")


class MatchedRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    customer_name: str
    fusion_customer_name: str | None = None
    payment_reference: str | None = None
    fusion_receipt_number: str | None = None
    payment_date: str | None = None
    fusion_receipt_date: str | None = None
    header_id: int | None = None
    invoices: list[FusedInvoiceItem] = []
    total_amount: float | None = None
    confidence_score: float | None = None
    confidence_label: str | None = None
    invoice_count: int | None = None
    meta: dict[str, object] | None = Field(None, alias="_meta")
