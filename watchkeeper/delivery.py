"""Fixed-recipient local email sink with a durable outbox."""

from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
import hashlib
import json
import os
from email import policy
import re
import smtplib
import ssl
import socket


class DeliveryNotAccepted(Exception):
    """Transport proved that a message was not accepted and may be retried."""


class FileEmailSink:
    idempotent_acceptance = True
    def __init__(self, directory: Path | str, recipient: str, public_base_url: str = "http://127.0.0.1:8765"):
        self.directory = Path(directory)
        self.recipient = recipient
        self.public_base_url = public_base_url.rstrip("/")

    def has_delivery(self, delivery_id: str) -> bool:
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", delivery_id)
        return (self.directory / f"{safe_id}.eml").is_file()

    def deliver(self, delivery_id: str, project: dict) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", delivery_id)
        path = self.directory / f"{safe_id}.eml"
        if path.exists():
            return path
        message = EmailMessage()
        message["To"] = self.recipient
        message["From"] = "watchkeeper@localhost"
        message["Subject"] = "Watchkeeper attention item"
        message["X-Watchkeeper-Delivery-ID"] = delivery_id
        message.set_content(
            f"Outcome: {project['outcome']}\n\nNext action: {project['next_action']}\n\n"
            f"Reason: {project['reasoning']}\n\nControl page (read-only until you submit an authenticated action): {self.public_base_url}/\n\nEvidence:\n" +
            "\n".join(f"- {e['source_id']} / {e['thread_id']} / {e['locator']}" for e in project["evidence"])
        )
        self._durable_capture(path, message.as_bytes())
        return path

    def deliver_digest(self, delivery_id: str, projects: list[dict], notices: list[dict] | None = None) -> Path:
        """Capture one daily message containing all eligible findings."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", delivery_id)
        path = self.directory / f"{safe_id}.eml"
        if path.exists():
            return path
        message = build_digest_message(delivery_id, projects, notices, recipient=self.recipient,
                                       sender="watchkeeper@localhost", public_base_url=self.public_base_url)
        self._durable_capture(path, message.as_bytes())
        return path

    @staticmethod
    def _durable_capture(path: Path, body: bytes):
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def build_digest_message(delivery_id: str, projects: list[dict], notices: list[dict] | None,
                         *, recipient: str, sender: str, public_base_url: str) -> EmailMessage:
    """Render the fixed-recipient digest shared by file and SMTP transports."""
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = "Watchkeeper daily attention digest"
    message["Date"] = formatdate(localtime=False)
    message["X-Watchkeeper-Delivery-ID"] = delivery_id
    sections = []
    for item in projects:
        evidence = "\n".join(f"- {e['source_id']} / {e['thread_id']} / {e['locator']}"
                              for e in item["evidence"])
        sections.append(f"Outcome: {item['outcome']}\n\nNext action: {item['next_action']}\n\n"
                        f"Reason: {item['reasoning']}\n\nEvidence:\n{evidence}")
    if notices:
        notice_text = "\n".join(f"- {item['category']} {item['subject']}: {item['failure_kind']} "
                                 f"({item['occurrences']} failed attempts)" for item in notices)
        sections.append("Collection and review notices\n\n" + notice_text)
    message.set_content(("\n\n---\n\n".join(sections) if sections else "No new attention findings.") +
                        f"\n\nControl page: {public_base_url.rstrip('/')}/\n")
    return message


class _AcceptanceAwareSMTP(smtplib.SMTP):
    """SMTP DATA implementation that distinguishes refusal from ambiguity."""

    def __init__(self, host="", port=0, local_hostname=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                 source_address=None, *, ssl_context=None):
        self._watchkeeper_ssl_context = ssl_context
        super().__init__(host, port, local_hostname, timeout, source_address)

    def _get_socket(self, host, port, timeout):
        sock = super()._get_socket(host, port, timeout)
        if self._watchkeeper_ssl_context is not None:
            return self._watchkeeper_ssl_context.wrap_socket(sock, server_hostname=self._host)
        return sock

    def data(self, msg):
        self.putcmd("data")
        code, reply = self.getreply()
        if code != 354:
            raise smtplib.SMTPDataError(code, reply)
        self.watchkeeper_data_started = True
        if isinstance(msg, str):
            msg = smtplib._fix_eols(msg).encode("ascii")
        q = smtplib._quote_periods(msg)
        if q[-2:] != b"\r\n":
            q += b"\r\n"
        self.send(q + b".\r\n")
        code, reply = self.getreply()
        self.watchkeeper_data_started = False
        if code != 250:
            raise smtplib.SMTPDataError(code, reply)
        return code, reply


class SMTPEmailSink:
    """Fixed-recipient SMTP transport with explicit TLS and honest ambiguity."""

    idempotent_acceptance = False

    def __init__(self, host: str, port: int, recipient: str, sender: str, public_base_url: str,
                 *, security: str = "starttls", username: str | None = None,
                 password_file: Path | str | None = None, timeout: int = 15):
        if security not in {"plain", "starttls", "ssl"}:
            raise ValueError("SMTP security must be plain, starttls, or ssl")
        self.host, self.port = host, port
        self.recipient, self.sender = recipient, sender
        self.public_base_url = public_base_url.rstrip("/")
        self.security, self.username = security, username
        self.password_file = Path(password_file) if password_file else None
        self.timeout = max(1, min(120, int(timeout)))
        if self.username and not self.password_file:
            raise ValueError("SMTP username requires a password file")

    def deliver_digest(self, delivery_id: str, projects: list[dict], notices: list[dict] | None = None):
        message = build_digest_message(delivery_id, projects, notices, recipient=self.recipient,
                                       sender=self.sender, public_base_url=self.public_base_url)
        smtp = None
        try:
            context = ssl.create_default_context()
            if self.security == "ssl":
                smtp = _AcceptanceAwareSMTP(self.host, self.port, timeout=self.timeout, ssl_context=context)
            else:
                smtp = _AcceptanceAwareSMTP(self.host, self.port, timeout=self.timeout)
            code, reply = smtp.ehlo()
            if code >= 400:
                raise smtplib.SMTPHeloError(code, reply)
            if self.security == "starttls":
                smtp.starttls(context=context)
                code, reply = smtp.ehlo()
                if code >= 400:
                    raise smtplib.SMTPHeloError(code, reply)
            if self.username:
                password = self.password_file.read_text(encoding="utf-8").rstrip("\r\n")
                smtp.login(self.username, password)
            code, reply = smtp.mail(self.sender)
            if code >= 400:
                raise smtplib.SMTPSenderRefused(code, reply, self.sender)
            code, reply = smtp.rcpt(self.recipient)
            if code >= 400:
                raise smtplib.SMTPRecipientsRefused({self.recipient: (code, reply)})
            try:
                smtp.data(message.as_bytes(policy=policy.SMTP))
            except smtplib.SMTPDataError:
                raise
            return {"accepted": True}
        except smtplib.SMTPDataError as exc:
            # A final explicit server rejection proves it did not accept DATA.
            raise DeliveryNotAccepted("SMTP server explicitly rejected the message") from exc
        except (smtplib.SMTPException, OSError, socket.timeout) as exc:
            data_started = bool(getattr(smtp, "watchkeeper_data_started", False)) if smtp else False
            if not data_started:
                raise DeliveryNotAccepted("SMTP transport proved the message was not accepted") from exc
            # Once DATA may have reached the server, retrying could duplicate mail.
            raise RuntimeError("SMTP acceptance is ambiguous") from exc
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except (smtplib.SMTPException, OSError):
                    try:
                        smtp.close()
                    except Exception:
                        pass


def fingerprint(project: dict) -> str:
    # Evidence grows as a conversation is revisited. A changed reference set
    # does not make the same user action a new reminder.
    body = {key: project[key] for key in ("project_id", "outcome", "next_action")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
