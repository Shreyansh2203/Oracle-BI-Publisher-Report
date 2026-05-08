from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")


class DownloadRequest(BaseModel):
    report_path: str
    customer_name: str | None = None
    from_date: str | None = None  # DD-MM-YYYY
    to_date: str | None = None    # DD-MM-YYYY

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "report_path": "/Custom/Finance/AR_Aging_Report.xdo",
                    "customer_name": "Acme Corp",
                    "from_date": "01-01-2024",
                    "to_date": "31-03-2024",
                }
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
        return bool(self.customer_name or self.from_date or self.to_date)


class BatchDownloadRequest(BaseModel):
    reports: list[DownloadRequest]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "reports": [
                        {"report_path": "/Custom/Finance/AR_Aging_Report.xdo"},
                        {
                            "report_path": "/Custom/Finance/AP_Report.xdo",
                            "from_date": "01-01-2024",
                            "to_date": "31-03-2024",
                        },
                    ]
                }
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
