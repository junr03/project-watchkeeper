"""HTTP acceptance for durable user decisions and reviewer precedence."""

import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from watchkeeper.config import Config
from watchkeeper.db import Database, SCHEMA_VERSION
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


class SchemaMigrationTest(unittest.TestCase):
    def test_v1_upgrade_preserves_project_snooze_outbox_and_backfills_shared_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE observations(observation_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,thread_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,revision TEXT NOT NULL,source_timestamp TEXT NOT NULL,captured_at TEXT NOT NULL,
                    locator TEXT NOT NULL,text TEXT NOT NULL,UNIQUE(source_id,thread_id,message_id));
                CREATE TABLE sources(source_id TEXT PRIMARY KEY,cursor TEXT,last_seen_at TEXT,available INTEGER NOT NULL DEFAULT 0,last_error TEXT);
                CREATE TABLE projects(project_id TEXT PRIMARY KEY,outcome TEXT NOT NULL,next_action TEXT NOT NULL,reasoning TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,fingerprint TEXT NOT NULL,disposition TEXT NOT NULL DEFAULT 'open',snooze_until TEXT,updated_at TEXT NOT NULL);
                CREATE TABLE outbox(delivery_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,status TEXT NOT NULL,delivered_at TEXT);
                PRAGMA user_version=1;
            """)
            evidence = [{"observation_id": "obs1", "source_id": "codex/demo", "thread_id": "shared/thread",
                         "message_id": "m1", "revision": "1", "source_timestamp": "2026-10-21T15:00:00Z",
                         "captured_at": "2026-10-21T16:00:00+00:00", "locator": "safe", "text": "synthetic"}]
            connection.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?)",
                               ("kept-project", "Outcome", "Action", "Reason", json.dumps(evidence), "fp", "open",
                                "2026-10-22T16:00:00+00:00", "2026-10-21T16:00:00+00:00"))
            connection.execute("INSERT INTO outbox VALUES(?,?,?,?,?,NULL)",
                               ("delivery-1", "kept-project", "fp", "2026-10-21T16:00:00+00:00", "pending"))
            connection.commit()
            connection.close()
            db = Database(path)
            with db.connect() as c:
                self.assertEqual(SCHEMA_VERSION, c.execute("PRAGMA user_version").fetchone()[0])
                row = c.execute("SELECT disposition,snooze_until FROM projects WHERE project_id='kept-project'").fetchone()
                self.assertEqual("open", row["disposition"])
                self.assertEqual("2026-10-22T16:00:00+00:00", row["snooze_until"])
                self.assertEqual("pending", c.execute("SELECT status FROM outbox WHERE delivery_id='delivery-1'").fetchone()[0])
                self.assertEqual("kept-project", c.execute("SELECT project_id FROM project_threads WHERE thread_id='shared/thread'").fetchone()[0])

    def test_future_schema_rejected_and_failed_v2_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            future = Path(directory) / "future.sqlite"
            c = sqlite3.connect(future)
            c.execute("PRAGMA user_version=99")
            c.close()
            with self.assertRaisesRegex(RuntimeError, "unsupported database schema version"):
                Database(future)

            path = Path(directory) / "rollback.sqlite"
            db = Database(path)
            with db.connect() as c:
                c.execute("INSERT INTO projects(project_id,outcome,next_action,reasoning,evidence_json,fingerprint,updated_at) VALUES(?,?,?,?,?,?,?)",
                          ("broken", "Outcome", "Action", "Reason", "not-json", "fp", "2026-10-21T16:00:00+00:00"))
                c.execute("DROP TABLE project_threads")
                c.execute("PRAGMA user_version=2")
            with self.assertRaises(ValueError):
                Database(path)
            with closing(sqlite3.connect(path)) as c:
                self.assertEqual(2, c.execute("PRAGMA user_version").fetchone()[0])
                self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_threads'").fetchone())


class DecisionsHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.config = Config(self.root / "state" / "watchkeeper.db", self.root / "email",
                             "intake-secret", "reviewer-secret", "alice", "private-pass")
        self.batch = {"source_id": "codex/demo", "cursor": "c1", "available": True,
                      "observations": [
                          {"thread_id": "thread/one?#%", "message_id": "m1", "revision": "1",
                           "source_timestamp": "2026-10-21T15:00:00Z", "locator": "demo locator",
                           "text": "The guide needs a nursery date check."},
                          {"thread_id": "thread-two", "message_id": "m2", "revision": "1",
                           "source_timestamp": "2026-10-21T14:00:00Z", "locator": "second locator",
                           "text": "A second supporting discussion."}]}
        self._start()

    def tearDown(self):
        self._stop()
        self.temp.cleanup()

    def _start(self):
        sink = FileEmailSink(self.config.email_dir, self.config.recipient, self.config.public_base_url)
        service = WatchkeeperService(Database(self.config.database), sink, clock=self.clock)
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
        if getattr(self, "server", None):
            self.server.shutdown()
            self.worker.join(timeout=3)
            self.server.server_close()
            self.server = None

    def _request(self, path, *, token=None, basic=None, payload=None, csrf=None, cookie=None, origin=True,
                 method=None, expect_error=False):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        if basic:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
            if origin:
                headers["Origin"] = self.base
        if cookie:
            headers["Cookie"] = cookie
        data = json.dumps(payload).encode() if payload is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        if method is None:
            method = "POST" if data is not None or path == "/api/deliver" else "GET"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                content = response.read()
                is_json = "json" in response.headers.get("Content-Type", "")
                return response.status, json.loads(content) if is_json else content.decode(), response.headers
        except HTTPError as error:
            if not expect_error:
                raise
            result = (error.code, error.read().decode(), error.headers)
            error.close()
            return result

    def _csrf(self):
        _, page, headers = self._request("/", basic=("alice", "private-pass"))
        token = re.search(r"const csrf=\"([^\"]+)\"", page).group(1)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return token, cookie

    def _proposal(self, project_id="garden-guide"):
        observations = self._request("/api/review-input", token="reviewer-secret")[1]["observations"]
        refs = [item for item in observations if item["source_id"] == self.batch["source_id"]]
        value = {"project_id": project_id, "outcome": "Publish a tested garden guide",
                 "next_action": "Ask a local nursery to confirm planting dates",
                 "reasoning": "The draft is ready, but evidence says the plans are untested.",
                 "evidence_refs": [item["observation_id"] for item in refs],
                 "evidence_revisions": {item["observation_id"]: item["revision"] for item in refs},
                 "status": "explicit_request", "grouping_confidence": "strong"}
        return value

    def _ingest_and_review(self):
        self._request("/api/observations", token="intake-secret", payload=self.batch)
        proposal = self._proposal()
        result = self._request("/api/reviews", token="reviewer-secret", payload=proposal)
        return proposal, result[1]

    def _action(self, project_id, action, payload=None, *, csrf=None, cookie=None, basic=True, origin=True):
        return self._request(f"/api/projects/{quote(project_id, safe='')}/actions/{action}",
                             basic=("alice", "private-pass") if basic else None,
                             csrf=csrf, cookie=cookie, origin=origin, payload=payload or {}, expect_error=True)

    def test_lifecycle_decisions_survive_restart_and_conflicting_proposals(self):
        proposal, result = self._ingest_and_review()
        self.assertTrue(result["delivery_created"])
        csrf, cookie = self._csrf()
        self.assertEqual(401, self._action("garden-guide", "complete", basic=False)[0])
        self.assertEqual(403, self._action("garden-guide", "complete", csrf=csrf, cookie=cookie, origin=False)[0])
        self.assertEqual(403, self._action("garden-guide", "complete", csrf=csrf, cookie="pw_csrf=wrong")[0])
        self.assertEqual(200, self._action("garden-guide", "complete", csrf=csrf, cookie=cookie)[0])
        before = self._request("/api/state", basic=("alice", "private-pass"))[1]
        self._request("/", basic=("alice", "private-pass"))  # link preview is read-only
        self.assertEqual(before, self._request("/api/state", basic=("alice", "private-pass"))[1])
        self._stop()
        self._start()
        revised_batch = {**self.batch, "cursor": "c2", "observations": [
            {**item, "revision": "2", "text": "Fresh synthetic evidence after the original review."}
            for item in self.batch["observations"]]}
        revised = self._request("/api/observations", token="intake-secret", payload=revised_batch)[1]
        self.assertEqual(2, revised["stored"])
        proposal["outcome"] = "Hostile rewrite of a completed project"
        # Reuse the same evidence identities with their current revisions. The
        # service can now exercise lifecycle suppression instead of rejecting
        # this replay solely because the review snapshot is stale.
        proposal["evidence_revisions"] = {ref: "2" for ref in proposal["evidence_refs"]}
        rejected = self._request("/api/reviews", token="reviewer-secret", payload=proposal)[1]
        self.assertEqual("suppressed", rejected["status"])
        state = self._request("/api/state", basic=("alice", "private-pass"))[1]
        project = state["projects"][0]
        self.assertEqual("completed", project["disposition"])
        self.assertEqual("Publish a tested garden guide", project["outcome"])
        self.assertEqual([], self._request("/api/review-input", token="reviewer-secret")[1]["observations"])
        csrf, cookie = self._csrf()
        self.assertEqual(0, self._request("/api/deliver", basic=("alice", "private-pass"), csrf=csrf,
                                          cookie=cookie)[1]["delivered"])
        csrf, cookie = self._csrf()
        self.assertEqual(200, self._action("garden-guide", "reopen", csrf=csrf, cookie=cookie)[0])
        self.assertFalse(self._request("/api/state", basic=("alice", "private-pass"))[1]["projects"][0]["eligible"])
        self._request("/api/reviews", token="reviewer-secret", payload=proposal)
        self.assertEqual("open", self._request("/api/state", basic=("alice", "private-pass"))[1]["projects"][0]["disposition"])

    def test_archive_restore_and_snooze_require_new_assessment(self):
        proposal, _ = self._ingest_and_review()
        csrf, cookie = self._csrf()
        self.assertEqual(200, self._action("garden-guide", "archive", csrf=csrf, cookie=cookie)[0])
        alias_proposal = dict(proposal, project_id="archived-alias")
        alias = self._request("/api/reviews", token="reviewer-secret", payload=alias_proposal)[1]
        self.assertEqual("suppressed", alias["status"])
        state = self._request("/api/state", basic=("alice", "private-pass"))[1]
        self.assertNotIn("archived-alias", {p["project_id"] for p in state["projects"]})
        self.assertNotIn("archived-alias", {p["requested_project_id"] for p in state["proposals"]})
        self.assertNotIn("archived-alias", self._request("/", basic=("alice", "private-pass"))[1])
        self._stop()
        self._start()
        page = self._request("/", basic=("alice", "private-pass"))[1]
        self.assertNotIn("Publish a tested garden guide", page)
        archived_page = self._request("/?view=archived", basic=("alice", "private-pass"))[1]
        self.assertIn("Publish a tested garden guide", archived_page)
        csrf, cookie = self._csrf()
        self.assertEqual(200, self._action("garden-guide", "restore", csrf=csrf, cookie=cookie)[0])
        self.assertFalse(self._request("/api/state", basic=("alice", "private-pass"))[1]["projects"][0]["eligible"])
        self._request("/api/reviews", token="reviewer-secret", payload=self._proposal())

        csrf, cookie = self._csrf()
        until = (self.clock() + timedelta(hours=2)).isoformat()
        self.assertEqual(200, self._action("garden-guide", "snooze", {"until": until}, csrf=csrf, cookie=cookie)[0])
        self.assertEqual([], self._request("/api/review-input", token="reviewer-secret")[1]["observations"])
        self.clock.advance(hours=3)
        waking = dict(self.batch, cursor="c2")
        self._request("/api/observations", token="intake-secret", payload=waking)
        self.assertTrue(self._request("/api/review-input", token="reviewer-secret")[1]["observations"])
        self.assertTrue(self._request("/api/reviews", token="reviewer-secret", payload=self._proposal())[1]["delivery_created"])

    def test_exclusion_and_thread_grouping_are_durable_and_encoded(self):
        self._ingest_and_review()
        state = self._request("/api/state", basic=("alice", "private-pass"))[1]
        self.assertIn("%2F", self._request("/", basic=("alice", "private-pass"))[1])
        csrf, cookie = self._csrf()
        source = quote(self.batch["source_id"], safe="")
        self.assertEqual(200, self._request(f"/api/sources/{source}/exclude", basic=("alice", "private-pass"),
                                            payload={}, csrf=csrf, cookie=cookie)[0])
        self.assertEqual([], self._request("/api/review-input", token="reviewer-secret")[1]["observations"])
        self.assertEqual(0, self._request("/api/observations", token="intake-secret", payload=self.batch)[1]["stored"])
        csrf, cookie = self._csrf()
        self._request(f"/api/sources/{source}/include", basic=("alice", "private-pass"), payload={}, csrf=csrf, cookie=cookie)
        self.assertEqual(200, self._request("/api/observations", token="intake-secret", payload=self.batch)[0])
        thread = quote("thread/one?#%", safe="")
        csrf, cookie = self._csrf()
        thread_url = f"/api/threads/{source}/{thread}"
        self._request(thread_url + "/exclude", basic=("alice", "private-pass"), payload={}, csrf=csrf, cookie=cookie)
        remaining = self._request("/api/review-input", token="reviewer-secret")[1]["observations"]
        self.assertNotIn("thread/one?#%", {item["thread_id"] for item in remaining})
        self.assertIn("Re-enable conversation", self._request("/", basic=("alice", "private-pass"))[1])
        csrf, cookie = self._csrf()
        self._request(thread_url + "/include", basic=("alice", "private-pass"), payload={}, csrf=csrf, cookie=cookie)
        target = self._request("/api/reviews", token="reviewer-secret", payload=self._proposal("second-project"))[1]
        self.assertEqual("attention", target["kind"])  # one thread may support multiple outcomes
        state_after = self._request("/api/state", basic=("alice", "private-pass"))[1]
        self.assertEqual(["garden-guide", "second-project"], [p["project_id"] for p in state_after["projects"]])
        shared = [r for r in state_after["project_threads"] if r["thread_id"] == "thread/one?#%"]
        self.assertEqual({"garden-guide", "second-project"}, {r["project_id"] for r in shared})
        csrf, cookie = self._csrf()
        moved_url = f"/api/threads/{quote('codex/demo', safe='')}/{quote('thread/one?#%', safe='')}/assign"
        result = self._request(moved_url, basic=("alice", "private-pass"), payload={"project_id": "second-project"},
                               csrf=csrf, cookie=cookie)
        self.assertEqual("second-project", result[1]["project_id"])
        original = self._request("/api/reviews", token="reviewer-secret", payload=self._proposal("garden-guide"))[1]
        self.assertEqual("proposal", original["kind"])  # explicit user grouping correction wins
        csrf, cookie = self._csrf()
        split_url = f"/api/threads/{source}/{quote('thread-two', safe='')}/assign"
        split = self._request(split_url, basic=("alice", "private-pass"),
                              payload={"project_id": "split-project", "from_project_id": "second-project"},
                              csrf=csrf, cookie=cookie, expect_error=True)
        self.assertEqual(200, split[0], split[1])
        split = split[1]
        self.assertEqual("split-project", split["project_id"])
        grouped = self._request("/api/state", basic=("alice", "private-pass"))[1]
        self.assertIn({"source_id": "codex/demo", "thread_id": "thread-two", "project_id": "split-project"},
                      grouped["project_threads"])
        self.assertIn({"source_id": "codex/demo", "thread_id": "thread-two", "project_id": "split-project"},
                      grouped["thread_ownership"])

        self._stop()
        self._start()
        grouped = self._request("/api/state", basic=("alice", "private-pass"))[1]
        self.assertIn({"source_id": "codex/demo", "thread_id": "thread/one?#%", "project_id": "second-project"},
                      grouped["project_threads"])
        self.assertIn({"source_id": "codex/demo", "thread_id": "thread/one?#%", "project_id": "second-project"},
                      grouped["thread_ownership"])
        self.assertIn({"source_id": "codex/demo", "thread_id": "thread-two", "project_id": "split-project"},
                      grouped["project_threads"])

    def test_abandonment_and_exclusion_survive_restart_and_hostile_proposals(self):
        proposal, result = self._ingest_and_review()
        self.assertTrue(result["delivery_created"])
        csrf, cookie = self._csrf()
        self.assertEqual(200, self._action("garden-guide", "abandon", csrf=csrf, cookie=cookie)[0])
        csrf, cookie = self._csrf()
        self.assertEqual(200, self._action("garden-guide", "archive", csrf=csrf, cookie=cookie)[0])

        # Exercise an exclusion against a genuinely new message identity while
        # its durable source exclusion is active.
        excluded_batch = {"source_id": "private-source", "cursor": "p1", "available": True,
                          "observations": [{"thread_id": "hidden", "message_id": "new-after-exclusion",
                              "revision": "1", "source_timestamp": "2026-10-21T15:30:00Z",
                              "locator": "synthetic", "text": "Synthetic excluded content."}]}
        csrf, cookie = self._csrf()
        self._request("/api/sources/private-source/exclude", basic=("alice", "private-pass"),
                      payload={}, csrf=csrf, cookie=cookie)
        self._request("/api/observations", token="intake-secret", payload=excluded_batch)

        self._stop()
        self._start()
        proposal["outcome"] = "Hostile rewrite of an abandoned project"
        self._request("/api/reviews", token="reviewer-secret", payload=proposal)
        state = self._request("/api/state", basic=("alice", "private-pass"))[1]
        project = next(p for p in state["projects"] if p["project_id"] == "garden-guide")
        self.assertEqual("abandoned", project["disposition"])
        self.assertTrue(project["archived"])
        self.assertEqual("Publish a tested garden guide", project["outcome"])
        self.assertNotIn("new-after-exclusion", {o["message_id"] for o in
                         self._request("/api/review-input", token="reviewer-secret")[1]["observations"]})

        # An exclusion on one conversation also blocks later message IDs after
        # restart, independently of whether the adapter has already filtered it.
        thread_url = f"/api/threads/{quote('codex/demo', safe='')}/{quote('thread-two', safe='')}"
        csrf, cookie = self._csrf()
        self._request(thread_url + "/exclude", basic=("alice", "private-pass"), payload={}, csrf=csrf, cookie=cookie)
        thread_batch = {"source_id": "codex/demo", "cursor": "c3", "available": True,
                        "observations": [{"thread_id": "thread-two", "message_id": "new-thread-message",
                            "revision": "1", "source_timestamp": "2026-10-21T15:30:00Z",
                            "locator": "synthetic", "text": "Synthetic excluded conversation content."}]}
        self._request("/api/observations", token="intake-secret", payload=thread_batch)
        self._stop()
        self._start()
        self.assertNotIn("new-thread-message", {o["message_id"] for o in
                         self._request("/api/review-input", token="reviewer-secret")[1]["observations"]})
if __name__ == "__main__":
    unittest.main()
