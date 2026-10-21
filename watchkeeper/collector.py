"""Read-only Codex app-server collector pinned to the inspected v2 thread API."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import queue
import time
import tempfile
import shutil
import re
import urllib.error
import urllib.request
from typing import Callable, Protocol

CODEX_READER_VERSION = "0.162.1"
SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]
READ_METHODS = frozenset({"thread/list", "thread/read", "thread/turns/list", "thread/items/list"})
_ALLOWED_METHODS = READ_METHODS | {"initialize", "initialized"}
MAX_INTAKE_BATCH_BYTES = 900_000


class ReaderFailure(RuntimeError):
    def __init__(self, kind: str):
        if kind not in {"authentication", "quota", "unsupported", "unavailable", "protocol"}:
            kind = "protocol"
        super().__init__(kind)
        self.kind = kind


class Rpc(Protocol):
    def call(self, method: str, params: dict | None = None): ...
    def notify(self, method: str, params: dict | None = None): ...


class JsonLineAppServer:
    """Minimal stdio JSON-RPC client. It rejects every method outside the reader allowlist."""

    def __init__(self, process, runtime_home=None, *, timeout=30):
        self.process = process
        self.runtime_home = runtime_home
        self.timeout = timeout
        self._next_id = 1
        self._lines = queue.Queue()
        def read_lines():
            for line in iter(process.stdout.readline, ""):
                self._lines.put(line)
            self._lines.put(None)
        threading.Thread(target=read_lines, daemon=True).start()
        self._lock = threading.Lock()

    @classmethod
    def launch(cls, binary: str, source_location: Path, *, timeout=30):
        # Runtime is intentionally unavailable against a conventional Codex
        # home. Operators must provision a credential-free, disposable mirror.
        marker = source_location / ".watchkeeper-reader-state"
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("live reader requires an explicitly provisioned isolated Codex reader state")
        marker_data = marker.read_text(encoding="utf-8")
        try:
            attestation = json.loads(marker_data)
        except json.JSONDecodeError as exc:
            raise ValueError("isolated reader marker must be valid JSON") from exc
        if attestation != {"format": "watchkeeper-codex-reader-state-v1", "credentials_excluded": True,
                           "transcripts_only": True}:
            raise ValueError("isolated reader marker does not attest credential-free transcript-only state")
        for entry in source_location.iterdir():
            if entry.is_symlink():
                raise ValueError("isolated reader state cannot contain symlinks")
            if entry.name == ".watchkeeper-reader-state":
                continue
            if entry.is_file():
                raise ValueError("isolated reader state contains an unapproved file")
            if entry.is_dir() and entry.name not in {"sessions", "archived_sessions"}:
                raise ValueError("isolated reader state contains an unapproved directory")
            if not entry.is_file() and not entry.is_dir():
                raise ValueError("isolated reader state contains a special file")
        for dirname in ("sessions", "archived_sessions"):
            directory = source_location / dirname
            if directory.exists():
                for root, dirs, files in os.walk(directory, followlinks=False):
                    if any((Path(root) / child).is_symlink() for child in dirs + files):
                        raise ValueError("isolated reader history cannot contain symlinks")
                    if any(not file.endswith(".jsonl") or not (Path(root) / file).is_file() for file in files):
                        raise ValueError("isolated reader history accepts JSONL transcript files only")
        runtime_home = Path(tempfile.mkdtemp(prefix="watchkeeper-codex-reader-"))
        proc = None
        rpc = None
        try:
            env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
            env["CODEX_HOME"] = str(runtime_home)
            env["HOME"] = str(runtime_home)
            env["XDG_CONFIG_HOME"] = str(runtime_home / "config")
            env["XDG_CACHE_HOME"] = str(runtime_home / "cache")
            version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                     timeout=timeout, check=True, env=env, cwd=str(runtime_home))
            if not re.search(rf"(?<![0-9]){re.escape(CODEX_READER_VERSION)}(?![0-9])", version.stdout.strip()):
                raise RuntimeError(f"Codex reader version must be {CODEX_READER_VERSION}")
            # App-server receives only the explicitly prepared state mirror. It
            # cannot discover or mutate the user's normal Codex home.
            for dirname in ("sessions", "archived_sessions"):
                entry = source_location / dirname
                if entry.is_dir():
                    shutil.copytree(entry, runtime_home / dirname, dirs_exist_ok=True)
            proc = subprocess.Popen([binary, "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                    env=env, cwd=str(runtime_home))
            rpc = cls(proc, runtime_home, timeout=timeout)
            rpc.call("initialize", {"clientInfo": {"name": "project-watchkeeper", "title": "Project Watchkeeper",
                                                      "version": "0.1.0"}, "capabilities": {}})
            rpc.notify("initialized", {})
            return rpc
        except Exception:
            if rpc is not None:
                rpc.close()
            elif proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
            shutil.rmtree(runtime_home, ignore_errors=True)
            raise

    def call(self, method: str, params: dict | None = None):
        if method not in _ALLOWED_METHODS or method == "initialized":
            raise PermissionError(f"Codex collector cannot call {method!r}")
        with self._lock:
            deadline = time.monotonic() + self.timeout
            request_id = self._next_id
            self._next_id += 1
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                                 "params": params or {}}, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex app-server read timed out")
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("Codex app-server read timed out") from exc
                if not line:
                    raise RuntimeError("Codex app-server closed its reader stream")
                reply = json.loads(line)
                if reply.get("id") != request_id:
                    # Server notifications and requests do not authorize additional client methods.
                    if "method" in reply:
                        continue
                    raise RuntimeError("unexpected Codex app-server response")
                if "error" in reply:
                    error = reply.get("error") or {}
                    data = error.get("data") if isinstance(error, dict) else None
                    code = data.get("codexErrorInfo") if isinstance(data, dict) else None
                    kind = {"unauthorized": "authentication", "usageLimitExceeded": "quota",
                            "rateLimitExceeded": "quota", "unsupportedMethod": "unsupported"}.get(code, "protocol")
                    raise ReaderFailure(kind)
                return reply.get("result", {})

    def notify(self, method: str, params: dict | None = None):
        if method not in _ALLOWED_METHODS or method != "initialized":
            raise PermissionError(f"Codex collector cannot send notification {method!r}")
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}},
                                            separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except Exception:
                    try:
                        self.process.kill()
                        self.process.wait(timeout=5)
                    except Exception:
                        pass
        finally:
            if self.runtime_home is not None:
                shutil.rmtree(self.runtime_home, ignore_errors=True)


class CodexCollector:
    """Collect only from one explicitly enabled Codex installation.

    The caller supplies the source location, installation identity, RPC client,
    and durable HTTP intake callback. There is no home-directory discovery.
    """

    def __init__(self, *, source_id: str, source_location: str | Path, enabled: bool,
                 rpc: Rpc | None = None, binary="codex", source_version=CODEX_READER_VERSION,
                 page_size=100, max_threads=500, max_pages=100, now: Callable[[], datetime] | None = None):
        if enabled is not True:
            raise ValueError("Codex collection requires explicit opt-in")
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
            raise ValueError("an explicit source installation ID is required")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in source_id):
            raise ValueError("source installation ID cannot contain control characters")
        if source_location is None or not str(source_location).strip():
            raise ValueError("an explicit Codex source location is required")
        self.source_location = Path(source_location).expanduser().resolve()
        if not self.source_location.is_dir():
            raise ValueError("configured Codex source location must be an existing directory")
        if page_size < 1 or page_size > 500 or max_threads < 1 or max_threads > 10_000 or max_pages < 1 or max_pages > 1_000:
            raise ValueError("collection limits must be positive and bounded")
        if source_version != CODEX_READER_VERSION:
            raise ValueError(f"Codex reader schema is pinned to {CODEX_READER_VERSION}")
        self.source_id = source_id
        self.rpc = rpc
        self.binary = binary
        self.source_version = source_version
        self.page_size = page_size
        self.max_threads = max_threads
        self.max_pages = max_pages
        self.now = now or (lambda: datetime.now(timezone.utc))

    def collect(self, cursor: str, post_batch: Callable[[dict], dict], *, scan_archived=True) -> dict:
        if not isinstance(cursor, str) or len(cursor) > 2000:
            raise ValueError("invalid durable collector cursor")
        rpc = self.rpc
        owns_rpc = rpc is None
        latest_ack = {"cursor": cursor}
        latest_report = {}
        def acknowledged(batch):
            result = post_batch(batch)
            if isinstance(result, dict) and result.get("cursor") == batch["cursor"]:
                latest_ack["cursor"] = batch["cursor"]
                if isinstance(batch.get("coverage"), dict):
                    latest_report.update(batch["coverage"])
            return result
        try:
            if rpc is None:
                rpc = JsonLineAppServer.launch(self.binary, self.source_location)
            return self._collect(rpc, cursor, acknowledged, scan_archived)
        except Exception as exc:
            failure = exc.kind if isinstance(exc, ReaderFailure) else ("unavailable" if isinstance(exc, (TimeoutError, OSError)) else "protocol")
            report = dict(latest_report) if latest_report else self._report(
                "partial", 0, 0, 0, scan_archived, "unknown", [], latest_ack["cursor"])
            report["state"] = "partial"
            report["failure"] = failure
            report["gaps"] = (list(report.get("gaps", [])) +
                              ["Collection interrupted; the last acknowledged cursor is retained"])[:50]
            try:
                post_batch({"source_id": self.source_id, "cursor": latest_ack["cursor"], "available": False,
                            "observations": [], "coverage": report})
            except Exception:
                pass
            raise
        finally:
            if owns_rpc and rpc is not None:
                rpc.close()

    def _collect(self, rpc, cursor, post_batch, scan_archived):
        self._observed_running = False
        checkpoint = self._decode_cursor(cursor)
        committed_cursor = cursor
        phase = checkpoint["phase"]
        list_cursor = checkpoint["cursor"]
        next_list_cursor = checkpoint.get("next_cursor")
        last_thread_id = checkpoint.get("last_thread_id")
        turn_cursor = checkpoint.get("turn_cursor")
        thread_partial = bool(checkpoint.get("thread_partial", False))
        scan_gap = bool(checkpoint.get("scan_gap", False))
        resume_thread_id = (checkpoint.get("thread_id") if checkpoint.get("thread_id") and
                            (turn_cursor or checkpoint.get("page_pending")) else None)
        if (checkpoint.get("thread_id") and turn_cursor is None and last_thread_id is None and
                not checkpoint.get("page_pending")):
            last_thread_id = checkpoint["thread_id"]
        listed = read = messages = work_pages = 0
        gaps = []
        # Persist an explicit in-progress report before touching source history.
        self._ack(post_batch, {"source_id": self.source_id, "cursor": committed_cursor, "available": True, "observations": [],
                    "coverage": self._report("partial", 0, 0, 0, scan_archived, "unknown", ["Collection in progress"], committed_cursor)})
        phases = [False, True] if scan_archived else [False]
        if phase not in phases:
            phase, list_cursor = phases[0], None
        while phase in phases and work_pages < self.max_pages and read < self.max_threads:
            response = rpc.call("thread/list", {"archived": phase, "cursor": list_cursor,
                                                 "limit": min(self.page_size, self.max_threads - read),
                                                 "sortKey": "updated_at", "sortDirection": "desc", "useStateDbOnly": False,
                                                 "sourceKinds": SOURCE_KINDS})
            data = response.get("data")
            if not isinstance(data, list):
                raise RuntimeError("Codex thread/list returned no data page")
            next_list_cursor = response.get("nextCursor")
            if next_list_cursor is not None and not isinstance(next_list_cursor, str):
                raise ReaderFailure("protocol")
            work_pages += 1
            listed += len(data)
            previous_index = -1
            restart = False
            if last_thread_id:
                previous_index = next((i for i, t in enumerate(data) if t.get("id") == last_thread_id), -1)
                if previous_index < 0:
                    restart = True
            first_index = previous_index + 1
            if resume_thread_id:
                first_index = next((i for i, t in enumerate(data) if t.get("id") == resume_thread_id), -1)
                if first_index < 0:
                    restart = True
            if restart:
                scan_gap = True
                gaps.append("Source listing changed; traversal restarted with overlapping reads")
                phase, list_cursor, next_list_cursor = phases[0], None, None
                last_thread_id = resume_thread_id = None
                turn_cursor = None
                thread_partial = False
                listed = read = messages = 0
                committed_cursor = self._encode_cursor(phase, None, None, None, None, None, scan_gap=scan_gap)
                self._ack(post_batch, {"source_id": self.source_id, "cursor": committed_cursor,
                                       "available": True, "observations": [],
                                       "coverage": self._report("partial", listed, read, messages, scan_archived,
                                                                 "unknown", ["Rescanning changed source pages"], committed_cursor)})
                continue
            for thread in data[first_index:]:
                thread_id = thread.get("id") if isinstance(thread, dict) else None
                if not isinstance(thread_id, str) or not thread_id:
                    gaps.append("A listed thread had no stable ID")
                    continue
                if work_pages >= self.max_pages or read >= self.max_threads:
                    break
                try:
                    read_response = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False})
                except ReaderFailure:
                    raise
                except Exception:
                    gaps.append(f"Could not read thread {thread_id}")
                    self._ack(post_batch, {"source_id": self.source_id, "cursor": committed_cursor,
                                           "available": True, "observations": [],
                                           "coverage": self._report("partial", listed, read, messages, scan_archived,
                                                                     "partial", gaps, committed_cursor),
                                           "thread_coverage": [{"thread_id": thread_id, "completeness": "partial",
                                                                "activity": "unknown"}]})
                    break
                metadata = read_response.get("thread") or thread
                activity = _activity(metadata)
                listed_activity = _activity(thread)
                if "observed-running" in (activity, listed_activity):
                    activity = "observed-running"
                elif "observed-not-running" in (activity, listed_activity):
                    activity = "observed-not-running"
                if activity == "observed-running":
                    self._observed_running = True
                local_turn_cursor = turn_cursor if thread_id == checkpoint.get("thread_id") else None
                local_thread_partial = thread_partial if thread_id == checkpoint.get("thread_id") else False
                pending_turn_cursor = None
                while work_pages < self.max_pages:
                    turn_page_cursor = local_turn_cursor
                    prior_thread_partial = local_thread_partial
                    turn_response = rpc.call("thread/turns/list", {"threadId": thread_id, "cursor": turn_page_cursor,
                                                                      "limit": self.page_size, "sortDirection": "asc",
                                                                      "itemsView": "full"})
                    turns = turn_response.get("data")
                    if not isinstance(turns, list):
                        gaps.append(f"Could not read turns for thread {thread_id}")
                        break
                    observations, complete = self._turn_observations(turns, thread_id, metadata)
                    work_pages += 1
                    next_turn_cursor = turn_response.get("nextCursor")
                    if next_turn_cursor is not None and not isinstance(next_turn_cursor, str):
                        raise ReaderFailure("protocol")
                    page_thread_partial = prior_thread_partial or not complete
                    chunks = self._split_observations(observations)
                    for chunk_index, chunk in enumerate(chunks):
                        final_chunk = chunk_index == len(chunks) - 1
                        saved_turn_cursor = next_turn_cursor if final_chunk else turn_page_cursor
                        saved_thread_partial = page_thread_partial if final_chunk else prior_thread_partial
                        continuation = self._encode_cursor(
                            phase, list_cursor, next_list_cursor, thread_id, saved_turn_cursor, None,
                            saved_thread_partial, scan_gap=scan_gap, page_pending=True)
                        coverage = self._report("partial", listed, read,
                                                messages + sum(len(part) for part in chunks[:chunk_index + 1]),
                                                scan_archived, "partial" if page_thread_partial else "unknown",
                                                ["Collection in progress"], continuation)
                        self._ack(post_batch, {"source_id": self.source_id, "cursor": continuation,
                                               "available": True, "observations": chunk, "coverage": coverage,
                                               "thread_coverage": [{"thread_id": thread_id,
                                                                    "completeness": "complete" if final_chunk and not page_thread_partial and not next_turn_cursor else "partial",
                                                                    "activity": activity}]})
                        committed_cursor = continuation
                        messages += len(chunk)
                    local_thread_partial = page_thread_partial
                    pending_turn_cursor = next_turn_cursor
                    if gaps or not next_turn_cursor:
                        break
                    local_turn_cursor = next_turn_cursor
                if gaps:
                    break
                if pending_turn_cursor and work_pages >= self.max_pages:
                    # Turns remain; their opaque source cursor is already durable.
                    gaps.append("Configured pagination limit reached")
                    break
                last_thread_id = thread_id
                read += 1
                turn_cursor = None
                thread_partial = False
                resume_thread_id = None
                committed_cursor = self._encode_cursor(phase, list_cursor, next_list_cursor, None, None, thread_id, False, scan_gap=scan_gap)
                self._ack(post_batch, {"source_id": self.source_id, "cursor": committed_cursor,
                                       "available": True, "observations": [],
                                       "coverage": self._report("partial", listed, read, messages, scan_archived,
                                                                 "unknown", ["Collection in progress"], committed_cursor),
                                       "thread_coverage": [{"thread_id": thread_id,
                                                            "completeness": "partial" if local_thread_partial else "complete",
                                                            "activity": activity}]})
            if gaps:
                break
            if work_pages >= self.max_pages or read >= self.max_threads:
                break
            if next_list_cursor:
                list_cursor = next_list_cursor
                last_thread_id = None
                turn_cursor = None
                committed_cursor = self._encode_cursor(phase, list_cursor, None, None, None, None, scan_gap=scan_gap)
                self._ack(post_batch, {"source_id": self.source_id, "cursor": committed_cursor,
                                       "available": True, "observations": [],
                                       "coverage": self._report("partial", listed, read, messages, scan_archived,
                                                                 "unknown", ["Collection continues"], committed_cursor)})
                continue
            phase_index = phases.index(phase)
            if phase_index + 1 < len(phases):
                phase = phases[phase_index + 1]
                list_cursor = next_list_cursor = last_thread_id = turn_cursor = None
                committed_cursor = self._encode_cursor(phase, None, None, None, None, None, scan_gap=scan_gap)
                self._ack(post_batch, {"source_id": self.source_id, "cursor": committed_cursor,
                                       "available": True, "observations": [],
                                       "coverage": self._report("partial", listed, read, messages, scan_archived,
                                                                 "unknown", ["Archived scan in progress"], committed_cursor)})
            else:
                phase = phases[0]
                list_cursor = next_list_cursor = last_thread_id = turn_cursor = None
                break
        bounded = work_pages >= self.max_pages or read >= self.max_threads
        if bounded:
            gaps.append("Configured scan limit reached")
        if scan_gap:
            gaps.append("A clean follow-up scan is required after the changed source listing")
        state = "partial" if gaps or bounded or scan_gap else "complete"
        if state == "complete" or (scan_gap and not bounded):
            # Preserve a partial report for this overlapping run, then start a
            # clean cycle so the following run can restore complete coverage.
            final_cursor = self._encode_cursor(phases[0], None, None, None, None, None)
        else:
            final_cursor = committed_cursor
        report = self._report(state, listed, read, messages, scan_archived,
                              "full" if state == "complete" else "partial", gaps, final_cursor)
        # Final ACK commits complete coverage and the resumable scan checkpoint.
        ack = self._ack(post_batch, {"source_id": self.source_id, "cursor": final_cursor, "available": True,
                                     "observations": [], "coverage": report})
        return {"source_id": self.source_id, "status": state, "threads_listed": listed,
                "threads_read": read, "messages_read": messages, "cursor": ack.get("cursor", final_cursor),
                "coverage": report}

    @staticmethod
    def _split_observations(observations):
        """Split one source page well below the HTTP intake request limit."""
        chunks = []
        current = []
        for observation in observations:
            candidate = current + [observation]
            if len(json.dumps({"observations": candidate}).encode("utf-8")) > 700_000:
                if not current:
                    raise ValueError("one observation exceeds the safe intake batch size")
                chunks.append(current)
                current = [observation]
                if len(json.dumps({"observations": current}).encode("utf-8")) > 700_000:
                    raise ValueError("one observation exceeds the safe intake batch size")
            else:
                current = candidate
        if current or not chunks:
            chunks.append(current)
        return chunks

    def _turn_observations(self, turns, thread_id, thread):
        complete = True
        observations = []
        for turn in turns:
            turn_id = turn.get("id")
            if not isinstance(turn_id, str):
                complete = False
                continue
            entries = turn.get("items", [])
            if turn.get("itemsView") != "full" or not isinstance(entries, list):
                complete = False
            for entry in entries:
                item = entry.get("item", entry) if isinstance(entry, dict) else {}
                kind = item.get("type")
                if kind not in ("userMessage", "agentMessage"):
                    complete = False
                    continue
                text, text_complete = _message_text(item)
                item_id = item.get("id")
                if not text_complete:
                    complete = False
                if not text or not isinstance(item_id, str) or not item_id:
                    continue
                try:
                    timestamp = _item_timestamp(entry if "item" in entry else {}, turn, thread)
                except ValueError:
                    complete = False
                    continue
                digest = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                revision = f"{timestamp}|{digest}"
                source_kind = thread.get("threadSource") or thread.get("source") or "unknown"
                observations.append({"thread_id": thread_id, "message_id": item_id, "revision": revision,
                                     "source_timestamp": timestamp, "locator": f"Codex {source_kind} thread {thread_id}, {kind}, turn {turn_id}",
                                     "text": text[:20000]})
                if len(text) > 20000:
                    complete = False
        return observations, complete

    @staticmethod
    def _ack(post_batch, batch):
        if len(json.dumps(batch).encode("utf-8")) > MAX_INTAKE_BATCH_BYTES:
            raise ValueError("intake batch exceeds the safe request size")
        result = post_batch(batch)
        if not isinstance(result, dict) or result.get("cursor") != batch["cursor"]:
            raise RuntimeError("intake ACK did not confirm the requested durable cursor")
        return result

    def _report(self, state, listed, read, messages, archived, completeness, gaps, cursor, failure="none"):
        return {"state": state, "threads_listed": listed, "threads_read": read, "messages_read": messages,
                "scan_limit": self.max_threads, "archived_scanned": bool(archived and state == "complete"),
                "message_completeness": completeness,
                "current_activity": "observed-running" if getattr(self, "_observed_running", False) else "unknown",
                "gaps": list(gaps[:50]), "source_version": self.source_version, "failure": failure}

    @staticmethod
    def _decode_cursor(cursor):
        if not cursor:
            return {"phase": False, "cursor": None}
        try:
            value = json.loads(cursor)
            allowed = {"phase", "cursor", "next_cursor", "last_thread_id", "thread_id", "turn_cursor",
                       "thread_partial", "scan_gap", "page_pending"}
            if (set(value).issubset(allowed) and "phase" in value and type(value["phase"]) is bool
                    and type(value.get("scan_gap", False)) is bool
                    and type(value.get("page_pending", False)) is bool):
                return value
        except (json.JSONDecodeError, TypeError):
            pass
        return {"phase": False, "cursor": None}

    @staticmethod
    def _encode_cursor(phase, cursor, next_cursor=None, thread_id=None, turn_cursor=None,
                       last_thread_id=None, thread_partial=False, *, scan_gap=False, page_pending=False):
        return json.dumps({"phase": bool(phase), "cursor": cursor, "next_cursor": next_cursor,
                           "last_thread_id": last_thread_id, "thread_id": thread_id, "turn_cursor": turn_cursor,
                           "thread_partial": bool(thread_partial), "scan_gap": bool(scan_gap),
                           "page_pending": bool(page_pending)}, separators=(",", ":"))


def _message_text(item):
    if item.get("type") == "agentMessage":
        value = item.get("text", "")
        return (value, True) if isinstance(value, str) else ("", False)
    chunks = []
    complete = True
    for part in item.get("content", []):
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        else:
            complete = False
    return "\n".join(chunks), complete


def _activity(thread):
    status = thread.get("status")
    if isinstance(status, dict):
        kind = status.get("type")
    elif isinstance(status, str):
        kind = status
    else:
        kind = None
    if kind == "active":
        return "observed-running"
    if kind == "idle":
        return "observed-not-running"
    return "unknown"


def _item_timestamp(entry, turn, thread):
    milliseconds = entry.get("completedAtMs") or entry.get("startedAtMs")
    if isinstance(milliseconds, (int, float)):
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="seconds")
    value = turn.get("completedAt") or turn.get("startedAt") or thread.get("updatedAt")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    raise ValueError("Codex did not provide a source timestamp")


def _save_cursor(path: Path, cursor: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(cursor)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    path.chmod(0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only collector for one explicitly enabled Codex reader state")
    parser.add_argument("--enable-codex", action="store_true", help="explicit opt-in required for collection")
    parser.add_argument("--source-id", required=True, help="stable installation identity")
    parser.add_argument("--source-dir", required=True, help="isolated Codex reader state directory")
    parser.add_argument("--intake-url", required=True, help="Watchkeeper POST /api/observations URL")
    parser.add_argument("--intake-token-file", required=True, help="file containing collector bearer token")
    parser.add_argument("--cursor-file", required=True, help="private local durable cursor file")
    parser.add_argument("--codex-binary", default="codex", help="pinned Codex 0.162.1 executable")
    parser.add_argument("--max-threads", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args(argv)
    if not args.enable_codex:
        parser.error("--enable-codex is required; collection is opt-in")
    token_path = Path(args.intake_token_file).expanduser()
    if token_path.stat().st_mode & 0o077:
        parser.error("intake token file must not be accessible by group or other users")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        parser.error("intake token file is empty")
    cursor_path = Path(args.cursor_file).expanduser()
    cursor = cursor_path.read_text(encoding="utf-8") if cursor_path.exists() else ""
    collector = CodexCollector(source_id=args.source_id, source_location=args.source_dir, enabled=True,
                               binary=args.codex_binary, max_threads=args.max_threads, max_pages=args.max_pages)

    def post_batch(batch):
        request = urllib.request.Request(args.intake_url, data=json.dumps(batch).encode("utf-8"),
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Watchkeeper intake did not acknowledge collection") from exc
        if result.get("cursor") != batch["cursor"]:
            raise RuntimeError("Watchkeeper returned a different durable cursor")
        _save_cursor(cursor_path, result["cursor"])
        return result

    result = collector.collect(cursor, post_batch)
    print(json.dumps({k: v for k, v in result.items() if k != "cursor"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
