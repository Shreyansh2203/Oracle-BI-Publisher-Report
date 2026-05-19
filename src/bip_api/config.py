from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    oracle_username: str
    oracle_password: str
    oracle_base_url: str

    @field_validator("oracle_base_url")
    @classmethod
    def _require_https(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("oracle_base_url must use HTTPS to protect credentials in transit")
        return v

    reports_file: Path = Path("reports.txt")
    max_batch_size: int = 20
    request_timeout: int = 120
    http_pool_size: int = 10
    github_token: str = ""
    github_repo: str = ""
    github_branch: str = "main"
    github_reports_dir: str = "reports"
    file_age_threshold_hours: float = 4
    receipt_report_path: str = ""
    cors_origins: str = ""
    port: int = 8000
    debug: bool = False
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    _paths_cache: list[str] | None = PrivateAttr(default=None)

    def load_report_paths(self) -> list[str]:
        if self._paths_cache is None:
            self._paths_cache = self._read_report_paths()
        return self._paths_cache

    def _read_report_paths(self) -> list[str]:
        if not self.reports_file.exists():
            return []
        return [
            line.strip()
            for line in self.reports_file.read_text().splitlines()
            if line.strip() and (not line.strip().startswith("#"))
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
