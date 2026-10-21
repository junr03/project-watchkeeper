"""Synthetic tests for explicitly opted-in Codex mirror preparation."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_collection import make_history_projection
from watchkeeper.codex_mirror import MirrorPreparationError, prepare_codex_mirror
from watchkeeper.codex_mirror import MAX_TRANSCRIPT_FILE_BYTES
from watchkeeper.collector import main


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IntakeHandler(BaseHTTPRequestHandler):
    received = []
    authorization = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(value)
        type(self).authorization.append(self.headers.get("Authorization"))
        body = json.dumps({"cursor": value["cursor"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CodexMirrorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "explicit-codex-source"
        (self.source / "sessions" / "nested").mkdir(parents=True)
        (self.source / "archived_sessions").mkdir()
        self.thread_id = "synthetic-mirror-thread"
        self.transcript = self.source / "sessions" / "nested" / "session.jsonl"
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.thread_id}}) + "\n")
        self.archived = self.source / "archived_sessions" / "old.jsonl"
        self.archived.write_text(json.dumps({"type": "session_meta", "payload": {"id": "synthetic-archived"}}) + "\n")
        self.sentinels = {}
        for name, content in (("auth.json", "sentinel auth data"),
                              ("config.toml", "sentinel config data"),
                              ("state_5.sqlite", "sentinel general state, not SQLite")):
            path = self.source / name
            path.write_text(content)
            self.sentinels[path] = _digest(path)

    def tearDown(self):
        self.temp.cleanup()

    def test_only_selected_history_is_copied_and_source_files_remain_unchanged(self):
        projection = self.source / "thread_history_1.sqlite"
        make_history_projection(projection, self.thread_id)
        source_paths = [self.transcript, self.archived, projection, *self.sentinels]
        before = {path: _digest(path) for path in source_paths}
        with prepare_codex_mirror(self.source) as mirror:
            self.assertEqual((mirror / "sessions/nested/session.jsonl").read_text(), self.transcript.read_text())
            self.assertTrue((mirror / "archived_sessions/old.jsonl").is_file())
            self.assertTrue((mirror / ".watchkeeper-reader-state").is_file())
            self.assertTrue((mirror / "thread_history_1.sqlite").is_file())
            self.assertFalse((mirror / "auth.json").exists())
            self.assertFalse((mirror / "config.toml").exists())
            self.assertFalse((mirror / "state_5.sqlite").exists())
            self.assertEqual(0o700, mirror.stat().st_mode & 0o777)
        self.assertEqual(before, {path: _digest(path) for path in source_paths})

    def test_wal_projection_is_backed_up_without_changing_source_or_copying_sidecars(self):
        projection = self.source / "thread_history_1.sqlite"
        make_history_projection(projection, self.thread_id)
        source_db = sqlite3.connect(projection)
        self.addCleanup(source_db.close)
        source_db.execute("PRAGMA journal_mode=WAL")
        source_db.execute("INSERT INTO thread_items(thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, item_json) "
                          "VALUES(?, 'turn-wal', 'item-wal', 1, 1792617000000, '{}')", (self.thread_id,))
        source_db.commit()
        wal = Path(str(projection) + "-wal")
        shm = Path(str(projection) + "-shm")
        self.assertTrue(wal.is_file())
        source_paths = [projection, wal, shm, self.transcript, self.archived, *self.sentinels]
        before = {path: _digest(path) for path in source_paths if path.exists()}
        with prepare_codex_mirror(self.source) as mirror:
            self.assertTrue((mirror / "thread_history_1.sqlite").is_file())
            self.assertFalse(Path(str(mirror / "thread_history_1.sqlite") + "-wal").exists())
            self.assertFalse(Path(str(mirror / "thread_history_1.sqlite") + "-shm").exists())
        self.assertEqual(before, {path: _digest(path) for path in source_paths if path.exists()})

    def test_selected_symlinks_and_projection_thread_mismatch_fail_closed(self):
        outside = self.root / "outside.jsonl"
        outside.write_text("not copied\n")
        linked = self.source / "sessions" / "linked.jsonl"
        linked.symlink_to(outside)
        with self.assertRaises(MirrorPreparationError) as caught:
            with prepare_codex_mirror(self.source):
                pass
        self.assertEqual("protocol", caught.exception.kind)
        linked.unlink()

        projection = self.source / "thread_history_1.sqlite"
        make_history_projection(projection, "unselected-synthetic-thread")
        with self.assertRaises(MirrorPreparationError) as caught:
            with prepare_codex_mirror(self.source):
                pass
        self.assertEqual("protocol", caught.exception.kind)

    def test_oversized_transcript_fails_without_truncation(self):
        oversized = self.source / "sessions" / "oversized.jsonl"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_TRANSCRIPT_FILE_BYTES + 1)
        with self.assertRaises(MirrorPreparationError) as caught:
            with prepare_codex_mirror(self.source):
                pass
        self.assertEqual("protocol", caught.exception.kind)

    def test_changes_during_transcript_copy_are_unavailable_and_temp_is_removed(self):
        original_copy = __import__("watchkeeper.codex_mirror", fromlist=["_copy_transcript"])._copy_transcript
        def mutate_after_copy(entry, destination):
            original_copy(entry, destination)
            if entry[0] == self.transcript:
                self.transcript.write_text(self.transcript.read_text() + "{}\n")
        with patch("watchkeeper.codex_mirror._copy_transcript", side_effect=mutate_after_copy):
            with self.assertRaises(MirrorPreparationError) as caught:
                with prepare_codex_mirror(self.source):
                    pass
        self.assertEqual("unavailable", caught.exception.kind)

    def test_cli_reports_content_free_preparation_failure_to_configured_intake(self):
        invalid = self.source / "sessions" / "linked.jsonl"
        outside = self.root / "outside.jsonl"
        outside.write_text("secret-sentinel\n")
        invalid.symlink_to(outside)
        token_file = self.root / "token"
        token_file.write_text("synthetic-intake-token")
        token_file.chmod(0o600)
        cursor_file = self.root / "cursor"
        cursor_file.write_text("durable-cursor")
        IntakeHandler.received = []
        IntakeHandler.authorization = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), IntakeHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("sys.stdout"):
                result = main(["--enable-codex", "--source-id", "synthetic-codex",
                               "--prepare-from-codex-dir", str(self.source),
                               "--intake-url", f"http://127.0.0.1:{server.server_port}/api/observations",
                               "--intake-token-file", str(token_file), "--cursor-file", str(cursor_file)])
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()
        self.assertEqual(2, result)
        self.assertEqual("durable-cursor", cursor_file.read_text())
        self.assertEqual(1, len(IntakeHandler.received))
        self.assertEqual(["Bearer synthetic-intake-token"], IntakeHandler.authorization)
        batch = IntakeHandler.received[0]
        self.assertFalse(batch["available"])
        self.assertEqual("durable-cursor", batch["cursor"])
        self.assertEqual("protocol", batch["coverage"]["failure"])
        serialized = json.dumps(batch)
        self.assertNotIn(str(self.source), serialized)
        self.assertNotIn("secret-sentinel", serialized)
        self.assertNotIn("sentinel auth data", serialized)

    def test_cli_uses_and_removes_temporary_mirror_after_collection(self):
        token_file = self.root / "token-success"
        token_file.write_text("synthetic-intake-token")
        token_file.chmod(0o600)
        cursor_file = self.root / "cursor-success"
        cursor_file.write_text("old-cursor")
        original = {path: _digest(path) for path in [self.transcript, self.archived, *self.sentinels]}
        captured = {}

        class FakeCollector:
            def __init__(self, **kwargs):
                captured["mirror"] = Path(kwargs["source_location"])

            def collect(self, cursor, post_batch):
                mirror = captured["mirror"]
                self_outer.assertTrue((mirror / ".watchkeeper-reader-state").is_file())
                self_outer.assertTrue((mirror / "sessions/nested/session.jsonl").is_file())
                self_outer.assertFalse((mirror / "auth.json").exists())
                batch = {"source_id": "synthetic-codex", "cursor": "new-cursor",
                         "available": True, "observations": [], "coverage": {}}
                post_batch(batch)
                return {"status": "complete", "cursor": batch["cursor"]}

        self_outer = self
        IntakeHandler.received = []
        IntakeHandler.authorization = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), IntakeHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("watchkeeper.collector.CodexCollector", FakeCollector), patch("sys.stdout"):
                result = main(["--enable-codex", "--source-id", "synthetic-codex",
                               "--prepare-from-codex-dir", str(self.source),
                               "--intake-url", f"http://127.0.0.1:{server.server_port}/api/observations",
                               "--intake-token-file", str(token_file), "--cursor-file", str(cursor_file)])
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()
        self.assertEqual(0, result)
        self.assertFalse(captured["mirror"].exists())
        self.assertEqual("new-cursor", cursor_file.read_text())
        self.assertEqual(original, {path: _digest(path) for path in original})


if __name__ == "__main__":
    unittest.main()
