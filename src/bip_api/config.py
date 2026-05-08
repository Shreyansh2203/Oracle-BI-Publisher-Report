from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    oracle_username: str
    oracle_password: str
    oracle_base_url: str
    reports_file: Path = Path("reports.txt")

    max_batch_size: int = 20
    request_timeout: int = 120  # seconds per Oracle BIP call
    http_pool_size: int = 10    # max connections in shared session pool
    cache_ttl: int = 300        # seconds to cache report results; 0 = disabled

    # GitHub — leave empty to disable auto-commit of downloaded reports
    github_token: str = ""
    github_repo: str = ""        # format: owner/repo
    github_branch: str = "main"
    github_reports_dir: str = "reports"

    # File-age check: skip Oracle if a GitHub file is younger than this threshold
    file_age_threshold_hours: float = 4.0

    # Scheduler — auto-refresh all reports.txt entries on a background loop
    schedule_enabled: bool = False
    schedule_interval_hours: float = 1.0  # how often the scheduler checks

    debug: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def load_report_paths(self) -> list[str]:
        """Read XDO paths from reports_file; skip blanks and `#` comments."""
        if not self.reports_file.exists():
            return []
        return [
            line.strip()
            for line in self.reports_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]


@lru_cache
def get_settings() -> Settings:
    # Required fields are loaded from env / .env at runtime; mypy can't see that.
    return Settings()  # type: ignore[call-arg]
