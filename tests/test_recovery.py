import json
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from watchkeeper.db import Database
from watchkeeper.delivery import DeliveryNotAccepted, FileEmailSink
from watchkeeper.policy import validate_proposal
from watchkeeper.service import WatchkeeperService


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **parts):
        self.value += timedelta(**parts)


def coverage(failure="none"):
    return {"state": "complete", "threads_listed": 1, "threads_read": 1,
            "messages_read": 1, "scan_limit": 100, "archived_scanned": True,
            "message_completeness": "full", "current_activity": "unknown",
            "gaps": [], "source_version": "fixture-1", "failure": failure}


class FakeMail:
    idempotent_acceptance = False

    def __init__(self):
        self.calls = 0

    def deliver_digest(self, delivery_id, projects, notices):
        self.calls += 1
        raise OSError("simulated ambiguous acceptance")


class RetryableMail:
    idempotent_acceptance = True

    def __init__(self):
        self.calls = []

    def deliver_digest(self, delivery_id, projects, notices):
        self.calls.append((delivery_id, [item["project_id"] for item in projects]))
        if len(self.calls) == 1:
            raise OSError("simulated process interruption after stable claim")


class DefiniteRejectMail:
    idempotent_acceptance = False

    def __init__(self):
        self.calls = []

    def deliver_digest(self, delivery_id, projects, notices):
        self.calls.append(delivery_id)
        if len(self.calls) == 1:
            raise DeliveryNotAccepted("connection refused before acceptance")


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.clock = Clock(datetime(2026, 10, 21, 10, tzinfo=timezone.utc))
        self.mail_dir = root / "mail"
        self.service = WatchkeeperService(
            Database(root / "watchkeeper.sqlite"),
            FileEmailSink(self.mail_dir, "demo@example.invalid"),
            clock=self.clock, schedule_timezone="UTC", review_time="00:00", digest_time="00:00",
            review_window_limit=2, review_windows_per_day=4)

    def tearDown(self):
        self.temp.cleanup()

    def batch(self, items, *, failure="none", cursor="cursor-1"):
        return {"source_id": "codex:test-install", "cursor": cursor, "available": True,
                "coverage": coverage(failure),
                "thread_coverage": [{"thread_id": "thread-a", "completeness": "complete",
                                     "activity": "unknown"}],
                "observations": items}

    def observations(self, count=5):
        return [{"thread_id": "thread-a", "message_id": f"message-{i}", "revision": "1",
                 "source_timestamp": (self.clock() - timedelta(days=5, minutes=i)).isoformat(),
                 "locator": f"thread-a#{i}", "text": f"Public synthetic progress {i}"}
                for i in range(count)]

    def _finish_empty(self, job):
        return self.service.complete_review_job(job["run_id"], job["lease_token"], [])

    def test_bounded_windows_resume_and_eventually_reach_oldest_evidence(self):
        self.service.ingest(self.batch(self.observations()))
        windows = []
        while True:
            job = self.service.claim_review_job()
            if job is None:
                break
            rows = job["review_input"]["observations"]
            windows.append(rows)
            self.assertLessEqual(len(rows), 2)
            if rows:
                self.assertFalse(job["review_input"]["sources"]["codex:test-install"]["threads"]["thread-a"]["coverage_complete"])
            self.assertEqual("success", self._finish_empty(job)["status"])
        self.assertEqual([2, 2, 2, 1], [len(rows) for rows in windows])
        self.assertEqual({f"message-{i}" for i in range(5)},
                         {row["message_id"] for window in windows for row in window})
        with self.service.db.connect() as c:
            self.assertIsNone(c.execute("SELECT cursor_json FROM review_progress WHERE stream='history'").fetchone()[0])

    def test_revision_change_and_expired_lease_reject_job_results(self):
        item = self.observations(1)[0]
        self.service.ingest(self.batch([item]))
        job = self.service.claim_review_job()
        revised = dict(item, revision="2", text="Synthetic revised evidence")
        self.service.ingest(self.batch([revised], cursor="cursor-2"))
        result = self._finish_empty(job)
        self.assertEqual("stale", result["status"])

        self.service.ingest(self.batch([item], cursor="cursor-3"))
        job = self.service.claim_review_job()
        self.clock.advance(seconds=301)
        self.assertEqual("stale", self._finish_empty(job)["status"])

    def test_same_second_new_thread_evidence_invalidates_partial_job(self):
        items = self.observations(3)
        self.service.ingest(self.batch(items))
        job = self.service.claim_review_job()
        self.assertEqual(2, len(job["review_input"]["observations"]))
        extra = dict(items[0], message_id="same-second-new", locator="thread-a#same-second-new")
        self.service.ingest(self.batch([extra], cursor="cursor-same-second"))
        self.assertEqual("stale", self._finish_empty(job)["status"])

    def test_byte_limited_windows_converge_large_observation_backlog(self):
        root = Path(self.temp.name)
        service = WatchkeeperService(Database(root / "byte-bounded.sqlite"),
            FileEmailSink(root / "byte-mail", "demo@example.invalid"), clock=self.clock,
            schedule_timezone="UTC", review_time="00:00", digest_time="00:00",
            review_window_limit=10, review_window_bytes=180_000, review_windows_per_day=10)
        items = self.observations(5)
        for index, item in enumerate(items):
            item["text"] = "Synthetic long content " + (chr(1) * 19_900)
        service.ingest(self.batch(items))
        seen = set()
        windows = 0
        while True:
            job = service.claim_review_job()
            if job is None:
                break
            payload = job["review_input"]
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertLessEqual(len(encoded), 180_000)
            self.assertGreaterEqual(len(payload["observations"]), 1)
            seen.update(item["message_id"] for item in payload["observations"])
            windows += 1
            self.assertEqual("success", service.complete_review_job(job["run_id"], job["lease_token"], []) ["status"])
        self.assertEqual({f"message-{i}" for i in range(5)}, seen)
        self.assertGreater(windows, 1)

    def test_rejected_final_seed_remains_for_next_byte_bounded_window(self):
        root = Path(self.temp.name)
        service = WatchkeeperService(Database(root / "final-seed.sqlite"),
            FileEmailSink(root / "final-seed-mail", "demo@example.invalid"), clock=self.clock,
            schedule_timezone="UTC", review_time="00:00", digest_time="00:00",
            review_window_limit=10, review_window_bytes=180_000, review_windows_per_day=5)
        items = self.observations(2)
        for item in items:
            item["text"] = chr(1) * 19_900
        service.ingest(self.batch(items))
        first = service.claim_review_job()
        self.assertEqual(1, len(first["review_input"]["observations"]))
        self.assertTrue(first["review_input"]["coverage"]["backlog_remaining"])
        self.assertEqual("success", service.complete_review_job(first["run_id"], first["lease_token"], [])["status"])
        second = service.claim_review_job()
        self.assertEqual(1, len(second["review_input"]["observations"]))
        self.assertTrue(second["review_input"]["coverage"]["backlog_remaining"])
        self.assertEqual("success", service.complete_review_job(second["run_id"], second["lease_token"], [])["status"])
        third = service.claim_review_job()
        self.assertEqual(1, len(third["review_input"]["observations"]))
        self.assertNotEqual(first["review_input"]["observations"][0]["message_id"],
                            third["review_input"]["observations"][0]["message_id"])
        self.assertEqual("success", service.complete_review_job(third["run_id"], third["lease_token"], [])["status"])
        self.assertFalse(third["review_input"]["coverage"]["backlog_remaining"])

    def test_review_windows_order_source_instants_across_offsets(self):
        earlier_utc = {"thread_id": "thread-a", "message_id": "earlier", "revision": "1",
                       "source_timestamp": "2026-10-21T09:30:00+00:00", "locator": "thread-a#earlier",
                       "text": "Synthetic earlier instant"}
        later_offset = {"thread_id": "thread-a", "message_id": "later", "revision": "1",
                        "source_timestamp": "2026-10-21T08:00:00-02:00", "locator": "thread-a#later",
                        "text": "Synthetic later instant"}
        self.service.ingest(self.batch([earlier_utc, later_offset]))
        review_input = self.service.review_input(limit=2)
        self.assertEqual(["later", "earlier"], [row["message_id"] for row in review_input["observations"]])

    def test_interleaved_small_thread_keeps_complete_context_when_it_fits(self):
        root = Path(self.temp.name)
        service = WatchkeeperService(Database(root / "interleaved.sqlite"),
            FileEmailSink(root / "interleaved-mail", "demo@example.invalid"), clock=self.clock,
            schedule_timezone="UTC", review_time="00:00", digest_time="00:00",
        review_window_limit=4, review_window_bytes=180_000)
        items = [
            {"thread_id": "thread-a", "message_id": "a-new", "revision": "1", "source_timestamp": "2026-10-21T09:59:00Z", "locator": "a#new", "text": "A newest"},
            {"thread_id": "thread-x", "message_id": "x-one", "revision": "1", "source_timestamp": "2026-10-21T09:58:00Z", "locator": "x#one", "text": "X progress"},
            {"thread_id": "thread-y", "message_id": "y-one", "revision": "1", "source_timestamp": "2026-10-21T09:57:00Z", "locator": "y#one", "text": "Y progress"},
            {"thread_id": "thread-a", "message_id": "a-old", "revision": "1", "source_timestamp": "2026-10-21T09:56:00Z", "locator": "a#old", "text": "A older"},
            {"thread_id": "thread-z", "message_id": "z-one", "revision": "1", "source_timestamp": "2026-10-21T09:55:00Z", "locator": "z#one", "text": "Z progress"},
        ]
        batch = self.batch(items)
        batch["thread_coverage"] = [{"thread_id": thread, "completeness": "complete", "activity": "unknown"}
                                    for thread in ("thread-a", "thread-x", "thread-y", "thread-z")]
        service.ingest(batch)
        job = service.claim_review_job()
        a_rows = [row for row in job["review_input"]["observations"] if row["thread_id"] == "thread-a"]
        self.assertEqual({"a-new", "a-old"}, {row["message_id"] for row in a_rows})
        self.assertTrue(job["review_input"]["sources"]["codex:test-install"]["threads"]["thread-a"]["coverage_complete"])

    def _proposal(self, project_id, refs, service=None):
        target = service or self.service
        with target.db.connect() as c:
            rows = c.execute("SELECT observation_id,revision FROM observations ORDER BY message_id").fetchall()
        revisions = {row["observation_id"]: row["revision"] for row in rows if row["observation_id"] in refs}
        return {"project_id": project_id, "outcome": f"Synthetic outcome {project_id}",
                "next_action": "Review the synthetic plan", "reasoning": "A fabricated test case.",
                "evidence_refs": refs, "evidence_revisions": revisions,
                "status": "explicit_request", "grouping_confidence": "strong"}

    def test_one_daily_digest_aggregates_findings_and_fingerprint_ignores_evidence_growth(self):
        items = self.observations(2)
        self.service.ingest(self.batch(items))
        with self.service.db.connect() as c:
            refs = [row[0] for row in c.execute("SELECT observation_id FROM observations ORDER BY message_id")]
        first = self.service.review(self._proposal("synthetic-a", refs[:1]))
        second = self.service.review(self._proposal("synthetic-b", refs[1:]))
        self.assertTrue(first["delivery_created"] and second["delivery_created"])
        result = self.service.scheduled_tick()
        self.assertEqual({"delivered": 2, "emails": 1, "notices": 0}, result)
        emails = list(self.mail_dir.glob("*.eml"))
        self.assertEqual(1, len(emails))
        body = BytesParser(policy=policy.default).parsebytes(emails[0].read_bytes()).get_content()
        self.assertIn("Synthetic outcome synthetic-a", body)
        self.assertIn("Synthetic outcome synthetic-b", body)

        # Adding another reference to an unchanged finding is not a new reminder.
        later = dict(items[0], message_id="message-later", revision="1",
                     source_timestamp=(self.clock() - timedelta(days=4)).isoformat(),
                     locator="thread-a#later")
        self.service.ingest(self.batch([later], cursor="cursor-2"))
        with self.service.db.connect() as c:
            extended = [row[0] for row in c.execute("SELECT observation_id FROM observations ORDER BY message_id")]
        proposal = self._proposal("synthetic-a", [extended[0], extended[-1]])
        self.assertFalse(self.service.review(proposal)["delivery_created"])
        self.assertEqual({"delivered": 0, "emails": 0, "notices": 0}, self.service.scheduled_tick())

    def test_ambiguous_acceptance_is_never_retried_automatically(self):
        root = Path(self.temp.name)
        service = WatchkeeperService(Database(root / "ambiguous.sqlite"), FakeMail(), clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        service.ingest(self.batch(self.observations(1)))
        with service.db.connect() as c:
            row = c.execute("SELECT observation_id FROM observations").fetchone()
        proposal = self._proposal("ambiguous-project", [row[0]], service=service)
        service.review(proposal)
        self.assertEqual("ambiguous", service.deliver_pending()["status"])
        self.assertEqual(0, service.deliver_pending()["emails"])
        self.assertEqual(1, service.email_sink.calls)
        with service.db.connect() as c:
            self.assertEqual("ambiguous", c.execute("SELECT status FROM outbox").fetchone()[0])

    def test_definite_transport_rejection_retries_same_batch_after_backoff(self):
        root = Path(self.temp.name)
        mail = DefiniteRejectMail()
        service = WatchkeeperService(Database(root / "definite.sqlite"), mail, clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        service.ingest(self.batch(self.observations(1)))
        with service.db.connect() as c:
            ref = c.execute("SELECT observation_id FROM observations").fetchone()[0]
        service.review(self._proposal("definite", [ref], service=service))
        self.assertEqual("retryable", service.deliver_pending()["status"])
        self.assertEqual("retry_deferred", service.deliver_pending()["status"])
        self.clock.advance(seconds=60)
        self.assertEqual(1, service.deliver_pending()["delivered"])
        self.assertEqual(mail.calls[0], mail.calls[1])

    def test_changed_finding_cancels_old_retry_before_new_finding_sends(self):
        root = Path(self.temp.name)
        mail = DefiniteRejectMail()
        service = WatchkeeperService(Database(root / "changed-retry.sqlite"), mail, clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        service.ingest(self.batch(self.observations(1)))
        with service.db.connect() as c:
            ref = c.execute("SELECT observation_id FROM observations").fetchone()[0]
        old = self._proposal("changed-retry", [ref], service=service)
        service.review(old)
        self.assertEqual("retryable", service.deliver_pending()["status"])
        changed = dict(old, next_action="Choose a different synthetic plan")
        self.assertTrue(service.review(changed)["delivery_created"])

        self.clock.advance(seconds=60)
        self.assertEqual("cancelled_stale", service.deliver_pending()["status"])
        self.assertEqual(1, len(mail.calls))
        self.assertEqual(1, service.deliver_pending()["delivered"])
        self.assertEqual(2, len(mail.calls))
        with service.db.connect() as c:
            statuses = [row[0] for row in c.execute("SELECT status FROM outbox ORDER BY created_at,delivery_id")]
        self.assertCountEqual(["cancelled", "delivered"], statuses)

    def test_idempotent_retry_reuses_persisted_digest_membership(self):
        root = Path(self.temp.name)
        mail = RetryableMail()
        service = WatchkeeperService(Database(root / "retry.sqlite"), mail, clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        service.ingest(self.batch(self.observations(2)))
        with service.db.connect() as c:
            refs = [row[0] for row in c.execute("SELECT observation_id FROM observations ORDER BY message_id")]
        service.review(self._proposal("retry-a", refs[:1], service=service))
        service.review(self._proposal("retry-b", refs[1:], service=service))
        self.assertEqual("retryable", service.deliver_pending()["status"])
        # A new finding after the crash must not change the already-claimed batch.
        with service.db.connect() as c:
            self.assertEqual(2, c.execute("SELECT COUNT(*) FROM outbox WHERE status='sending'").fetchone()[0])
        self.assertEqual("retry_deferred", service.deliver_pending()["status"])
        self.clock.advance(seconds=60)
        self.assertEqual(2, service.deliver_pending()["delivered"])
        self.assertEqual(mail.calls[0], mail.calls[1])

    def test_accepted_local_capture_is_recorded_after_lifecycle_change_without_resend(self):
        root = Path(self.temp.name)
        sink = FileEmailSink(root / "accepted-mail", "demo@example.invalid")
        original = sink.deliver_digest
        calls = []

        def accept_then_interrupt(delivery_id, projects, notices):
            calls.append(delivery_id)
            original(delivery_id, projects, notices)
            raise OSError("simulated crash after durable acceptance")

        sink.deliver_digest = accept_then_interrupt
        service = WatchkeeperService(Database(root / "accepted.sqlite"), sink, clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        service.ingest(self.batch(self.observations(1)))
        with service.db.connect() as c:
            ref = c.execute("SELECT observation_id FROM observations").fetchone()[0]
        service.review(self._proposal("accepted", [ref], service=service))
        self.assertEqual("retryable", service.deliver_pending()["status"])
        service.decide("accepted", "archive")
        self.clock.advance(seconds=60)
        result = service.deliver_pending()
        self.assertEqual("previously_accepted", result["status"])
        self.assertEqual(0, result["emails"])
        self.assertEqual(1, len(calls))
        with service.db.connect() as c:
            self.assertEqual("delivered", c.execute("SELECT status FROM outbox").fetchone()[0])

    def test_notice_only_ambiguous_digest_is_not_retried(self):
        root = Path(self.temp.name)
        service = WatchkeeperService(Database(root / "notice-only.sqlite"), FakeMail(), clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        service.scheduler.record_failure("source", "codex:test-install", "quota", "a1")
        service.scheduler.record_failure("source", "codex:test-install", "quota", "a2")
        self.assertEqual("ambiguous", service.deliver_pending()["status"])
        self.assertEqual({"delivered": 0, "emails": 0, "notices": 0}, service.deliver_pending())
        self.assertEqual(1, service.email_sink.calls)
        with service.db.connect() as c:
            self.assertEqual("ambiguous", c.execute("SELECT status FROM delivery_batches").fetchone()[0])

    def test_newer_thread_evidence_cancels_persisted_digest_before_transport(self):
        root = Path(self.temp.name)
        mail = RetryableMail()
        service = WatchkeeperService(Database(root / "stale-batch.sqlite"), mail, clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        item = self.observations(1)[0]
        service.ingest(self.batch([item]))
        with service.db.connect() as c:
            ref = c.execute("SELECT observation_id FROM observations").fetchone()[0]
        service.review(self._proposal("stale-batch", [ref], service=service))
        self.assertEqual("retryable", service.deliver_pending()["status"])
        latest = dict(item, message_id="newer", locator="thread-a#newer",
                      source_timestamp=(self.clock() - timedelta(days=1)).isoformat())
        service.ingest(self.batch([latest], cursor="newer-progress"))
        self.clock.advance(seconds=60)
        self.assertEqual("cancelled_stale", service.deliver_pending()["status"])
        self.assertEqual(1, len(mail.calls))

    def test_privacy_purge_releases_unrelated_member_of_aggregate_batch(self):
        root = Path(self.temp.name)
        mail = RetryableMail()
        service = WatchkeeperService(Database(root / "purge-batch.sqlite"), mail, clock=self.clock,
                                     schedule_timezone="UTC", review_time="00:00", digest_time="00:00")
        items = self.observations(2)
        items[1]["thread_id"] = "thread-b"
        items[1]["locator"] = "thread-b#1"
        service.ingest(self.batch(items))
        with service.db.connect() as c:
            refs = {row["thread_id"]: row["observation_id"] for row in c.execute(
                "SELECT thread_id,observation_id FROM observations").fetchall()}
        for project_id, thread_id in (("purge-a", "thread-a"), ("keep-b", "thread-b")):
            service.review(self._proposal(project_id, [refs[thread_id]], service=service))
        self.assertEqual("retryable", service.deliver_pending()["status"])
        with service.db.connect() as c:
            ids = {row["project_id"]: row["delivery_id"] for row in c.execute(
                "SELECT o.delivery_id,p.project_id FROM outbox o JOIN projects p USING(project_id)").fetchall()}
            c.execute("BEGIN IMMEDIATE")
            captures = service.privacy_purge_hook(c, project_ids=["purge-a"], threads=[],
                observation_ids=[], delivery_ids=[ids["purge-a"]])
            c.commit()
            self.assertEqual(1, len(captures))
            self.assertEqual("cancelled", c.execute("SELECT status FROM outbox WHERE delivery_id=?",
                                                      (ids["keep-b"],)).fetchone()[0])
            self.assertEqual("cancelled", c.execute("SELECT result FROM outbox_attempts WHERE delivery_id=?",
                                                      (ids["keep-b"],)).fetchone()[0])

    def test_stale_source_cancels_delivery_until_a_fresh_review_rearms_it(self):
        item = self.observations(1)[0]
        self.service.ingest(self.batch([item]))
        with self.service.db.connect() as c:
            ref = c.execute("SELECT observation_id FROM observations").fetchone()[0]
        proposal = self._proposal("freshness", [ref])
        self.service.review(proposal)
        self.clock.advance(hours=2)
        self.assertEqual(0, self.service.deliver_pending()["delivered"])
        with self.service.db.connect() as c:
            self.assertEqual("cancelled", c.execute("SELECT status FROM outbox").fetchone()[0])
        self.service.ingest(self.batch([item], cursor="fresh-heartbeat"))
        self.assertTrue(self.service.review(proposal)["delivery_created"])
        self.assertEqual(1, self.service.deliver_pending()["delivered"])

    def test_repeated_typed_failures_create_separate_recoverable_notice(self):
        self.service.scheduler.record_failure("source", "codex:test-install", "quota", "attempt-1")
        self.service.scheduler.record_failure("source", "codex:test-install", "quota", "attempt-2")
        state = self.service.state()["monitoring"]
        self.assertEqual(1, len(state["open_notices"]))
        self.assertEqual(2, state["open_notices"][0]["occurrences"])
        self.service.scheduler.recover("source", "codex:test-install")
        self.assertEqual([], self.service.state()["monitoring"]["open_notices"])
        self.service.scheduler.record_failure("source", "codex:test-install", "quota", "attempt-3")
        self.service.scheduler.record_failure("source", "codex:test-install", "quota", "attempt-4")
        self.assertEqual(1, len(self.service.state()["monitoring"]["open_notices"]))
        self.service.scheduler.recover("source", "codex:test-install")
        self.assertEqual([], self.service.state()["monitoring"]["open_notices"])
        with self.service.db.connect() as c:
            self.assertEqual(2, c.execute("SELECT COUNT(*) FROM health_incidents WHERE status='resolved'").fetchone()[0])

    def test_preflight_health_persists_retry_backoff_across_worker_restart(self):
        first = self.service.scheduler.record_failure("reviewer", "openclaw", "authentication", "preflight-1")
        self.assertEqual(900, first["retry_after_seconds"])
        restarted_worker_delay = self.service.scheduler.health_backoff_seconds("reviewer", "openclaw")
        self.assertEqual(900, restarted_worker_delay["retry_after_seconds"])
        self.clock.advance(seconds=901)
        self.assertEqual(0, self.service.scheduler.health_backoff_seconds("reviewer", "openclaw")["retry_after_seconds"])
        second = self.service.scheduler.record_failure("reviewer", "openclaw", "authentication", "preflight-2")
        self.assertEqual(1800, second["retry_after_seconds"])

    def test_local_schedule_coalesces_missed_days_and_uses_iana_date(self):
        root = Path(self.temp.name)
        local_clock = Clock(datetime(2026, 10, 21, 16, tzinfo=timezone.utc))
        service = WatchkeeperService(Database(root / "schedule.sqlite"),
            FileEmailSink(root / "schedule-mail", "demo@example.invalid"), clock=local_clock,
            schedule_timezone="America/Los_Angeles", review_time="08:00", digest_time="08:15")
        self.assertEqual(2, len(service.scheduler.ensure_daily()))
        self.assertEqual([], service.scheduler.ensure_daily())
        local_clock.advance(days=3)
        self.assertEqual(2, len(service.scheduler.ensure_daily()))
        with service.db.connect() as c:
            rows = c.execute("SELECT schedule_key,status FROM monitoring_runs ORDER BY schedule_key").fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual(["superseded", "pending"], [row["status"] for row in rows])

    def test_daily_recent_delta_precedes_resumed_history_backfill(self):
        root = Path(self.temp.name)
        service = WatchkeeperService(Database(root / "recent-backfill.sqlite"),
            FileEmailSink(root / "recent-mail", "demo@example.invalid"), clock=self.clock,
            schedule_timezone="UTC", review_time="00:00", digest_time="00:00",
            review_window_limit=2, review_windows_per_day=3)
        service.ingest(self.batch(self.observations(5)))
        first_day_recent = service.claim_review_job()
        self.assertEqual({"message-0", "message-1"},
                         {row["message_id"] for row in first_day_recent["review_input"]["observations"]})
        self.assertEqual("success", service.complete_review_job(first_day_recent["run_id"],
            first_day_recent["lease_token"], [])["status"])
        self.clock.advance(days=1)
        newest = {"thread_id": "thread-a", "message_id": "new-delta", "revision": "1",
                  "source_timestamp": (self.clock() - timedelta(minutes=1)).isoformat(),
                  "locator": "thread-a#new-delta", "text": "Synthetic newest delta"}
        service.ingest(self.batch([newest], cursor="delta-cursor"))
        recent = service.claim_review_job()
        self.assertIn("new-delta", {row["message_id"] for row in recent["review_input"]["observations"]})
        self.assertEqual("success", service.complete_review_job(recent["run_id"], recent["lease_token"], [])["status"])
        history = service.claim_review_job()
        self.assertIsNotNone(history)
        self.assertEqual({"new-delta", "message-0"},
            {row["message_id"] for row in history["review_input"]["observations"]})

    def test_recent_capture_order_finds_old_revision_without_moving_history_cursor(self):
        root = Path(self.temp.name)
        service = WatchkeeperService(Database(root / "recent-old-revision.sqlite"),
            FileEmailSink(root / "recent-old-revision-mail", "demo@example.invalid"), clock=self.clock,
            schedule_timezone="UTC", review_time="00:00", digest_time="00:00",
            review_window_limit=2, review_windows_per_day=3)
        items = self.observations(6)
        service.ingest(self.batch(items))

        initial_recent = service.claim_review_job()
        self.assertEqual("recent", self._job_stream(service, initial_recent))
        self.assertEqual("success", service.complete_review_job(initial_recent["run_id"],
            initial_recent["lease_token"], []) ["status"])
        first_history = service.claim_review_job()
        self.assertEqual("history", self._job_stream(service, first_history))
        history_page_ids = {row["message_id"] for row in first_history["review_input"]["observations"]}
        self.assertEqual({"message-0", "message-1"}, history_page_ids)
        self.assertEqual("success", service.complete_review_job(first_history["run_id"],
            first_history["lease_token"], []) ["status"])
        with service.db.connect() as c:
            saved_cursor = c.execute("SELECT cursor_json FROM review_progress WHERE stream='history'").fetchone()[0]
        self.assertIsNotNone(saved_cursor)

        self.clock.advance(days=1)
        revised = dict(items[4], revision="2", text="Synthetic revised old message")
        service.ingest(self.batch([revised], cursor="old-message-revised-today"))
        next_recent = service.claim_review_job()
        self.assertEqual("recent", self._job_stream(service, next_recent))
        recent_rows = next_recent["review_input"]["observations"]
        self.assertEqual("message-4", recent_rows[0]["message_id"])
        self.assertEqual("2", recent_rows[0]["revision"])
        self.assertEqual("success", service.complete_review_job(next_recent["run_id"],
            next_recent["lease_token"], []) ["status"])
        with service.db.connect() as c:
            self.assertEqual(saved_cursor,
                c.execute("SELECT cursor_json FROM review_progress WHERE stream='history'").fetchone()[0])

        resumed_history = service.claim_review_job()
        self.assertEqual("history", self._job_stream(service, resumed_history))
        resumed_ids = {row["message_id"] for row in resumed_history["review_input"]["observations"]}
        self.assertEqual({"message-2", "message-3"}, resumed_ids)
        self.assertFalse(resumed_ids.intersection(history_page_ids))

    @staticmethod
    def _job_stream(service, job):
        with service.db.connect() as c:
            row = c.execute("SELECT snapshot_json FROM monitoring_runs WHERE run_id=?", (job["run_id"],)).fetchone()
        return json.loads(row[0])["stream"]

    def test_progress_watermark_prevents_old_retained_evidence_from_claiming_stall(self):
        current = self.clock()
        observation = {"observation_id": "obs", "source_id": "src", "thread_id": "thread",
                      "revision": "1", "source_timestamp": (current - timedelta(days=5)).isoformat(),
                      "captured_at": (current - timedelta(days=5)).isoformat()}
        payload = {"project_id": "watermark", "outcome": "Synthetic outcome", "next_action": "Review the synthetic plan",
                   "reasoning": "Fabricated", "evidence_refs": ["obs"], "evidence_revisions": {"obs": "1"},
                   "status": "inferred", "grouping_confidence": "strong"}
        source = {"available": True, "last_seen_at": current.isoformat(),
                  "coverage": {"state": "complete", "gaps": []},
                  "threads": {"thread": {"coverage_complete": True,
                      "gaps": [],
                      "last_progress_at": (current - timedelta(days=1)).isoformat(),
                      "last_progress_revision": "2",
                      "last_progress_captured_at": (current - timedelta(hours=1)).isoformat()}}}
        result = validate_proposal(payload, {"obs": observation}, now=current,
                                   sources={"src": source}, inferred_after_days=3)
        self.assertFalse(result["eligible"])
        self.assertEqual("newer_progress_not_reviewed", result["eligibility_reason"])
        payload["status"] = "explicit_request"
        result = validate_proposal(payload, {"obs": observation}, now=current,
                                   sources={"src": source}, inferred_after_days=3)
        self.assertFalse(result["eligible"])
        self.assertEqual("newer_progress_not_reviewed", result["eligibility_reason"])
        payload["status"] = "inferred"
        source["threads"]["thread"]["last_progress_at"] = (current + timedelta(days=1)).isoformat()
        result = validate_proposal(payload, {"obs": observation}, now=current,
                                   sources={"src": source}, inferred_after_days=3)
        self.assertFalse(result["eligible"])
        self.assertEqual("invalid_progress_watermark", result["eligibility_reason"])

    def test_watermark_matches_newest_source_instant_not_later_captured_backfill(self):
        current = self.clock()
        latest_source_at = current - timedelta(days=5)
        latest_source_capture = latest_source_at + timedelta(minutes=1)
        evidence = {
            "older-backfill": {"observation_id": "older-backfill", "source_id": "src", "thread_id": "thread",
                "revision": "1", "source_timestamp": (latest_source_at - timedelta(days=1)).isoformat(),
                "captured_at": (current - timedelta(minutes=1)).isoformat()},
            "newest-source": {"observation_id": "newest-source", "source_id": "src", "thread_id": "thread",
                "revision": "1", "source_timestamp": latest_source_at.isoformat(),
                "captured_at": latest_source_capture.isoformat()},
        }
        proposal = {"project_id": "backfill-watermark", "outcome": "Synthetic outcome",
                   "next_action": "Review the synthetic plan", "reasoning": "Fabricated",
                   "evidence_refs": ["older-backfill"], "evidence_revisions": {"older-backfill": "1"},
                   "status": "explicit_request", "grouping_confidence": "strong"}
        source = {"available": True, "freshness": "fresh", "last_seen_at": current.isoformat(),
                  "coverage": {"state": "complete", "message_completeness": "full",
                               "archived_scanned": True, "gaps": []},
                  "threads": {"thread": {"coverage_complete": True, "gaps": [],
                      "last_progress_at": latest_source_at.isoformat(), "last_progress_revision": "1",
                      "last_progress_captured_at": latest_source_capture.isoformat()}}}
        result = validate_proposal(proposal, evidence, now=current, sources={"src": source})
        self.assertTrue(result["eligible"])


if __name__ == "__main__":
    unittest.main()
