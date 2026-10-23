"""Validated, secret-file-aware runtime configuration."""

from dataclasses import dataclass
from email.utils import parseaddr
from ipaddress import ip_address
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _secret(name: str, default: str | None = None) -> str | None:
    """Read a direct value or a private secret file, rejecting ambiguous input."""
    value = os.environ.get(name)
    file_name = os.environ.get(name + "_FILE")
    if value and file_name:
        raise ValueError(f"configure only one of {name} and {name}_FILE")
    if file_name:
        path = Path(file_name)
        try:
            info = path.lstat()
            if path.is_symlink() or not path.is_file() or info.st_mode & 0o077:
                raise ValueError(f"{name}_FILE must be a private regular file")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o077:
                    raise ValueError(f"{name}_FILE must be a private regular file")
                data = os.read(descriptor, 4097)
                if len(data) > 4096:
                    raise ValueError(f"{name}_FILE exceeds 4096 bytes")
                value = data.decode("utf-8").rstrip("\r\n")
            finally:
                os.close(descriptor)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"{name}_FILE could not be read") from exc
    return value if value is not None else default


def _validate_time(value: str, field: str):
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must use HH:MM") from exc
    if len(value) != 5 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{field} must use HH:MM")


def _validate_email(value: str, field: str):
    display, address = parseaddr(value)
    if display or not address or address != value or any(ch in value for ch in "\r\n\0") or "@" not in value:
        raise ValueError(f"{field} must be one plain email address")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must use ASCII because SMTPUTF8 is not enabled") from exc


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
    mail_transport: str = "file"
    mail_host: str = "smtp-sink"
    mail_port: int = 1025
    mail_security: str = "plain"
    mail_username: str | None = None
    mail_password_file: Path | None = None
    mail_sender: str = "watchkeeper@localhost"
    mail_timeout_seconds: int = 15
    bind_host: str = "0.0.0.0"
    bind_port: int = 8765
    allowed_origins: tuple[str, ...] = ()
    proxy_mode: str = "ignore"
    reviewer_broker_socket: Path = Path("/run/watchkeeper/broker/app.sock")
    reviewer_broker_token: str = ""
    state_lock: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, "database", Path(self.database))
        object.__setattr__(self, "email_dir", Path(self.email_dir))
        object.__setattr__(self, "reviewer_broker_socket", Path(self.reviewer_broker_socket))
        object.__setattr__(self, "mail_password_file", Path(self.mail_password_file) if self.mail_password_file else None)
        if self.state_lock is not None:
            object.__setattr__(self, "state_lock", Path(self.state_lock))
        if str(self.database) == ":memory:":
            raise ValueError("database must be persistent file storage")
        if not self.intake_token or not self.reviewer_token or self.intake_token == self.reviewer_token:
            raise ValueError("intake and reviewer tokens must be set and distinct")
        for label, secret in (("intake token", self.intake_token), ("reviewer token", self.reviewer_token)):
            try:
                secret.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(f"{label} must contain ASCII characters only") from exc
            if any(ch in secret for ch in "\r\n\0"):
                raise ValueError(f"{label} contains an invalid character")
        if not self.ui_username or not self.ui_password:
            raise ValueError("UI username and password must be set")
        if len(self.intake_token) > 4096 or len(self.reviewer_token) > 4096 or len(self.ui_username) > 4096 or len(self.ui_password) > 4096:
            raise ValueError("authentication settings must be no larger than 4096 characters")
        if any(ch in self.ui_username for ch in ":\r\n\0"):
            raise ValueError("UI username contains an invalid character")
        if "\r" in self.ui_password or "\n" in self.ui_password or "\0" in self.ui_password:
            raise ValueError("UI password contains an invalid character")
        _validate_email(self.recipient, "recipient")
        _validate_email(self.mail_sender, "mail sender")
        parsed = urlsplit(self.public_base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.path not in {"", "/"} or not _valid_host(parsed.hostname)):
            raise ValueError("public base URL must be an HTTP(S) origin without credentials or path")
        try:
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("public base URL port is invalid")
        except ValueError as exc:
            raise ValueError("public base URL port is invalid") from exc
        try:
            literal = ip_address(parsed.hostname)
            local = literal.is_loopback
        except ValueError:
            local = parsed.hostname.lower() in {"localhost", "watchkeeper", "app"}
        if parsed.scheme != "https" and not local:
            raise ValueError("non-local public base URL must use HTTPS")
        if type(self.bind_port) is not int or not 1 <= self.bind_port <= 65535:
            raise ValueError("bind port must be from 1 through 65535")
        if not self.bind_host or any(ch in self.bind_host for ch in "\r\n\0"):
            raise ValueError("bind host is invalid")
        if type(self.stale_after_seconds) is not int or not 1 <= self.stale_after_seconds <= 31_536_000:
            raise ValueError("stale-after seconds must be from 1 through 31536000")
        if type(self.transcript_retention_days) is not int or not 1 <= self.transcript_retention_days <= 36500:
            raise ValueError("transcript_retention_days must be from 1 through 36500")
        if type(self.inferred_after_days) is not int or not 0 <= self.inferred_after_days <= 365:
            raise ValueError("inferred threshold must be from 0 through 365 days")
        try:
            ZoneInfo(self.schedule_timezone)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError("schedule time zone must be a valid IANA name") from exc
        _validate_time(self.review_time, "review time")
        _validate_time(self.digest_time, "digest time")
        for name, value, low, high in (
            ("review window limit", self.review_window_limit, 1, 500),
            ("review window bytes", self.review_window_bytes, 180000, 220000),
            ("review windows per day", self.review_windows_per_day, 2, 100),
            ("scheduler poll seconds", self.scheduler_poll_seconds, 5, 300),
            ("mail port", self.mail_port, 1, 65535),
            ("mail timeout", self.mail_timeout_seconds, 1, 120),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be from {low} through {high}")
        if self.mail_transport not in {"file", "smtp"}:
            raise ValueError("mail transport must be file or smtp")
        if self.mail_security not in {"plain", "starttls", "ssl"}:
            raise ValueError("mail security must be plain, starttls, or ssl")
        if self.mail_transport == "smtp":
            if not _valid_host(self.mail_host):
                raise ValueError("SMTP host is invalid")
            if self.mail_username and not self.mail_password_file:
                raise ValueError("SMTP username requires a password file")
            if self.mail_password_file and not self.mail_username:
                raise ValueError("SMTP password file requires a username")
            if self.mail_password_file:
                _secret_file(self.mail_password_file, "SMTP password")
            if self.mail_security == "plain" and self.mail_username:
                raise ValueError("SMTP authentication is not allowed over plaintext transport")
            if self.mail_security == "plain":
                try:
                    local_mail_host = ip_address(self.mail_host).is_loopback
                except ValueError:
                    local_mail_host = self.mail_host.lower() in {"localhost", "smtp-sink"}
                if not local_mail_host:
                    raise ValueError("plaintext SMTP is limited to localhost or the synthetic SMTP sink")
        elif self.mail_password_file or self.mail_username:
            raise ValueError("SMTP credentials are only valid with SMTP transport")
        if self.proxy_mode != "ignore":
            raise ValueError("proxy mode must be ignore; forwarded headers never establish identity")
        if not self.reviewer_broker_socket.is_absolute():
            raise ValueError("reviewer socket paths must be absolute")
        if not self.reviewer_broker_token:
            # Reviewer container is optional in the synthetic profile, while
            # the app broker still always has a distinct reviewer token.
            object.__setattr__(self, "reviewer_broker_token", self.reviewer_token)
        if self.reviewer_broker_token != self.reviewer_token:
            raise ValueError("reviewer broker token must match the reviewer role token")
        origins = tuple(self.allowed_origins) or (f"{parsed.scheme}://{parsed.netloc}",)
        for origin in origins:
            part = urlsplit(origin)
            if (part.scheme not in {"http", "https"} or not part.hostname or part.username or part.password or
                    part.path or part.query or part.fragment or not _valid_host(part.hostname)):
                raise ValueError("allowed origins must be HTTP(S) origins")
            try:
                _ = part.port
            except ValueError as exc:
                raise ValueError("allowed origin port is invalid") from exc
        canonical_origin = f"{parsed.scheme}://{parsed.netloc}"
        if canonical_origin not in origins:
            raise ValueError("allowed origins must include the configured public base URL origin")
        object.__setattr__(self, "allowed_origins", origins)
        journal = self.privacy_journal or Path(str(self.database) + ".privacy.jsonl")
        if Path(journal).resolve() == self.database.resolve():
            raise ValueError("privacy journal must be a separate file from the database")
        object.__setattr__(self, "privacy_journal", Path(journal))
        canonical_lock = self.database.resolve().parent / ".watchkeeper.lock"
        if self.state_lock is not None and Path(self.state_lock).resolve() != canonical_lock:
            raise ValueError("state lock path is fixed beside the database")
        object.__setattr__(self, "state_lock", canonical_lock)

    @classmethod
    def from_env(cls) -> "Config":
        if os.environ.get("WATCHKEEPER_TRUSTED_PROXIES"):
            raise ValueError("WATCHKEEPER_TRUSTED_PROXIES is unsupported; forwarded headers are ignored")
        def value(name, default):
            return os.environ.get(name, default)

        def integer(name, default):
            try:
                return int(value(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        intake = _secret("WATCHKEEPER_INTAKE_TOKEN")
        reviewer = _secret("WATCHKEEPER_REVIEWER_TOKEN")
        username = _secret("WATCHKEEPER_UI_USERNAME")
        password = _secret("WATCHKEEPER_UI_PASSWORD")
        missing = [name for name, val in (("WATCHKEEPER_INTAKE_TOKEN", intake),
                    ("WATCHKEEPER_REVIEWER_TOKEN", reviewer), ("WATCHKEEPER_UI_USERNAME", username),
                    ("WATCHKEEPER_UI_PASSWORD", password)) if not val]
        if missing:
            raise ValueError("missing required runtime settings: " + ", ".join(missing))
        raw_origins = value("WATCHKEEPER_ALLOWED_ORIGINS", "")
        return cls(
            database=Path(value("WATCHKEEPER_DATABASE", "/var/lib/watchkeeper/watchkeeper.sqlite3")),
            email_dir=Path(value("WATCHKEEPER_EMAIL_DIR", "/var/lib/watchkeeper/email")),
            intake_token=intake, reviewer_token=reviewer, ui_username=username, ui_password=password,
            recipient=value("WATCHKEEPER_RECIPIENT", "watchkeeper-demo@example.invalid"),
            public_base_url=value("WATCHKEEPER_PUBLIC_BASE_URL", "http://127.0.0.1:8765"),
            stale_after_seconds=integer("WATCHKEEPER_STALE_AFTER_SECONDS", 3600),
            transcript_retention_days=integer("WATCHKEEPER_TRANSCRIPT_RETENTION_DAYS", 30),
            privacy_journal=Path(value("WATCHKEEPER_PRIVACY_JOURNAL", "/var/lib/watchkeeper/privacy.jsonl")),
            inferred_after_days=integer("WATCHKEEPER_INFERRED_AFTER_DAYS", 3),
            schedule_timezone=value("WATCHKEEPER_SCHEDULE_TIMEZONE", "UTC"),
            review_time=value("WATCHKEEPER_REVIEW_TIME", "08:00"),
            digest_time=value("WATCHKEEPER_DIGEST_TIME", "08:15"),
            review_window_limit=integer("WATCHKEEPER_REVIEW_WINDOW_LIMIT", 100),
            review_window_bytes=integer("WATCHKEEPER_REVIEW_WINDOW_BYTES", 180000),
            review_windows_per_day=integer("WATCHKEEPER_REVIEW_WINDOWS_PER_DAY", 4),
            scheduler_poll_seconds=integer("WATCHKEEPER_SCHEDULER_POLL_SECONDS", 30),
            mail_transport=value("WATCHKEEPER_MAIL_TRANSPORT", "file"),
            mail_host=value("WATCHKEEPER_MAIL_HOST", "smtp-sink"),
            mail_port=integer("WATCHKEEPER_MAIL_PORT", 1025),
            mail_security=value("WATCHKEEPER_MAIL_SECURITY", "plain"),
            mail_username=_secret("WATCHKEEPER_MAIL_USERNAME"),
            mail_password_file=Path(value("WATCHKEEPER_MAIL_PASSWORD_FILE", "")) if value("WATCHKEEPER_MAIL_PASSWORD_FILE", "") else None,
            mail_sender=value("WATCHKEEPER_MAIL_SENDER", "watchkeeper@localhost"),
            mail_timeout_seconds=integer("WATCHKEEPER_MAIL_TIMEOUT_SECONDS", 15),
            bind_host=value("WATCHKEEPER_BIND_HOST", "0.0.0.0"),
            bind_port=integer("WATCHKEEPER_PORT", 8765),
            allowed_origins=tuple(part.strip() for part in raw_origins.split(",") if part.strip()),
            proxy_mode=value("WATCHKEEPER_PROXY_MODE", "ignore"),
            reviewer_broker_socket=Path(value("WATCHKEEPER_REVIEWER_BROKER_SOCKET", "/run/watchkeeper/broker/app.sock")),
            reviewer_broker_token=_secret("WATCHKEEPER_REVIEWER_BROKER_TOKEN", reviewer),
            state_lock=None,
        )


def _secret_file(path: Path, label: str):
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_mode & 0o077:
            raise ValueError(f"{label} file must be a private regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o077:
                raise ValueError(f"{label} file must be private")
            data = os.read(descriptor, 4097)
            if len(data) > 4096:
                raise ValueError(f"{label} file exceeds 4096 bytes")
        finally:
            os.close(descriptor)
        if not data.decode("utf-8").rstrip("\r\n"):
            raise ValueError(f"{label} file must not be empty")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} file could not be read") from exc


def _validate_hostnames(hosts):
    dns_name = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z")
    if not hosts or any(not isinstance(host, str) or not dns_name.fullmatch(host.lower().rstrip(".")) for host in hosts):
        raise ValueError("inference hosts must be exact DNS names")
    for host in hosts:
        try:
            ip_address(host)
        except ValueError:
            continue
        raise ValueError("inference hosts must not be IP literals")


def _valid_host(host: str) -> bool:
    if not isinstance(host, str) or not host or any(ch in host for ch in "\r\n/\\ \t\0"):
        return False
    try:
        ip_address(host)
        return True
    except ValueError:
        return bool(re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?", host))
