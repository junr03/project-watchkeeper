"""Fixed-recipient local email sink with a durable outbox."""

from email.message import EmailMessage
from pathlib import Path
import hashlib
import json
import os
import re


class FileEmailSink:
    def __init__(self, directory: Path | str, recipient: str, public_base_url: str = "http://127.0.0.1:8765"):
        self.directory = Path(directory)
        self.recipient = recipient
        self.public_base_url = public_base_url.rstrip("/")

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
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(message.as_bytes())
        tmp.replace(path)
        path.chmod(0o600)
        return path


def fingerprint(project: dict) -> str:
    body = {key: project[key] for key in ("project_id", "outcome", "next_action", "evidence_refs")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
