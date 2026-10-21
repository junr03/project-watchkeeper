"""Runtime configuration for the synthetic first slice."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    database: Path
    email_dir: Path
    intake_token: str
    reviewer_token: str
    ui_username: str
    ui_password: str
    recipient: str = "watchkeeper-demo@example.invalid"
    public_base_url: str = "http://127.0.0.1:8765"
    stale_after_seconds: int = 3600
    transcript_retention_days: int = 30
    privacy_journal: Path | None = None
    inferred_after_days: int = 3
    schedule_timezone: str = "UTC"
    review_time: str = "08:00"
    digest_time: str = "08:15"
    review_window_limit: int = 100
    review_window_bytes: int = 180000
    review_windows_per_day: int = 4
    scheduler_poll_seconds: int = 30

    def __post_init__(self):
        if not self.intake_token or not self.reviewer_token or self.intake_token == self.reviewer_token:
            raise ValueError("intake and reviewer tokens must be set and distinct")
        if not self.ui_username or not self.ui_password:
            raise ValueError("UI username and password must be set")
        if "\r" in self.recipient or "\n" in self.recipient:
            raise ValueError("recipient must be a single safe address")
        if "\r" in self.public_base_url or "\n" in self.public_base_url:
            raise ValueError("public_base_url must be a single URL")
        if type(self.transcript_retention_days) is not int or not 1 <= self.transcript_retention_days <= 36500:
            raise ValueError("transcript_retention_days must be an integer from 1 to 36500")
        journal = self.privacy_journal or Path(str(self.database) + ".privacy.jsonl")
        if Path(journal).resolve() == Path(self.database).resolve():
            raise ValueError("privacy journal must be a separate file from the database")
        object.__setattr__(self, "privacy_journal", Path(journal))

    @classmethod
    def from_env(cls) -> "Config":
        import os
        required = ("WATCHKEEPER_INTAKE_TOKEN", "WATCHKEEPER_REVIEWER_TOKEN",
                    "WATCHKEEPER_UI_USERNAME", "WATCHKEEPER_UI_PASSWORD")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError("missing required runtime settings: " + ", ".join(missing))
        return cls(
            database=Path(os.environ.get("WATCHKEEPER_DATABASE", "watchkeeper-data/watchkeeper.sqlite3")),
            email_dir=Path(os.environ.get("WATCHKEEPER_EMAIL_DIR", "watchkeeper-data/email")),
            intake_token=os.environ["WATCHKEEPER_INTAKE_TOKEN"],
            reviewer_token=os.environ["WATCHKEEPER_REVIEWER_TOKEN"],
            ui_username=os.environ["WATCHKEEPER_UI_USERNAME"],
            ui_password=os.environ["WATCHKEEPER_UI_PASSWORD"],
            recipient=os.environ.get("WATCHKEEPER_RECIPIENT", "watchkeeper-demo@example.invalid"),
            public_base_url=os.environ.get("WATCHKEEPER_PUBLIC_BASE_URL", "http://127.0.0.1:8765"),
            stale_after_seconds=int(os.environ.get("WATCHKEEPER_STALE_AFTER_SECONDS", "3600")),
            transcript_retention_days=int(os.environ.get("WATCHKEEPER_TRANSCRIPT_RETENTION_DAYS", "30")),
            privacy_journal=Path(os.environ["WATCHKEEPER_PRIVACY_JOURNAL"]) if os.environ.get("WATCHKEEPER_PRIVACY_JOURNAL") else None,
            inferred_after_days=int(os.environ.get("WATCHKEEPER_INFERRED_AFTER_DAYS", "3")),
            schedule_timezone=os.environ.get("WATCHKEEPER_SCHEDULE_TIMEZONE", "UTC"),
            review_time=os.environ.get("WATCHKEEPER_REVIEW_TIME", "08:00"),
            digest_time=os.environ.get("WATCHKEEPER_DIGEST_TIME", "08:15"),
            review_window_limit=int(os.environ.get("WATCHKEEPER_REVIEW_WINDOW_LIMIT", "100")),
            review_window_bytes=int(os.environ.get("WATCHKEEPER_REVIEW_WINDOW_BYTES", "180000")),
            review_windows_per_day=int(os.environ.get("WATCHKEEPER_REVIEW_WINDOWS_PER_DAY", "4")),
            scheduler_poll_seconds=int(os.environ.get("WATCHKEEPER_SCHEDULER_POLL_SECONDS", "30")),
        )
