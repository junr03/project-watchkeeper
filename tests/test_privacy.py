"""Acceptance for transcript expiry, deletion impact, and privacy-journal replay."""

from datetime import datetime, timedelta, timezone
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import re
import sqlite3
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from watchkeeper.config import Config
from watchkeeper.db import Database
from watchkeeper.delivery import FileEmailSink
from watchkeeper.http import serve
from watchkeeper.privacy import PrivacyManager
from watchkeeper.service import WatchkeeperService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class PrivacyAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.db_path = self.root / "state.sqlite"
        self.journal = self.root / "privacy.jsonl"
        self.email_dir = self.root / "email"
        self.service = self._service()

    def tearDown(self):
        self.temp.cleanup()

    def _service(self):
        return WatchkeeperService(Database(self.db_path), FileEmailSink(self.email_dir,
            "watchkeeper-demo@example.invalid"), clock=self.clock, transcript_retention_days=1,
            privacy_journal=self.journal, captured_email_dir=self.email_dir)

    def _ingest(self, messages, cursor="c1"):
        threads = sorted({message[0] for message in messages})
        return self.service.ingest({"source_id": "codex/demo", "cursor": cursor, "available": True,
            "observations": [{"thread_id": thread, "message_id": message, "revision": revision,
                "source_timestamp": timestamp, "locator": "synthetic locator", "text": text}
                for thread, message, revision, timestamp, text in messages],
            "coverage": {"state": "complete", "threads_listed": len(threads), "threads_read": len(threads),
                "messages_read": len(messages), "scan_limit": 500, "archived_scanned": True,
                "message_completeness": "full", "current_activity": "unknown", "gaps": [],
                "source_version": "synthetic", "failure": "none"},
            "thread_coverage": [{"thread_id": thread, "completeness": "complete", "activity": "unknown"}
                                for thread in threads]})

    def _review(self, project_id, refs):
        return self.service.review({"project_id": project_id, "outcome": f"Outcome for {project_id}",
            "next_action": f"Next action for {project_id}", "reasoning": f"Reason for {project_id}",
            "evidence_refs": refs, "evidence_revisions": {ref: "1" for ref in refs},
            "status": "explicit_request", "grouping_confidence": "strong"})

    def _refs(self, thread=None):
        observations = self.service.review_input()["observations"]
        return [item["observation_id"] for item in observations
                if thread is None or item["thread_id"] == thread]

    def test_expiry_suppresses_same_revision_but_recaptures_new_revision(self):
        self._ingest([("t-open", "m1", "1", "2026-10-21T15:00:00Z", "keep while unresolved")])
        self._review("open-item", self._refs())
        self.service.decide("open-item", "archive")
        self.clock.advance(days=2)
        self.assertEqual(1, self.service.expire_retention()["expired"])
        state = self.service.state()
        self.assertEqual("keep while unresolved", state["projects"][0]["evidence"][0]["text"])
        self.assertEqual(1, self._ingest([("t-open", "m1", "1", "2026-10-21T15:00:00Z", "replayed old text")], "c2")["expired_replays_suppressed"])
        # Exercise the intake boundary directly: the transaction itself must
        # fence a race where service-level prefiltering was already bypassed.
        direct = {"source_id": "codex/demo", "cursor": "c2b", "available": True,
            "observations": [{"thread_id": "t-open", "message_id": "m1", "revision": "1",
                "source_timestamp": "2026-10-21T15:00:00Z", "locator": "synthetic locator", "text": "raced replay"}]}
        direct_result = self.service.intake.ingest(direct)
        self.assertEqual(1, direct_result["expired_replays_suppressed"])
        self.assertEqual("c2b", direct_result["cursor"])
        self.assertEqual(1, self._ingest([("t-open", "m1", "2", "2026-10-21T16:00:00Z", "new revision")], "c3")["stored"])
        with self.service.db.connect() as c:
            self.assertEqual("new revision", c.execute("SELECT text FROM observations WHERE thread_id='t-open'").fetchone()[0])

    def test_closed_expired_snapshot_is_removed_on_later_maintenance_but_archived_open_kept(self):
        self._ingest([("t-closed", "m1", "1", "2026-10-21T15:00:00Z", "closed text"),
                      ("t-archived", "m2", "1", "2026-10-21T15:00:00Z", "archived text")])
        refs_by_thread = {item["thread_id"]: item["observation_id"]
                          for item in self.service.review_input()["observations"]}
        self._review("closed", [refs_by_thread["t-closed"]])
        self._review("archived", [refs_by_thread["t-archived"]])
        self.service.decide("archived", "archive")
        self.clock.advance(days=2)
        self.service.expire_retention()
        self.service.decide("closed", "complete")
        before = {p["project_id"]: p for p in self.service.state()["projects"]}
        self.assertEqual("closed text", before["closed"]["evidence"][0]["text"])
        self.service.expire_retention()
        projects = {p["project_id"]: p for p in self.service.state()["projects"]}
        self.assertEqual([], projects["closed"]["evidence"])
        self.assertEqual("archived text", projects["archived"]["evidence"][0]["text"])

    def test_delete_only_scrubs_directly_shared_projects_and_replays_from_older_database(self):
        messages = [("t1", "m1", "1", "2026-10-21T15:00:00Z", "thread one secret"),
                    ("t2", "m2", "1", "2026-10-21T15:01:00Z", "thread two stays")]
        self._ingest(messages)
        refs = self._refs()
        by_thread = {item["thread_id"]: item["observation_id"] for item in self.service.review_input()["observations"]}
        self._review("A", [by_thread["t1"]])
        self._review("B", [by_thread["t1"], by_thread["t2"]])
        self._review("C", [by_thread["t2"]])
        snapshot = self.root / "older.sqlite"
        with self.service.db.connect() as c:
            c.execute("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(self.db_path, snapshot)

        preview = self.service.deletion_preview("A")
        self.assertEqual(["B"], preview["affected_projects"])
        self.service.delete_project("A", preview["impact_token"], confirmed=True)
        state = {p["project_id"]: p for p in self.service.state()["projects"]}
        self.assertNotIn("A", state)
        self.assertEqual("open", state["B"]["disposition"])
        self.assertIn("summary removed", state["B"]["outcome"])
        self.assertEqual(["t2"], [e["thread_id"] for e in state["B"]["evidence"]])
        self.assertEqual("Outcome for C", state["C"]["outcome"])
        with self.service.db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM observations WHERE thread_id='t1'").fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM expired_observations WHERE thread_id='t1'").fetchone())

        # Restore a pre-deletion backup: the external journal must replay deletion.
        self.service = None
        shutil.copy2(snapshot, self.db_path)
        self.service = self._service()
        state = {p["project_id"]: p for p in self.service.state()["projects"]}
        self.assertNotIn("A", state)
        self.assertIn("summary removed", state["B"]["outcome"])
        self.assertEqual("Outcome for C", state["C"]["outcome"])
        self.assertEqual(1, self._ingest([("t1", "m3", "1", "2026-10-21T15:02:00Z", "excluded new message")], "c2")["excluded_observations_skipped"])

    def test_include_event_replays_after_delete_and_authorizes_recapture(self):
        self._ingest([("t1", "m1", "1", "2026-10-21T15:00:00Z", "secret")])
        self._review("A", self._refs())
        snapshot = self.root / "before-delete.sqlite"
        with self.service.db.connect() as c:
            c.execute("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(self.db_path, snapshot)
        preview = self.service.deletion_preview("A")
        self.service.delete_project("A", preview["impact_token"], confirmed=True)
        self.service.exclude_thread("codex/demo", "t1", False)
        self.service = None
        shutil.copy2(snapshot, self.db_path)
        self.service = self._service()
        result = self._ingest([("t1", "m1", "1", "2026-10-21T15:00:00Z", "secret"),
                               ("t1", "m2", "1", "2026-10-21T15:00:00Z", "new message")], "c2")
        self.assertEqual(0, result["expired_replays_suppressed"])
        self.assertEqual(2, result["stored"])

    def test_restore_only_capture_ids_are_journaled_before_delete_and_retried_after_crash(self):
        self._ingest([("t1", "m1", "1", "2026-10-21T15:00:00Z", "synthetic")])
        self._review("A", self._refs())
        # This older restore contains an outbox ID that was absent from the
        # later database snapshot used to create the deletion event.
        with self.service.db.connect() as c:
            c.execute("DELETE FROM outbox")
            c.execute("PRAGMA wal_checkpoint(FULL)")
        snapshot = self.root / "restore-only.sqlite"
        shutil.copy2(self.db_path, snapshot)
        with sqlite3.connect(snapshot) as c:
            c.execute("INSERT INTO outbox VALUES(?,?,?,?,?,NULL)",
                      ("restore-only", "A", "restore-fingerprint", "2026-10-21T16:00:00+00:00", "delivered"))
        with sqlite3.connect(snapshot) as c:
            c.execute("PRAGMA wal_checkpoint(FULL)")
        preview = self.service.deletion_preview("A")
        self.service.delete_project("A", preview["impact_token"], confirmed=True)
        journal_events = [json.loads(line) for line in self.journal.read_text().splitlines()]
        deletion = next(item for item in journal_events if item["type"] == "delete_project")
        self.assertEqual([], deletion["delivery_ids"])

        self.service = None
        for suffix in ("-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        shutil.copy2(snapshot, self.db_path)
        (self.email_dir / "restore-only.eml").parent.mkdir(parents=True, exist_ok=True)
        (self.email_dir / "restore-only.eml").write_text("synthetic captured message")
        (self.email_dir / "aggregate-capture.eml").write_text("synthetic aggregate message")
        manager = PrivacyManager(Database(self.db_path), self.journal, self.email_dir, 1, self.clock,
                                 purge_hooks=[lambda _c, **_kwargs: ["aggregate-capture"]])
        manager._purge_capture_files = lambda _ids: None
        manager.replay()  # DB delete commits; simulate a crash before file unlink.
        self.assertTrue((self.email_dir / "restore-only.eml").exists())
        self.assertTrue((self.email_dir / "aggregate-capture.eml").exists())
        events = [json.loads(line) for line in self.journal.read_text().splitlines()]
        cleanup = [item for item in events if item["type"] == "purge_capture_files"]
        self.assertEqual({"restore-only", "aggregate-capture"},
                         {value for item in cleanup for value in item["delivery_ids"]})

        PrivacyManager(Database(self.db_path), self.journal, self.email_dir, 1, self.clock).replay()
        self.assertFalse((self.email_dir / "restore-only.eml").exists())
        self.assertFalse((self.email_dir / "aggregate-capture.eml").exists())

    def test_journal_cursor_ahead_of_file_fails_closed(self):
        with self.service.db.connect() as c:
            c.execute("UPDATE privacy_journal_state SET last_seq=1 WHERE singleton_id=1")
        with self.assertRaisesRegex(RuntimeError, "privacy journal is missing"):
            self.service.privacy.replay()

    def test_concurrent_exclusion_and_replay_use_consistent_lock_order(self):
        from threading import Barrier
        barrier = Barrier(2)
        manager = self.service.privacy
        def exclude():
            barrier.wait(timeout=2)
            manager.set_thread_excluded("codex/demo", "thread-race", True)
        def replay():
            barrier.wait(timeout=2)
            manager.replay()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(exclude)
            second = pool.submit(replay)
            first.result(timeout=4)
            second.result(timeout=4)
        with self.service.db.connect() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM excluded_threads WHERE source_id=? AND thread_id=?",
                                           ("codex/demo", "thread-race")).fetchone())

    def test_retention_configuration_defaults_to_thirty_days_and_rejects_invalid_values(self):
        config = Config(self.root / "default.sqlite", self.root / "email", "intake", "reviewer", "alice", "pw")
        self.assertEqual(30, config.transcript_retention_days)
        with self.assertRaisesRegex(ValueError, "transcript_retention_days"):
            Config(self.root / "invalid.sqlite", self.root / "email", "intake", "reviewer", "alice", "pw",
                   transcript_retention_days=0)


class PrivacyHTTPAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.config = Config(self.root / "state.sqlite", self.root / "email", "intake", "reviewer",
                             "alice", "password")
        self._start()
        self.batch = {"source_id": "codex/demo", "cursor": "c1", "available": True,
            "observations": [{"thread_id": "delete-thread", "message_id": "m1", "revision": "1",
                "source_timestamp": "2026-10-21T15:00:00Z", "locator": "synthetic", "text": "private synthetic text"}]}
        self._request("/api/observations", token="intake", payload=self.batch)
        observation = self._request("/api/review-input", token="reviewer")[1]["observations"][0]
        proposal = {"project_id": "delete-me", "outcome": "Synthetic outcome", "next_action": "Check one fact",
                    "reasoning": "Synthetic reason", "evidence_refs": [observation["observation_id"]],
                    "evidence_revisions": {observation["observation_id"]: observation["revision"]},
                    "status": "explicit_request", "grouping_confidence": "strong"}
        self._request("/api/reviews", token="reviewer", payload=proposal)

    def tearDown(self):
        self._stop()
        self.temp.cleanup()

    def _start(self):
        sink = FileEmailSink(self.config.email_dir, self.config.recipient, self.config.public_base_url)
        service = WatchkeeperService(Database(self.config.database), sink, clock=self.clock,
            privacy_journal=self.config.privacy_journal, captured_email_dir=self.config.email_dir)
        self.app = type("Application", (), {"config": self.config, "service": service})()
        self.server = serve(self.app, "127.0.0.1", 0)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"
        self.config = Config(self.config.database, self.config.email_dir, self.config.intake_token,
            self.config.reviewer_token, self.config.ui_username, self.config.ui_password,
            public_base_url=self.base, privacy_journal=self.config.privacy_journal)
        self.app.config = self.config
        self.app.service.email_sink.public_base_url = self.base
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def _stop(self):
        if getattr(self, "server", None):
            self.server.shutdown()
            self.worker.join(timeout=3)
            self.server.server_close()
            self.server = None

    def _request(self, path, *, token=None, basic=False, payload=None, method=None,
                 csrf=None, cookie=None, origin=True, expect_error=False):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        if basic:
            headers["Authorization"] = "Basic " + base64.b64encode(b"alice:password").decode()
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
            if origin:
                headers["Origin"] = self.base
        if cookie:
            headers["Cookie"] = cookie
        body = json.dumps(payload).encode() if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=body, headers=headers, method=method or ("POST" if body is not None else "GET"))
        try:
            with urlopen(request, timeout=3) as response:
                content = response.read()
                content_type = response.headers.get("Content-Type", "")
                return response.status, json.loads(content) if "json" in content_type else content.decode(), response.headers
        except HTTPError as error:
            result = (error.code, error.read().decode(), error.headers)
            error.close()
            if not expect_error:
                raise
            return result

    def _csrf(self):
        _, page, headers = self._request("/", basic=True)
        return re.search(r"const csrf=\"([^\"]+)\"", page).group(1), headers["Set-Cookie"].split(";", 1)[0]

    def test_authenticated_preview_stale_token_confirmation_and_post_delete_boundaries(self):
        project_path = "/api/projects/delete-me/deletion-preview"
        self.assertEqual(401, self._request(project_path, expect_error=True)[0])
        self.assertEqual(401, self._request(project_path, token="reviewer", expect_error=True)[0])
        before = self._request("/api/state", basic=True)[1]
        preview = self._request(project_path, basic=True)[1]
        self.assertEqual([], preview["affected_projects"])
        self.assertEqual(before, self._request("/api/state", basic=True)[1])

        # Add a shared association after preview; the original confirmation is stale.
        observation = self._request("/api/review-input", token="reviewer")[1]["observations"][0]
        self._request("/api/reviews", token="reviewer", payload={"project_id": "shared", "outcome": "Shared outcome",
            "next_action": "Shared action", "reasoning": "Shared reason",
            "evidence_refs": [observation["observation_id"]],
                    "evidence_revisions": {observation["observation_id"]: observation["revision"]},
                    "status": "explicit_request", "grouping_confidence": "strong"})
        csrf, cookie = self._csrf()
        delete_path = "/api/projects/delete-me/delete"
        payload = {"confirm": True, "impact_token": preview["impact_token"]}
        self.assertEqual(403, self._request(delete_path, basic=True, payload=payload,
            csrf=csrf, cookie="pw_csrf=wrong", expect_error=True)[0])
        self.assertEqual(403, self._request(delete_path, basic=True, payload=payload,
            csrf=csrf, cookie=cookie, origin=False, expect_error=True)[0])
        self.assertEqual(409, self._request(delete_path, basic=True, payload=payload,
            csrf=csrf, cookie=cookie, expect_error=True)[0])

        fresh = self._request(project_path, basic=True)[1]
        self.assertEqual(["shared"], fresh["affected_projects"])
        status, _result, _headers = self._request(delete_path, basic=True,
            payload={"confirm": True, "impact_token": fresh["impact_token"]}, csrf=csrf, cookie=cookie)
        self.assertEqual(200, status)
        self.assertEqual(["shared"], [p["project_id"] for p in self._request("/api/state", basic=True)[1]["projects"]])
        self.assertNotIn("private synthetic text", self._request("/", basic=True)[1])
        self.assertEqual([], self._request("/api/review-input", token="reviewer")[1]["observations"])
        self.assertEqual(0, self._request("/api/deliver", basic=True, payload={}, csrf=csrf, cookie=cookie)[1]["delivered"])
        result = self._request("/api/observations", token="intake", payload={**self.batch, "cursor": "c2"})[1]
        self.assertEqual(1, result["excluded_observations_skipped"])

        self._stop()
        self._start()
        self.assertEqual(["shared"], [p["project_id"] for p in self._request("/api/state", basic=True)[1]["projects"]])
        self.assertEqual([], self._request("/api/review-input", token="reviewer")[1]["observations"])


if __name__ == "__main__":
    unittest.main()
