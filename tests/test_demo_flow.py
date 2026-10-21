import base64
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import default
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from watchkeeper.config import Config
from watchkeeper.db import Database
from watchkeeper.delivery import FileEmailSink
from watchkeeper.http import serve
from watchkeeper.service import WatchkeeperService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class HTTPFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.config = Config(self.root / "state" / "watchkeeper.db", self.root / "email",
                             "intake-secret", "reviewer-secret", "alice", "private-pass")
        self._start()
        fixture = json.loads(Path(__file__).parents[1].joinpath("fixtures/codex-two-thread-demo.json").read_text())
        self.batch = fixture

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()
        self.temp.cleanup()

    def _start(self):
        sink = FileEmailSink(self.config.email_dir, self.config.recipient, self.config.public_base_url)
        service = WatchkeeperService(Database(self.config.database), sink, clock=self.clock, stale_after_seconds=3600)
        app = type("Application", (), {"config": self.config, "service": service})()
        self.server = serve(app, "127.0.0.1", 0)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"
        self.config = Config(self.config.database, self.config.email_dir, self.config.intake_token,
                             self.config.reviewer_token, self.config.ui_username, self.config.ui_password,
                             public_base_url=self.base)
        app.config = self.config
        app.service.email_sink.public_base_url = self.base
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def _stop(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()

    def _request(self, path, *, token=None, basic=None, payload=None, csrf=None, cookie=None, expect_error=False):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        if basic:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        if csrf:
            headers["X-CSRF-Token"] = csrf
            headers["Origin"] = self.base
        if cookie:
            headers["Cookie"] = cookie
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        method = "POST" if data is not None or path == "/api/deliver" else "GET"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                content = response.read()
                is_json = "json" in response.headers.get("Content-Type", "")
                return response.status, json.loads(content) if is_json else content.decode(), response.headers
        except HTTPError as error:
            if expect_error:
                status, body, headers = error.code, error.read().decode(), error.headers
                error.close()
                return status, body, headers
            raise

    def _proposal(self, project_id="garden-guide"):
        view = self._request("/api/review-input", token=self.config.reviewer_token)[1]
        selected = [item for item in view["observations"] if item["source_id"] == self.batch["source_id"]]
        return {"project_id": project_id,
                "outcome": "Publish a garden guide with three tested planting plans",
                "next_action": "Choose three planting zones for the garden guide",
                "reasoning": "The source asks the user to choose the planting zones before testing can finish.",
                "evidence_refs": [item["observation_id"] for item in selected],
                "evidence_revisions": {item["observation_id"]: item["revision"] for item in selected},
                "status": "explicit_request", "grouping_confidence": "strong"}

    def _page_csrf(self):
        _, page, headers = self._request("/", basic=(self.config.ui_username, self.config.ui_password))
        import re
        token = re.search(r"const csrf=\"([^\"]+)\"", page).group(1)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return token, cookie

    def test_real_http_demo_persists_and_sleeps_without_duplicate_delivery(self):
        self.assertEqual(401, self._request("/api/state", expect_error=True)[0])
        self.assertEqual(401, self._request("/api/observations", token=self.config.reviewer_token,
                                            payload=self.batch, expect_error=True)[0])
        self.assertEqual(200, self._request("/api/observations", token=self.config.intake_token, payload=self.batch)[0])
        proposal = self._proposal()
        self.assertEqual(200, self._request("/api/reviews", token=self.config.reviewer_token, payload=proposal)[0])
        self.assertEqual(200, self._request("/api/reviews", token=self.config.reviewer_token, payload=proposal)[0])
        csrf, cookie = self._page_csrf()
        self.assertEqual(1, self._request("/api/deliver", basic=(self.config.ui_username, self.config.ui_password),
                                          csrf=csrf, cookie=cookie)[1]["delivered"])
        self.assertEqual(0, self._request("/api/deliver", basic=(self.config.ui_username, self.config.ui_password),
                                          csrf=csrf, cookie=cookie)[1]["delivered"])
        emails = list(self.config.email_dir.glob("*.eml"))
        self.assertEqual(1, len(emails))
        email_text = message_from_bytes(emails[0].read_bytes(), policy=default).get_content()
        self.assertIn("Choose three planting zones", email_text)
        self.assertIn(self.base + "/", email_text)

        state_before_preview = self._request("/api/state", basic=(self.config.ui_username, self.config.ui_password))[1]
        self._request("/", basic=(self.config.ui_username, self.config.ui_password))
        state_after_preview = self._request("/api/state", basic=(self.config.ui_username, self.config.ui_password))[1]
        self.assertEqual(state_before_preview, state_after_preview)
        self.assertEqual(2, len({e["thread_id"] for e in state_after_preview["projects"][0]["evidence"]}))

        until = (self.clock() + timedelta(days=1)).isoformat()
        self.assertEqual(200, self._request("/api/projects/garden-guide/snooze", basic=(self.config.ui_username, self.config.ui_password),
                                            payload={"until": until}, csrf=csrf, cookie=cookie)[0])
        snoozed_input = self._request("/api/review-input", token=self.config.reviewer_token)[1]
        self.assertFalse(any(item["observation_id"] in proposal["evidence_refs"]
                             for item in snoozed_input["observations"]))
        self._stop()
        self.clock.advance(hours=3)
        self._start()
        cursor_before_sleep = self._request("/api/state", basic=(self.config.ui_username, self.config.ui_password))[1]["sources"][0]["cursor"]
        sleeping = dict(self.batch, cursor="cursor-sleep", available=False)
        self._request("/api/observations", token=self.config.intake_token, payload=sleeping)
        state = self._request("/api/state", basic=(self.config.ui_username, self.config.ui_password))[1]
        self.assertEqual("stale", state["sources"][0]["freshness"])
        self.assertEqual(cursor_before_sleep, state["sources"][0]["cursor"])
        self.assertEqual(until, state["projects"][0]["snooze_until"])

        self.clock.advance(hours=1)
        waking = dict(self.batch, cursor="cursor-wake", available=True)
        self.assertEqual(0, self._request("/api/observations", token=self.config.intake_token, payload=waking)[1]["stored"])
        # Replay the originally captured evidence. A reviewer snapshot excludes
        # snoozed work, so a newly generated proposal would have no references.
        self._request("/api/reviews", token=self.config.reviewer_token, payload=proposal)
        _, csrf_page, _ = self._request("/", basic=(self.config.ui_username, self.config.ui_password))
        csrf2 = __import__("re").search(r"const csrf=\"([^\"]+)\"", csrf_page).group(1)
        result = self._request("/api/deliver", basic=(self.config.ui_username, self.config.ui_password),
                               csrf=csrf2, cookie=f"pw_csrf={csrf2}")[1]
        self.assertEqual(0, result["delivered"])
        self.assertEqual(1, len(list(self.config.email_dir.glob("*.eml"))))
        state = self._request("/api/state", basic=(self.config.ui_username, self.config.ui_password))[1]
        self.assertEqual("fresh", state["sources"][0]["freshness"])

    def test_snooze_cancels_pending_delivery_and_csrf_requires_origin(self):
        self._request("/api/observations", token=self.config.intake_token, payload=self.batch)
        self._request("/api/reviews", token=self.config.reviewer_token, payload=self._proposal())
        csrf, cookie = self._page_csrf()
        self.assertEqual(403, self._wrong_origin_delivery(csrf, cookie))
        until = (self.clock() + timedelta(days=1)).isoformat()
        self._request("/api/projects/garden-guide/snooze", basic=(self.config.ui_username, self.config.ui_password),
                      payload={"until": until}, csrf=csrf, cookie=cookie)
        self.assertEqual(0, self._request("/api/deliver", basic=(self.config.ui_username, self.config.ui_password),
                                          csrf=csrf, cookie=cookie)[1]["delivered"])
        self.assertEqual([], list(self.config.email_dir.glob("*.eml")))

    def _wrong_origin_delivery(self, csrf, cookie):
        headers = {"Authorization": "Basic " + base64.b64encode(b"alice:private-pass").decode(),
                   "Cookie": cookie, "X-CSRF-Token": csrf, "Origin": "http://attacker.invalid"}
        try:
            with urlopen(Request(self.base + "/api/deliver", headers=headers, method="POST"), timeout=3) as response:
                return response.status
        except HTTPError as error:
            status = error.code
            error.close()
            return status


if __name__ == "__main__":
    unittest.main()
