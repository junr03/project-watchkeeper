"""Combined retention, scheduler, digest, and privacy replay acceptance."""

from datetime import datetime, timedelta, timezone
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from watchkeeper.db import Database
from watchkeeper.delivery import FileEmailSink
from watchkeeper.service import WatchkeeperService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class PrivacyRecoveryTest(unittest.TestCase):
    source_id = "codex:recovery-test"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "watchkeeper.sqlite"
        self.journal = self.root / "privacy.jsonl"
        self.email_dir = self.root / "mail"
        self.clock = Clock()
        self.service = self._new_service()

    def tearDown(self):
        self.temp.cleanup()

    def _new_service(self, db_path=None):
        return WatchkeeperService(
            Database(db_path or self.db_path),
            FileEmailSink(self.email_dir, "recovery-test@example.invalid"),
            clock=self.clock, transcript_retention_days=1,
            privacy_journal=self.journal, captured_email_dir=self.email_dir,
            schedule_timezone="UTC", review_time="00:00", digest_time="00:00",
            review_window_limit=20, review_windows_per_day=10,
        )

    @staticmethod
    def coverage():
        return {"state": "complete", "threads_listed": 1, "threads_read": 1,
                "messages_read": 1, "scan_limit": 100, "archived_scanned": True,
                "message_completeness": "full", "current_activity": "unknown",
                "gaps": [], "source_version": "synthetic", "failure": "none"}

    def _ingest(self, messages, cursor, *, threads=None):
        thread_ids = sorted(set(threads or [row[0] for row in messages]))
        return self.service.ingest({
            "source_id": self.source_id, "cursor": cursor, "available": True,
            "coverage": self.coverage(),
            "thread_coverage": [{"thread_id": thread, "completeness": "complete", "activity": "unknown"}
                                for thread in thread_ids],
            "observations": [{"thread_id": thread, "message_id": message, "revision": revision,
                              "source_timestamp": source_time, "locator": f"{thread}#{message}", "text": text}
                             for thread, message, revision, source_time, text in messages],
        })

    def _observation_id(self, thread, message):
        return hashlib.sha256(f"{self.source_id}\0{thread}\0{message}".encode()).hexdigest()

    def _proposal(self, project_id, refs, *, outcome=None):
        with self.service.db.connect() as c:
            revisions = {row["observation_id"]: row["revision"] for row in c.execute(
                "SELECT observation_id,revision FROM reviewable_observations WHERE observation_id IN (%s)" %
                ",".join("?" for _ in refs), refs)}
        return {"project_id": project_id,
                "outcome": outcome or f"Synthetic recovery finding {project_id}",
                "next_action": "Review the synthetic recovery plan",
                "reasoning": "A controlled acceptance fixture uses public synthetic evidence.",
                "evidence_refs": list(refs), "evidence_revisions": revisions,
                "status": "explicit_request", "grouping_confidence": "strong"}

    def _review(self, project_id, refs, *, outcome=None):
        return self.service.review(self._proposal(project_id, refs, outcome=outcome))

    def _state_project(self, project_id):
        return next(item for item in self.service.state()["projects"] if item["project_id"] == project_id)

    def test_retained_evidence_is_leased_reviewed_and_delivered_after_persistent_snooze(self):
        source_time = (self.clock() - timedelta(minutes=5)).isoformat()
        self._ingest([("snoozed-thread", "m1", "1", source_time, "Synthetic retained progress")], "c1")
        oid = self._observation_id("snoozed-thread", "m1")
        self._review("snoozed-project", [oid])
        self.service.decide("snoozed-project", "snooze", until=(self.clock() + timedelta(days=3)).isoformat())

        self.clock.advance(days=2)
        self.service = self._new_service()
        self.assertEqual("open", self._state_project("snoozed-project")["disposition"])
        self.assertIsNotNone(self._state_project("snoozed-project")["snooze_until"])
        self.assertEqual([], self.service.review_input()["observations"])
        with self.service.db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM observations WHERE observation_id=?", (oid,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM reviewable_observations WHERE observation_id=? AND is_retained=1",
                                           (oid,)).fetchone())

        self.clock.advance(days=2)
        self._ingest([], "c2", threads=["snoozed-thread"])
        leased = self.service.claim_review_job()
        self.assertIsNotNone(leased)
        self.assertIn(oid, {row["observation_id"] for row in leased["review_input"]["observations"]})
        proposal = self._proposal("snoozed-project", [oid], outcome="Synthetic retained finding after snooze")
        result = self.service.complete_review_job(leased["run_id"], leased["lease_token"], [proposal])
        self.assertEqual("success", result["status"])
        self.assertTrue(result["proposals"][0]["eligible"])

        tick = self.service.scheduled_tick()
        self.assertEqual(1, tick["emails"])
        captures = list(self.email_dir.glob("*.eml"))
        self.assertEqual(1, len(captures))
        body = BytesParser(policy=email_policy.default).parsebytes(captures[0].read_bytes()).get_content()
        self.assertIn("Synthetic retained finding after snooze", body)

    def test_watermark_blocks_unreviewed_expired_newer_context_but_not_older_citation_with_context_present(self):
        old_at = (self.clock() - timedelta(minutes=10)).isoformat()
        self._ingest([("watermark-thread", "old-message", "1", old_at, "Synthetic old retained context")], "c1")
        old_id = self._observation_id("watermark-thread", "old-message")
        self._review("watermark-project", [old_id])

        self.clock.advance(days=2)
        self.service.expire_retention()
        newer_at = self.clock().isoformat()
        self._ingest([("watermark-thread", "new-message", "1", newer_at, "Synthetic newer current context")], "c2")
        accepted = self._review("watermark-project", [old_id], outcome="Older citation with current context present")
        self.assertTrue(accepted["eligible"])

        self.clock.advance(days=2)
        self.service.expire_retention()
        self._ingest([], "c3", threads=["watermark-thread"])
        blocked = self._review("watermark-project", [old_id], outcome="Old citation after newer context expired")
        self.assertFalse(blocked["eligible"])
        self.assertEqual("newer_progress_not_reviewed", blocked["eligibility_reason"])
        with self.service.db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM observations WHERE observation_id=?",
                                        (self._observation_id("watermark-thread", "new-message"),)).fetchone())
            self.assertEqual(1, c.execute("SELECT COUNT(*) FROM reviewable_observations WHERE observation_id=?",
                                          (old_id,)).fetchone()[0])
            self.assertEqual("1", c.execute("SELECT revision FROM thread_progress_watermarks WHERE source_id=? AND thread_id=?",
                                             (self.source_id, "watermark-thread")).fetchone()[0])

    def test_delete_purges_accepted_aggregate_and_attempts_invalidates_lease_but_preserves_unrelated_disposition(self):
        now = self.clock().isoformat()
        self._ingest([("delete-thread", "m1", "1", now, "Synthetic delete target text"),
                      ("unrelated-thread", "m2", "1", now, "Synthetic unrelated text")], "c1")
        a_id = self._observation_id("delete-thread", "m1")
        u_id = self._observation_id("unrelated-thread", "m2")
        self._review("delete-target", [a_id])
        self._review("unrelated", [u_id])
        delivered = self.service.deliver_pending()
        self.assertEqual(1, delivered["emails"])
        with self.service.db.connect() as c:
            digest = c.execute("SELECT digest_id FROM delivery_batches WHERE status='accepted'").fetchone()[0]
            deliveries = {row["project_id"]: row["delivery_id"] for row in c.execute(
                "SELECT project_id,delivery_id FROM outbox")}
            self.assertEqual(2, c.execute("SELECT COUNT(*) FROM outbox_attempts WHERE result='accepted'").fetchone()[0])
        capture = self.email_dir / f"{digest}.eml"
        self.assertTrue(capture.is_file())

        lease = self.service.claim_review_job()
        self.assertIsNotNone(lease)
        self.assertIn(a_id, {row["observation_id"] for row in lease["review_input"]["observations"]})
        self.service.decide("unrelated", "abandon")
        preview = self.service.deletion_preview("delete-target")
        self.assertEqual(1, preview["captured_emails"])
        self.service.delete_project("delete-target", preview["impact_token"], confirmed=True)

        self.assertFalse(capture.exists())
        self.assertEqual("abandoned", self._state_project("unrelated")["disposition"])
        self.assertEqual("invalidated", next(run["status"] for run in self.service.state()["monitoring"]["review_runs"]
                                              if run["run_id"] == lease["run_id"]))
        with self.service.db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM outbox WHERE delivery_id=?", (deliveries["delete-target"],)).fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM outbox_attempts WHERE delivery_id=?",
                                        (deliveries["delete-target"],)).fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM project_assessment_guards WHERE project_id='delete-target'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM project_assessment_guards WHERE project_id='unrelated'").fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM delivery_batches WHERE digest_id=?", (digest,)).fetchone())

    def test_restore_predelete_backup_replays_scheduler_purge_before_service_is_returned(self):
        now = self.clock().isoformat()
        self._ingest([("restore-thread", "m1", "1", now, "Synthetic restore secret")], "c1")
        oid = self._observation_id("restore-thread", "m1")
        self._review("restore-target", [oid])
        lease = self.service.claim_review_job()
        self.assertIsNotNone(lease)
        self.assertIn(oid, {row["observation_id"] for row in lease["review_input"]["observations"]})

        snapshot = self.root / "predelete.sqlite"
        with self.service.db.connect() as c:
            c.execute("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(self.db_path, snapshot)

        preview = self.service.deletion_preview("restore-target")
        self.service.delete_project("restore-target", preview["impact_token"], confirmed=True)
        self.service = None
        shutil.copy2(snapshot, self.db_path)
        self.service = self._new_service()

        self.assertNotIn("restore-target", {p["project_id"] for p in self.service.state()["projects"]})
        with self.service.db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM observations WHERE observation_id=?", (oid,)).fetchone())
            run = c.execute("SELECT status,snapshot_json,lease_token FROM monitoring_runs WHERE run_id=?",
                            (lease["run_id"],)).fetchone()
            self.assertEqual("invalidated", run["status"])
            self.assertIsNone(run["snapshot_json"])
            self.assertIsNone(run["lease_token"])
        stale_proposal = {"project_id": "revived-by-stale-lease", "outcome": "Do not restore deleted content",
                          "next_action": "Review the synthetic recovery plan", "reasoning": "stale leased input",
                          "evidence_refs": [oid], "evidence_revisions": {oid: "1"},
                          "status": "explicit_request", "grouping_confidence": "strong"}
        stale = self.service.complete_review_job(lease["run_id"], lease["lease_token"], [stale_proposal])
        self.assertEqual("stale", stale["status"])
        self.assertNotIn("revived-by-stale-lease", {p["project_id"] for p in self.service.state()["projects"]})
        self.assertNotIn("restore secret", json.dumps(self.service.state()))

    def test_scheduled_tick_runs_retention_maintenance(self):
        self._ingest([("maintenance-thread", "m1", "1", self.clock().isoformat(), "Synthetic expired text")], "c1")
        oid = self._observation_id("maintenance-thread", "m1")
        with self.service.db.connect() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM observations WHERE observation_id=?", (oid,)).fetchone())
        self.clock.advance(days=2)
        self.service.scheduled_tick()
        with self.service.db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM observations WHERE observation_id=?", (oid,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM expired_observations WHERE observation_id=?", (oid,)).fetchone())

    def test_archived_shared_project_does_not_block_existing_peer_but_suppresses_new_alias(self):
        self._ingest([("shared-thread", "m1", "1", self.clock().isoformat(), "Synthetic shared evidence")], "c1")
        oid = self._observation_id("shared-thread", "m1")
        self.assertTrue(self._review("shared-A", [oid])["eligible"])
        self.assertTrue(self._review("shared-B", [oid])["eligible"])

        self.service.decide("shared-A", "archive")
        existing = self._review("shared-B", [oid], outcome="Fresh assessment for existing peer")
        self.assertTrue(existing["eligible"])
        alias = self._review("shared-C", [oid], outcome="New alias of archived project")
        self.assertEqual("suppressed", alias["status"])
        self.assertFalse(alias["eligible"])
        self.assertNotIn("shared-C", {project["project_id"] for project in self.service.state()["projects"]})


if __name__ == "__main__":
    unittest.main()
