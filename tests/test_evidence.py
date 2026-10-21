"""Acceptance for the bounded reviewable-evidence SQL relation."""

import json
import tempfile
import unittest
from pathlib import Path

from watchkeeper.db import Database
from watchkeeper.evidence import install_reviewable_observations, retained_evidence


class ReviewableEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "evidence.sqlite")
        with self.db.connect() as c:
            install_reviewable_observations(c)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _evidence(oid, revision="1", captured="2026-10-20T10:00:00+00:00", text=None):
        return {"observation_id": oid, "source_id": "codex/synthetic", "thread_id": "thread-" + oid,
                "message_id": "message-" + oid, "revision": revision,
                "source_timestamp": captured, "captured_at": captured,
                "locator": "synthetic locator", "text": text or ("synthetic " + oid)}

    @staticmethod
    def _project(c, project_id, evidence, *, disposition="open", archived=0):
        c.execute("""INSERT INTO projects(project_id,outcome,next_action,reasoning,evidence_json,fingerprint,
            disposition,snooze_until,updated_at,archived) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (project_id, "synthetic outcome", "synthetic action", "synthetic reason",
             json.dumps(evidence), project_id + "-fp", disposition, None, "2026-10-21T10:00:00+00:00", archived))

    @staticmethod
    def _expired(c, item, *, expired_at="2026-10-21T10:00:00+00:00", first_captured_at=None):
        c.execute("INSERT INTO expired_observations VALUES(?,?,?,?,?,?,?)",
            (item["observation_id"], item["revision"], item["source_id"], item["thread_id"],
             item["message_id"], first_captured_at or item["captured_at"], expired_at))

    def test_latest_expired_revision_current_raw_precedence_and_lifecycle(self):
        with self.db.connect() as c:
            current = self._evidence("current", "2", "2026-10-21T12:00:00+00:00")
            c.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)", tuple(current[k] for k in (
                "observation_id", "source_id", "thread_id", "message_id", "revision", "source_timestamp",
                "captured_at", "locator", "text")))
            old_current_snapshot = self._evidence("current", "1")
            self._expired(c, old_current_snapshot)
            self._project(c, "raw-wins", [old_current_snapshot])

            valid = self._evidence("shared", "3", "2026-10-19T10:00:00+00:00")
            self._expired(c, valid)
            self._project(c, "open", [valid])
            self._project(c, "archived-open", [valid], archived=1)

            stale = self._evidence("stale", "1")
            newer = self._evidence("stale", "2", "2026-10-21T09:00:00+00:00")
            self._expired(c, stale, expired_at="2026-10-20T10:00:00+00:00")
            self._expired(c, newer, expired_at="2026-10-21T10:00:00+00:00")
            self._project(c, "stale", [stale])
            closed_only = self._evidence("closed-only", "4")
            self._expired(c, closed_only)
            self._project(c, "closed", [closed_only], disposition="completed")

            rows = c.execute("SELECT * FROM reviewable_observations ORDER BY observation_id,is_retained").fetchall()
            by_id = {}
            for row in rows:
                by_id.setdefault(row["observation_id"], []).append(dict(row))
            self.assertEqual(1, len(by_id["current"]))
            self.assertEqual(0, by_id["current"][0]["is_retained"])
            self.assertEqual(1, len(by_id["shared"]))
            self.assertEqual(1, by_id["shared"][0]["is_retained"])
            self.assertEqual("3", by_id["shared"][0]["revision"])
            self.assertNotIn("stale", by_id)

            retained = retained_evidence(c, [old_current_snapshot, valid, stale])
            self.assertEqual(["shared"], [item["observation_id"] for item in retained])
            self.assertNotIn("closed-only", {r["observation_id"] for r in rows})
            self.assertNotIn("stale", {r["observation_id"] for r in rows})

    def test_retained_rows_participate_in_keyset_paging_and_deleted_content_is_absent(self):
        with self.db.connect() as c:
            raw = self._evidence("raw", "1", "2026-10-21T12:00:00+00:00")
            c.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)", tuple(raw[k] for k in (
                "observation_id", "source_id", "thread_id", "message_id", "revision", "source_timestamp",
                "captured_at", "locator", "text")))
            kept = self._evidence("retained", "1", "2026-10-20T12:00:00+00:00")
            self._expired(c, kept)
            self._project(c, "kept-project", [kept])
            query = """SELECT observation_id,captured_at,is_retained FROM reviewable_observations
                       ORDER BY captured_at DESC,observation_id ASC LIMIT 1"""
            first = dict(c.execute(query).fetchone())
            self.assertEqual("raw", first["observation_id"])
            page_two = c.execute("""SELECT observation_id,captured_at,is_retained FROM reviewable_observations
                WHERE captured_at<? OR (captured_at=? AND observation_id>?)
                ORDER BY captured_at DESC,observation_id ASC LIMIT 1""",
                (first["captured_at"], first["captured_at"], first["observation_id"])).fetchone()
            self.assertEqual("retained", page_two["observation_id"])
            self.assertEqual(1, page_two["is_retained"])

            c.execute("DELETE FROM projects WHERE project_id='kept-project'")
            c.execute("DELETE FROM expired_observations WHERE observation_id='retained'")
            self.assertIsNone(c.execute("SELECT 1 FROM reviewable_observations WHERE observation_id='retained'").fetchone())


if __name__ == "__main__":
    unittest.main()
