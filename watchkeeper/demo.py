"""Run the synthetic two-thread demonstration through the real HTTP service."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import threading
from urllib.request import Request, urlopen

from .config import Config
from .db import Database
from .delivery import FileEmailSink
from .http import serve
from .service import WatchkeeperService


class DemoClock:
    def __init__(self):
        self.value = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def call(base, path, token=None, body=None, basic=None, cookie=None, csrf=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if basic:
        raw = f"{basic[0]}:{basic[1]}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded = json.dumps(body).encode()
    else:
        encoded = None
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
        headers["Origin"] = base
    request = Request(base + path, data=encoded, headers=headers, method="POST" if encoded is not None or path == "/api/deliver" else "GET")
    with urlopen(request, timeout=3) as response:
        content = response.read()
        return response.status, json.loads(content) if content and "json" in response.headers.get("Content-Type", "") else content.decode(), response.headers


def make_app(config, clock):
    sink = FileEmailSink(config.email_dir, config.recipient, config.public_base_url)
    service = WatchkeeperService(Database(config.database), sink, clock=clock, stale_after_seconds=3600)
    return type("Application", (), {"config": config, "service": service})()


def run(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    config = Config(data_dir / "watchkeeper.sqlite3", data_dir / "email", "demo-intake-token",
                   "demo-reviewer-token", "demo", "demo-password")
    clock = DemoClock()
    app = make_app(config, clock)
    server = serve(app, "127.0.0.1", 0)
    host, port = server.server_address
    base = f"http://{host}:{port}"
    config = Config(config.database, config.email_dir, config.intake_token, config.reviewer_token,
                    config.ui_username, config.ui_password, public_base_url=base)
    app.config = config
    app.service.email_sink.public_base_url = base
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        batch = json.loads(Path(__file__).resolve().parent.parent.joinpath("fixtures/codex-two-thread-demo.json").read_text())
        _, intake, _ = call(base, "/api/observations", token=config.intake_token, body=batch)
        _, reviewer_view, _ = call(base, "/api/review-input", token=config.reviewer_token)
        selected = [o for o in reviewer_view["observations"] if o["source_id"] == batch["source_id"]]
        refs = [o["observation_id"] for o in selected]
        proposal = {"project_id": "garden-guide", "outcome": "Publish a garden guide with three tested planting plans",
                    "next_action": "Ask a local nursery to confirm the planting dates",
                    "reasoning": "The outline is drafted, but source evidence says the planting plans have not been tested.",
                    "evidence_refs": refs}
        call(base, "/api/reviews", token=config.reviewer_token, body=proposal)
        _, page, _ = call(base, "/", basic=(config.ui_username, config.ui_password))
        csrf = re.search(r"const csrf=\"([^\"]+)\"", page).group(1)
        call(base, "/api/reviews", token=config.reviewer_token, body=proposal)
        _, result, _ = call(base, "/api/deliver", basic=(config.ui_username, config.ui_password), cookie=f"pw_csrf={csrf}", csrf=csrf)
        assert result["delivered"] == 1
        _, state, _ = call(base, "/api/state", basic=(config.ui_username, config.ui_password))
        project = state["projects"][0]
        until = (clock() + timedelta(days=1)).isoformat()
        call(base, f"/api/projects/{project['project_id']}/snooze", basic=(config.ui_username, config.ui_password),
             cookie=f"pw_csrf={csrf}", csrf=csrf, body={"until": until})
        before_preview = call(base, "/api/state", basic=(config.ui_username, config.ui_password))[1]
        call(base, "/", basic=(config.ui_username, config.ui_password))
        after_preview = call(base, "/api/state", basic=(config.ui_username, config.ui_password))[1]
        assert before_preview == after_preview

        server.shutdown()
        worker.join(timeout=2)
        server.server_close()
        clock.advance(hours=3)
        app = make_app(config, clock)
        server = serve(app, "127.0.0.1", port)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        sleeping = dict(batch, cursor="demo-cursor-sleep", available=False)
        call(base, "/api/observations", token=config.intake_token, body=sleeping)
        _, sleeping_state, _ = call(base, "/api/state", basic=(config.ui_username, config.ui_password))
        assert sleeping_state["sources"][0]["freshness"] == "stale"
        clock.advance(hours=1)
        waking = dict(batch, cursor="demo-cursor-wake", available=True)
        _, wake_result, _ = call(base, "/api/observations", token=config.intake_token, body=waking)
        view = call(base, "/api/review-input", token=config.reviewer_token)[1]
        proposal["evidence_refs"] = [o["observation_id"] for o in view["observations"]]
        call(base, "/api/reviews", token=config.reviewer_token, body=proposal)
        _, after_wake_delivery, _ = call(base, "/api/deliver", basic=(config.ui_username, config.ui_password),
                                          cookie=f"pw_csrf={csrf}", csrf=csrf)
        _, final, _ = call(base, "/api/state", basic=(config.ui_username, config.ui_password))
        email_count = len(list(config.email_dir.glob("*.eml")))
        print(json.dumps({"intake": intake, "first_delivery_count": result["delivered"],
                          "project": final["projects"][0], "sleep_freshness": sleeping_state["sources"][0]["freshness"],
                          "wake_stored_revisions": wake_result["stored"], "after_wake_delivery_count": after_wake_delivery["delivered"],
                          "emails_after_wake": email_count,
                          "email_dir": str(config.email_dir)}, indent=2))
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("watchkeeper-demo-data"))
    args = parser.parse_args()
    run(args.data_dir)


if __name__ == "__main__":
    main()
