"""Synthetic Codex app-server contract through real HTTP intake and state."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from watchkeeper.collector import CodexCollector, JsonLineAppServer, READ_METHODS, ReaderFailure, _item_timestamp
from watchkeeper.config import Config
from watchkeeper.db import Database
from watchkeeper.delivery import FileEmailSink
from watchkeeper.http import serve
from watchkeeper.service import WatchkeeperService


class FakeCodexRpc:
    """Small protocol double backed only by fabricated public-safe history."""
    def __init__(self, history):
        self.history = deepcopy(history)
        self.calls = []
        self.fail_on_turn_cursor = None

    def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, deepcopy(params)))
        if method not in READ_METHODS | {"initialize"}:
            raise AssertionError(f"forbidden RPC: {method}")
        if method == "initialize":
            return {"userAgent": "Codex/0.162.1"}
        if method == "thread/list":
            archived = params["archived"]
            threads = [t for t in self.history["threads"] if t["archived"] is archived]
            cursor = params.get("cursor")
            offset = int(cursor.split(":")[1]) if cursor else 0
            limit = params["limit"]
            page = threads[offset:offset + limit]
            next_cursor = f"{int(archived)}:{offset + limit}" if offset + limit < len(threads) else None
            return {"data": [self._metadata(t) for t in page], "nextCursor": next_cursor}
        if method == "thread/read":
            return {"thread": self._metadata(self._thread(params["threadId"]))}
        if method == "thread/turns/list":
            cursor = params.get("cursor")
            if self.fail_on_turn_cursor is not None and cursor == self.fail_on_turn_cursor:
                raise ReaderFailure("unavailable")
            thread = self._thread(params["threadId"])
            turns = thread["turns"]
            offset = int(cursor) if cursor else 0
            page = turns[offset:offset + params["limit"]]
            next_cursor = str(offset + params["limit"]) if offset + params["limit"] < len(turns) else None
            return {"data": [dict(t, itemsView="full") for t in page], "nextCursor": next_cursor}
        raise AssertionError(f"unsupported RPC: {method}")

    @staticmethod
    def _metadata(thread):
        return {k: thread[k] for k in ("id", "threadSource", "updatedAt") if k in thread}

    def _thread(self, thread_id):
        return next(t for t in self.history["threads"] if t["id"] == thread_id)


class TestClock:
    def __init__(self):
        # Keep the clock after the newest source event in the public fixture;
        # policy correctly rejects evidence timestamps from the future.
        self.value = datetime(2026, 10, 21, 18, 0, 0, tzinfo=timezone.utc)
    def __call__(self):
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class CollectionHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = json.loads(Path(__file__).parents[1].joinpath("fixtures/codex-reader/threads.json").read_text())
        self.source_dir = self.root / "reader-state"
        self.source_dir.mkdir()
        self.config = Config(self.root / "state.db", self.root / "email", "intake-secret", "reviewer-secret",
                             "alice", "private-pass")
        database = Database(self.config.database)
        sink = FileEmailSink(self.config.email_dir, self.config.recipient, self.config.public_base_url)
        service = WatchkeeperService(database, sink, clock=TestClock())
        self.service = service
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

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, *, payload=None, token=None, basic=None):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        if basic:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        data = json.dumps(payload).encode() if payload is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers, method="POST" if data else "GET")
        with urlopen(request, timeout=3) as response:
            body = response.read()
            return json.loads(body) if "json" in response.headers.get("Content-Type", "") else body.decode()

    def collector(self, history=None, *, max_pages=50, max_threads=50):
        return CodexCollector(source_id=self.history["source_id"], source_location=self.source_dir, enabled=True,
                              rpc=FakeCodexRpc(history or self.history), source_version="0.162.1",
                              page_size=1, max_threads=max_threads, max_pages=max_pages)

    def intake(self, batch):
        return self.request("/api/observations", payload=batch, token=self.config.intake_token)

    def run_collect(self, collector, cursor):
        return collector.collect(cursor, self.intake)

    def source_state(self):
        return self.request("/api/state", basic=(self.config.ui_username, self.config.ui_password))["sources"][0]

    def test_collection_replay_revision_coverage_and_email_flow(self):
        result = self.run_collect(self.collector(), "")
        self.assertEqual("complete", result["status"])
        source = self.source_state()
        self.assertEqual("full", source["coverage"]["message_completeness"])
        self.assertTrue(source["coverage"]["archived_scanned"])
        self.assertEqual("unknown", source["coverage"]["current_activity"])

        before = self.request("/api/review-input", token=self.config.reviewer_token)["observations"]
        self.assertEqual(5, len(before))
        self.assertTrue(all("Codex " in item["locator"] for item in before))
        replay = self.run_collect(self.collector(), source["cursor"])
        after = self.request("/api/review-input", token=self.config.reviewer_token)["observations"]
        self.assertEqual("complete", replay["status"])
        self.assertEqual(5, len(after))
        captures = {o["message_id"]: o["captured_at"] for o in before}
        self.assertEqual(captures, {o["message_id"]: o["captured_at"] for o in after})

        edited = deepcopy(self.history)
        edited["threads"][0]["turns"][0]["items"][1]["text"] = "The garden plan still needs nursery confirmation."
        self.run_collect(self.collector(edited), source["cursor"])
        revised = self.request("/api/review-input", token=self.config.reviewer_token)["observations"]
        matching = next(o for o in revised if o["message_id"] == "message-g1-agent")
        self.assertIn("still needs", matching["text"])
        self.assertNotEqual(captures["message-g1-agent"], matching["captured_at"])
        older = {"source_id": self.history["source_id"], "cursor": self.source_state()["cursor"],
                 "available": True, "observations": [{"thread_id": matching["thread_id"],
                    "message_id": matching["message_id"], "revision": "2026-10-20T00:00:00+00:00|old",
                    "source_timestamp": "2026-10-20T00:00:00Z", "locator": "older source page",
                    "text": "Older content must not replace the newer source revision."}]}
        self.intake(older)
        after_old_retry = self.request("/api/review-input", token=self.config.reviewer_token)["observations"]
        matching_after_retry = next(o for o in after_old_retry if o["message_id"] == "message-g1-agent")
        self.assertIn("still needs", matching_after_retry["text"])

        proposal = {"project_id": "garden-guide", "outcome": "Publish a tested garden guide",
                    "next_action": "Ask a local nursery to confirm planting dates",
                    "reasoning": "The draft exists and the captured messages leave its plans untested.",
                    "evidence_refs": [o["observation_id"] for o in revised if o["thread_id"].startswith("fixture-thread-garden-")]}
        proposal["evidence_revisions"] = {o["observation_id"]: o["revision"] for o in revised
                                           if o["thread_id"].startswith("fixture-thread-garden-")}
        proposal["status"] = "explicit_request"
        proposal["grouping_confidence"] = "strong"
        self.request("/api/reviews", payload=proposal, token=self.config.reviewer_token)
        self.assertIn("Ask a local nursery", str(self.request("/api/state", basic=("alice", "private-pass"))))
        self.assertTrue(list(self.config.email_dir.glob("*.eml")) == [])
        # Email remains a controlled explicit user action.
        page_request = Request(self.base + "/", headers={"Authorization": "Basic " +
                              base64.b64encode(b"alice:private-pass").decode()})
        with urlopen(page_request) as response:
            page = response.read().decode()
            csrf = response.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        self.assertIn("Capture pending digest", page)
        deliver = Request(self.base + "/api/deliver", data=b"", method="POST", headers={
            "Authorization": "Basic " + base64.b64encode(b"alice:private-pass").decode(),
            "Origin": self.base, "Cookie": f"pw_csrf={csrf}", "X-CSRF-Token": csrf})
        with urlopen(deliver) as response:
            self.assertEqual(1, json.loads(response.read())["delivered"])
        self.assertEqual(1, len(list(self.config.email_dir.glob("*.eml"))))

    def test_partial_turn_pagination_resumes_after_ack_and_converges(self):
        cursor = ""
        states = []
        for _ in range(10):
            result = self.run_collect(self.collector(max_pages=2), cursor)
            cursor = result["cursor"]
            states.append(result["status"])
            if result["status"] == "complete":
                break
        self.assertIn("partial", states)
        self.assertEqual("complete", states[-1])
        observations = self.request("/api/review-input", token=self.config.reviewer_token)["observations"]
        self.assertEqual(5, len(observations))
        self.assertEqual("full", self.source_state()["coverage"]["message_completeness"])

    def test_changed_listing_resets_checkpoint_with_gap_then_clean_scan_converges(self):
        first = self.run_collect(self.collector(max_pages=2), "")
        checkpoint = CodexCollector._decode_cursor(first["cursor"])
        self.assertEqual("fixture-thread-garden-1", checkpoint.get("last_thread_id"))

        changed = deepcopy(self.history)
        changed["threads"] = [t for t in changed["threads"] if t["id"] != checkpoint["last_thread_id"]]
        # Reorder the surviving listing as well as deleting the acknowledged anchor.
        changed["threads"].reverse()
        second = self.run_collect(self.collector(changed, max_pages=20), first["cursor"])
        self.assertEqual("partial", second["status"])
        self.assertTrue(any("listing changed" in gap for gap in second["coverage"]["gaps"]))
        self.assertFalse(CodexCollector._decode_cursor(second["cursor"]).get("scan_gap", False))

        third = self.run_collect(self.collector(changed, max_pages=20), second["cursor"])
        self.assertEqual("complete", third["status"])
        state = self.source_state()
        self.assertEqual("complete", state["coverage"]["state"])
        observations = self.request("/api/review-input", token=self.config.reviewer_token)["observations"]
        self.assertTrue(any(item["message_id"] == "message-g2-agent" for item in observations))
        self.assertTrue(any(item["message_id"] == "message-archived-user" for item in observations))

    def test_json_rpc_deadline_includes_interleaved_notifications(self):
        class FakeInput:
            def write(self, value):
                pass
            def flush(self):
                pass
        class FakeProcess:
            stdin = FakeInput()
        class NotificationQueue:
            def __init__(self):
                self.reads = 0
            def get(self, timeout):
                self.reads += 1
                return '{"jsonrpc":"2.0","method":"thread/updated"}'
        rpc = object.__new__(JsonLineAppServer)
        rpc.process = FakeProcess()
        rpc.timeout = 0.05
        rpc._next_id = 1
        rpc._lines = NotificationQueue()
        rpc._lock = threading.Lock()
        with patch("watchkeeper.collector.time.monotonic", side_effect=[0.0, 0.0, 0.03, 0.06]):
            with self.assertRaises(TimeoutError):
                rpc.call("thread/list", {"archived": False})
        self.assertEqual(2, rpc._lines.reads)

    def test_reader_launch_cleans_temporary_state_when_initialize_times_out(self):
        class FakeInput:
            def write(self, value):
                pass
            def flush(self):
                pass
        class BlockingOutput:
            def __init__(self, stopped):
                self.stopped = stopped
            def readline(self):
                self.stopped.wait(timeout=1)
                return ""
        class FakeProcess:
            def __init__(self):
                self.stopped = threading.Event()
                self.stdin = FakeInput()
                self.stdout = BlockingOutput(self.stopped)
                self.running = True
            def poll(self):
                return None if self.running else 0
            def terminate(self):
                self.running = False
                self.stopped.set()
            def wait(self, timeout=None):
                self.running = False
                return 0
            def kill(self):
                self.terminate()
        marker = self.source_dir / ".watchkeeper-reader-state"
        marker.write_text(json.dumps({"format": "watchkeeper-codex-reader-state-v1",
                                      "credentials_excluded": True, "transcripts_only": True}))
        runtime_home = self.root / "runtime-timeout"
        runtime_home.mkdir()
        process = FakeProcess()
        with patch("watchkeeper.collector.tempfile.mkdtemp", return_value=str(runtime_home)), \
             patch("watchkeeper.collector.subprocess.run", return_value=type("Result", (), {"stdout": "codex-cli 0.162.1\n"})()), \
             patch("watchkeeper.collector.subprocess.Popen", return_value=process):
            with self.assertRaises(TimeoutError):
                JsonLineAppServer.launch("codex", self.source_dir, timeout=0.01)
        self.assertFalse(runtime_home.exists())
        self.assertFalse(process.running)

    def test_reader_launch_cleans_temporary_state_when_copy_fails(self):
        marker = self.source_dir / ".watchkeeper-reader-state"
        marker.write_text(json.dumps({"format": "watchkeeper-codex-reader-state-v1",
                                      "credentials_excluded": True, "transcripts_only": True}))
        sessions = self.source_dir / "sessions"
        sessions.mkdir()
        (sessions / "synthetic.jsonl").write_text("{}\n")
        runtime_home = self.root / "runtime-under-test"
        runtime_home.mkdir()
        with patch("watchkeeper.collector.tempfile.mkdtemp", return_value=str(runtime_home)), \
             patch("watchkeeper.collector.subprocess.run", return_value=type("Result", (), {"stdout": "codex-cli 0.162.1\n"})()), \
             patch("watchkeeper.collector.shutil.copytree", side_effect=OSError("synthetic copy failure")):
            with self.assertRaisesRegex(OSError, "synthetic copy failure"):
                JsonLineAppServer.launch("codex", self.source_dir)
        self.assertFalse(runtime_home.exists())

    def test_oversized_turn_page_chunks_http_payload_and_replays_after_lost_ack(self):
        history = {"source_id": self.history["source_id"], "source_version": "0.162.1", "threads": [{
            "id": "fixture-thread-large", "archived": False, "threadSource": "cli", "updatedAt": 1792602000,
            "turns": [{"id": "turn-large", "status": "completed", "startedAt": 1792600000,
                       "completedAt": 1792600100, "items": [
                {"id": f"large-message-{index:03d}", "type": "userMessage",
                 "content": [{"type": "text", "text": (f"synthetic-{index:03d}-" + "x" * 18_000)}],
                 "startedAtMs": 1792600000000, "turnId": "turn-large"}
                for index in range(120)]}]}]}
        collector = self.collector(history)
        observation_batches = 0
        batch_sizes = []

        def lose_second_ack(batch):
            nonlocal observation_batches
            batch_sizes.append(len(json.dumps(batch).encode("utf-8")))
            result = self.intake(batch)
            if batch["observations"]:
                observation_batches += 1
                if observation_batches == 2:
                    # Simulate the durable HTTP response being lost in transit.
                    raise RuntimeError("synthetic lost ACK after durable intake")
            return result

        with self.assertRaisesRegex(RuntimeError, "synthetic lost ACK"):
            collector.collect("", lose_second_ack)
        self.assertGreaterEqual(observation_batches, 2)
        self.assertLessEqual(max(batch_sizes), 900_000)
        self.assertGreater(sum(batch_sizes), 1_000_000)
        checkpoint = self.source_state()["cursor"]
        decoded = CodexCollector._decode_cursor(checkpoint)
        self.assertEqual("fixture-thread-large", decoded.get("thread_id"))
        self.assertTrue(decoded.get("page_pending"))
        with self.service.db.connect() as connection:
            partial_rows = connection.execute(
                "SELECT message_id,captured_at FROM observations WHERE source_id=? ORDER BY message_id",
                (self.history["source_id"],)).fetchall()
        self.assertGreater(len(partial_rows), 0)
        partial_captures = {row["message_id"]: row["captured_at"] for row in partial_rows}

        replay = self.run_collect(self.collector(history), checkpoint)
        self.assertEqual("complete", replay["status"])
        with self.service.db.connect() as connection:
            all_rows = connection.execute(
                "SELECT message_id,captured_at FROM observations WHERE source_id=? ORDER BY message_id",
                (self.history["source_id"],)).fetchall()
        self.assertEqual(120, len(all_rows))
        final_captures = {row["message_id"]: row["captured_at"] for row in all_rows}
        self.assertEqual(partial_captures, {key: final_captures[key] for key in partial_captures})

    def test_unsupported_and_malformed_history_stays_partial_across_capped_runs(self):
        history = deepcopy(self.history)
        history["threads"][0]["turns"][0]["items"].append(
            {"id": "synthetic-tool-output", "type": "functionCallOutput", "output": "not a message"})
        history["threads"][1]["turns"].insert(0, {"id": None, "itemsView": "full", "items": []})
        history["threads"][1]["turns"].insert(1, {"id": "turn-missing-time", "itemsView": "full", "items": [
            {"id": "message-missing-time", "type": "agentMessage", "text": "No source timestamp was recorded."}]})
        history["threads"][1].pop("updatedAt", None)
        cursor = ""
        last_result = None
        for _ in range(10):
            last_result = self.run_collect(self.collector(history, max_pages=2), cursor)
            cursor = last_result["cursor"]
            if last_result["status"] == "complete":
                break
        self.assertIsNotNone(last_result)
        self.assertEqual("complete", last_result["status"])
        state = self.source_state()
        self.assertEqual("partial", state["coverage"]["state"])
        self.assertEqual("partial", state["coverage"]["message_completeness"])
        self.assertEqual("partial", state["threads"]["fixture-thread-garden-1"]["completeness"])
        self.assertEqual("partial", state["threads"]["fixture-thread-garden-2"]["completeness"])
        observations = self.request("/api/review-input", token=self.config.reviewer_token)["observations"]
        self.assertTrue(any(item["message_id"] == "message-g2-agent" for item in observations))

    def test_partial_and_failure_coverage_are_visible_and_cursor_is_preserved(self):
        result = self.run_collect(self.collector(max_threads=1), "")
        state = self.source_state()
        self.assertEqual("partial", state["coverage"]["state"])
        self.assertFalse(state["coverage"]["archived_scanned"])
        self.assertIn("Configured scan limit", " ".join(state["coverage"]["gaps"]))
        old_cursor = state["cursor"]
        failure_report = {**state["coverage"], "failure": "unavailable", "state": "partial",
                          "gaps": ["Collection interrupted; last acknowledged checkpoint retained"]}
        failed = self.intake({"source_id": self.history["source_id"], "cursor": "untrusted-next",
                              "available": False, "observations": [], "coverage": failure_report})
        self.assertEqual(old_cursor, failed["cursor"])
        self.assertEqual(old_cursor, self.source_state()["cursor"])
        self.assertEqual("stale", self.source_state()["freshness"])
        self.assertEqual("unavailable", self.source_state()["coverage"]["failure"])
        self.assertEqual("unknown", self.request("/api/state", basic=("alice", "private-pass"))["sources"][0]["coverage"]["current_activity"])

    def test_guardrails_reject_missing_opt_in_arbitrary_rpc_and_fake_source_time(self):
        with self.assertRaises(ValueError):
            CodexCollector(source_id="x", source_location=self.source_dir, enabled=False)
        rpc = FakeCodexRpc(self.history)
        with self.assertRaises(PermissionError):
            from watchkeeper.collector import JsonLineAppServer
            object.__new__(JsonLineAppServer).call("turn/start", {})
        self.assertTrue(READ_METHODS.issuperset({"thread/list", "thread/read", "thread/turns/list", "thread/items/list"}))
        with self.assertRaises(ValueError):
            _item_timestamp({}, {}, {})
        with self.assertRaises(ValueError):
            self.service.intake.ingest({"source_id": "bad\x00source", "cursor": "",
                                        "available": True, "observations": []})


if __name__ == "__main__":
    unittest.main()
