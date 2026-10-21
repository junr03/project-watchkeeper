"""Fixed-recipient local email sink with a durable outbox."""

from email.message import EmailMessage
from pathlib import Path
import hashlib
import json
import os
import re


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
        message = EmailMessage()
        message["To"] = self.recipient
        message["From"] = "watchkeeper@localhost"
        message["Subject"] = "Watchkeeper daily attention digest"
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
                            f"\n\nControl page: {self.public_base_url}/\n")
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


def fingerprint(project: dict) -> str:
    # Evidence grows as a conversation is revisited. A changed reference set
    # does not make the same user action a new reminder.
    body = {key: project[key] for key in ("project_id", "outcome", "next_action")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
