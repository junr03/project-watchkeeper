"""Retention, project deletion, and replayable privacy decisions."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading


class ConflictError(ValueError):
    """The deletion preview no longer describes current shared associations."""


def _iso(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _iso(parsed)


class PrivacyManager:
    """Manage privacy operations independently of the SQLite backup lifecycle.

    The append-only journal contains identifiers and timestamps only. A record
    is fsynced before its SQLite mutation, then replayed idempotently on startup.
    """

    def __init__(self, database, journal_path, email_dir=None, retention_days=30, clock=None,
                 purge_hooks=None):
        if type(retention_days) is not int or not 1 <= retention_days <= 36500:
            raise ValueError("transcript retention must be an integer from 1 to 36500 days")
        self.db = database
        self.journal_path = Path(journal_path)
        self.email_dir = Path(email_dir) if email_dir is not None else None
        self.retention_days = retention_days
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._purge_hooks = list(purge_hooks or ())

    def register_purge_hook(self, hook):
        """Register a SQLite cleanup hook for derived stores owned by another slice.

        The hook receives the current transaction plus `project_ids`, `threads`,
        `observation_ids`, and `delivery_ids`; it must delete cached content
        without committing.
        Register hooks before calling `replay()` so restored snapshots are covered.
        """
        if not callable(hook):
            raise TypeError("purge hook must be callable")
        self._purge_hooks.append(hook)

    def _read_events(self):
        if not self.journal_path.exists():
            return []
        events = []
        with self.journal_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"privacy journal is malformed at line {line_number}") from exc
                if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] != line_number:
                    raise RuntimeError(f"privacy journal sequence is invalid at line {line_number}")
                events.append(event)
        return events

    def _append(self, event_type, **fields):
        with self._lock:
            events = self._read_events()
            event = {"seq": len(events) + 1, "type": event_type, "created_at": _iso(self.clock()), **fields}
            self.journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            existed = self.journal_path.exists()
            fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as stream:
                    stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(fd)
            self.journal_path.chmod(0o600)
            if not existed:
                dir_fd = os.open(self.journal_path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            return event

    def replay(self):
        """Apply all journal entries absent from this database snapshot."""
        with self._lock:
            events = self._read_events()
            cleanup_delivery_ids = set()
            recorded_cleanup_ids = set()
            for event in events:
                if event["type"] == "delete_project":
                    cleanup_delivery_ids.update(event.get("delivery_ids", []))
                elif event["type"] == "purge_capture_files":
                    ids = set(event.get("delivery_ids", []))
                    cleanup_delivery_ids.update(ids)
                    recorded_cleanup_ids.update(ids)
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                last = c.execute("SELECT last_seq FROM privacy_journal_state WHERE singleton_id=1").fetchone()[0]
                if type(last) is not int or last < 0:
                    c.rollback()
                    raise RuntimeError("privacy journal database cursor is invalid")
                if last > len(events):
                    c.rollback()
                    raise RuntimeError("privacy journal is missing records required by this database")
                for event in events:
                    if event["seq"] <= last:
                        continue
                    if event["type"] == "delete_project":
                        current_ids = self._current_delivery_ids(c, event)
                        cleanup_delivery_ids.update(current_ids)
                        missing_cleanup_ids = current_ids - set(event.get("delivery_ids", [])) - recorded_cleanup_ids
                        if missing_cleanup_ids:
                            cleanup_event = self._append("purge_capture_files",
                                                         delivery_ids=sorted(missing_cleanup_ids))
                            events.append(cleanup_event)
                            recorded_cleanup_ids.update(missing_cleanup_ids)
                            cleanup_delivery_ids.update(missing_cleanup_ids)
                    extra_capture_ids = self._apply(c, event) or set()
                    missing_hook_ids = extra_capture_ids - recorded_cleanup_ids - set(
                        event.get("delivery_ids", []) if event["type"] == "delete_project" else [])
                    if missing_hook_ids:
                        cleanup_event = self._append("purge_capture_files", delivery_ids=sorted(missing_hook_ids))
                        events.append(cleanup_event)
                        recorded_cleanup_ids.update(missing_hook_ids)
                        cleanup_delivery_ids.update(missing_hook_ids)
                    last = event["seq"]
                    c.execute("UPDATE privacy_journal_state SET last_seq=? WHERE singleton_id=1", (last,))
                c.commit()
            # A crash after the SQLite commit but before file cleanup is safe:
            # replay cleanup for every delete event, including already-applied
            # events, and file deletion is idempotent.
            self._purge_capture_files(cleanup_delivery_ids)

    @staticmethod
    def _current_delivery_ids(c, event):
        projects = {event["project_id"], *event.get("affected_project_ids", [])}
        threads = {(item["source_id"], item["thread_id"]) for item in event.get("threads", [])}
        if threads:
            clauses = " OR ".join("(source_id=? AND thread_id=?)" for _ in threads)
            args = tuple(value for pair in sorted(threads) for value in pair)
            projects.update(r[0] for r in c.execute(
                f"SELECT DISTINCT project_id FROM project_threads WHERE {clauses}", args))
        marks = ",".join("?" for _ in projects)
        return {r[0] for r in c.execute(
            f"SELECT delivery_id FROM outbox WHERE project_id IN ({marks})", tuple(projects))}

    def _apply(self, c, event):
        kind = event["type"]
        if kind == "delete_project":
            return self._apply_delete(c, event)
        elif kind == "purge_capture_files":
            # This append-only record is a durable retry queue. Capture files
            # are removed after commit and can be retried idempotently.
            pass
        elif kind == "exclude_source":
            c.execute("INSERT OR IGNORE INTO excluded_sources VALUES(?,?)",
                      (event["source_id"], event["created_at"]))
            self._cancel_source(c, event["source_id"])
        elif kind == "include_source":
            c.execute("DELETE FROM excluded_sources WHERE source_id=?", (event["source_id"],))
            self._cancel_source(c, event["source_id"])
        elif kind == "exclude_thread":
            c.execute("INSERT OR IGNORE INTO excluded_threads VALUES(?,?,?)",
                      (event["source_id"], event["thread_id"], event["created_at"]))
            self._cancel_thread(c, event["source_id"], event["thread_id"])
        elif kind == "include_thread":
            c.execute("DELETE FROM excluded_threads WHERE source_id=? AND thread_id=?",
                      (event["source_id"], event["thread_id"]))
            self._cancel_thread(c, event["source_id"], event["thread_id"])
        else:
            raise RuntimeError(f"unknown privacy journal event {kind!r}")

    @staticmethod
    def _cancel_source(c, source_id):
        c.execute("""UPDATE outbox SET status='cancelled' WHERE status='pending' AND project_id IN
            (SELECT project_id FROM project_threads WHERE source_id=? UNION
             SELECT project_id FROM thread_ownership WHERE source_id=?)""", (source_id, source_id))

    @staticmethod
    def _cancel_thread(c, source_id, thread_id):
        c.execute("""UPDATE outbox SET status='cancelled' WHERE status='pending' AND project_id IN
            (SELECT project_id FROM project_threads WHERE source_id=? AND thread_id=? UNION
             SELECT project_id FROM thread_ownership WHERE source_id=? AND thread_id=?)""",
                  (source_id, thread_id, source_id, thread_id))

    @staticmethod
    def _impact(c, project_id):
        project = c.execute("SELECT evidence_json FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise KeyError("project not found")
        threads = {(r[0], r[1]) for r in c.execute(
            "SELECT source_id,thread_id FROM project_threads WHERE project_id=?", (project_id,))}
        threads.update((item.get("source_id"), item.get("thread_id"))
                       for item in json.loads(project["evidence_json"])
                       if item.get("source_id") and item.get("thread_id"))
        if threads:
            clauses = " OR ".join("(source_id=? AND thread_id=?)" for _ in threads)
            args = tuple(value for pair in sorted(threads) for value in pair)
            associations = sorted((r[0], r[1], r[2]) for r in c.execute(
                f"SELECT source_id,thread_id,project_id FROM project_threads WHERE {clauses}", args))
        else:
            associations = []
        affected = sorted({row[2] for row in associations} - {project_id})
        return sorted(threads), associations, affected

    def preview(self, project_id):
        with self.db.connect() as c:
            threads, associations, affected = self._impact(c, project_id)
            projects = sorted([project_id, *affected])
            ids = ",".join("?" for _ in projects)
            deliveries = c.execute(
                f"SELECT delivery_id,status FROM outbox WHERE project_id IN ({ids})", tuple(projects)).fetchall()
        impact = {"project_id": project_id, "affected_projects": affected,
                  "threads": [{"source_id": s, "thread_id": t} for s, t in threads],
                  "associations": [list(row) for row in associations]}
        token = hashlib.sha256(json.dumps(impact, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"project_id": project_id, "affected_projects": affected,
                "affected_threads": impact["threads"], "associations": impact["associations"],
                "pending_notifications": sum(row["status"] == "pending" for row in deliveries),
                "captured_emails": sum(row["status"] == "delivered" for row in deliveries),
                "impact_token": token}

    def delete_project(self, project_id, impact_token, *, confirmed):
        with self._lock:
            return self._delete_project_locked(project_id, impact_token, confirmed=confirmed)

    def _delete_project_locked(self, project_id, impact_token, *, confirmed):
        if confirmed is not True or not isinstance(impact_token, str):
            raise ValueError("explicit confirmation and impact token are required")
        self.replay()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            threads, associations, affected = self._impact(c, project_id)
            projects = sorted([project_id, *affected])
            impact = {"project_id": project_id, "affected_projects": affected,
                      "threads": [{"source_id": s, "thread_id": t} for s, t in threads],
                      "associations": [list(row) for row in associations]}
            current_token = hashlib.sha256(json.dumps(impact, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if not hmac.compare_digest(current_token, impact_token):
                c.rollback()
                raise ConflictError("shared project associations changed; request a new deletion preview")
            ids = ",".join("?" for _ in projects)
            delivery_ids = [r[0] for r in c.execute(
                f"SELECT delivery_id FROM outbox WHERE project_id IN ({ids})", tuple(projects))]
            event = self._append("delete_project", project_id=project_id,
                                 affected_project_ids=affected,
                                 threads=impact["threads"], delivery_ids=sorted(delivery_ids))
            extra_capture_ids = self._apply(c, event) or set()
            cleanup_ids = set(delivery_ids) | extra_capture_ids
            missing_cleanup_ids = cleanup_ids - set(event.get("delivery_ids", []))
            if missing_cleanup_ids:
                cleanup_event = self._append("purge_capture_files", delivery_ids=sorted(missing_cleanup_ids))
                last_seq = cleanup_event["seq"]
            else:
                last_seq = event["seq"]
            c.execute("UPDATE privacy_journal_state SET last_seq=? WHERE singleton_id=1", (last_seq,))
            c.commit()
        self._purge_capture_files(cleanup_ids)
        return {"deleted_project": project_id, "affected_projects": affected,
                "excluded_threads": impact["threads"]}

    def set_source_excluded(self, source_id, excluded):
        with self._lock:
            self.replay()
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                exists = c.execute("SELECT 1 FROM excluded_sources WHERE source_id=?", (source_id,)).fetchone() is not None
                if exists == excluded:
                    c.commit()
                    return
                event = self._append("exclude_source" if excluded else "include_source", source_id=source_id)
                self._apply(c, event)
                c.execute("UPDATE privacy_journal_state SET last_seq=? WHERE singleton_id=1", (event["seq"],))
                c.commit()

    def set_thread_excluded(self, source_id, thread_id, excluded):
        with self._lock:
            self.replay()
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                exists = c.execute("SELECT 1 FROM excluded_threads WHERE source_id=? AND thread_id=?",
                                   (source_id, thread_id)).fetchone() is not None
                if exists == excluded:
                    c.commit()
                    return
                event = self._append("exclude_thread" if excluded else "include_thread",
                                     source_id=source_id, thread_id=thread_id)
                self._apply(c, event)
                c.execute("UPDATE privacy_journal_state SET last_seq=? WHERE singleton_id=1", (event["seq"],))
                c.commit()

    def expire(self):
        """Purge old raw text, retaining snapshots for every unresolved project."""
        cutoff = _iso(self.clock() - timedelta(days=self.retention_days))
        now = _iso(self.clock())
        removed = 0
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._refresh_watermarks(c)
            rows = c.execute("SELECT * FROM observations WHERE captured_at<?", (cutoff,)).fetchall()
            ids = {row["observation_id"] for row in rows}
            for row in rows:
                c.execute("INSERT OR IGNORE INTO expired_observations VALUES(?,?,?,?,?,?,?)",
                          (row["observation_id"], row["revision"], row["source_id"], row["thread_id"],
                           row["message_id"], row["captured_at"], now))
                c.execute("DELETE FROM observations WHERE observation_id=?", (row["observation_id"],))
            # Keep exact project evidence for all open projects, archived or
            # otherwise. Closed records retain summaries but not expired text.
            tombstoned = {(r[0], r[1]) for r in c.execute(
                "SELECT observation_id,revision FROM expired_observations WHERE first_captured_at<?", (cutoff,))}
            closed = c.execute("SELECT project_id,evidence_json FROM projects WHERE disposition!='open'").fetchall()
            for project in closed:
                evidence = json.loads(project["evidence_json"])
                retained = [item for item in evidence if (item.get("observation_id"), item.get("revision")) not in tombstoned]
                if len(retained) != len(evidence):
                    c.execute("UPDATE projects SET evidence_json=?,updated_at=? WHERE project_id=?",
                              (json.dumps(retained, ensure_ascii=False, sort_keys=True), now, project["project_id"]))
            proposals = c.execute("SELECT proposal_id,evidence_json FROM review_proposals").fetchall()
            for proposal in proposals:
                evidence = json.loads(proposal["evidence_json"])
                if any((item.get("observation_id"), item.get("revision")) in tombstoned for item in evidence):
                    c.execute("DELETE FROM review_proposals WHERE proposal_id=?", (proposal["proposal_id"],))
            removed = len(rows)
            c.commit()
        return {"expired": removed, "cutoff": cutoff}

    @staticmethod
    def _refresh_watermarks(c):
        for row in c.execute("SELECT source_id,thread_id,source_timestamp,revision,captured_at FROM observations").fetchall():
            progress = _timestamp(row["source_timestamp"])
            captured = _timestamp(row["captured_at"])
            prior = c.execute("SELECT last_progress_at,last_captured_at FROM thread_progress_watermarks "
                              "WHERE source_id=? AND thread_id=?", (row["source_id"], row["thread_id"])).fetchone()
            if prior is None:
                c.execute("INSERT INTO thread_progress_watermarks VALUES(?,?,?,?,?)",
                          (row["source_id"], row["thread_id"], progress, row["revision"], captured))
            elif progress > prior["last_progress_at"] or (
                    progress == prior["last_progress_at"] and captured > prior["last_captured_at"]):
                c.execute("UPDATE thread_progress_watermarks SET last_progress_at=?,revision=?,last_captured_at=? "
                          "WHERE source_id=? AND thread_id=?",
                          (progress, row["revision"], captured, row["source_id"], row["thread_id"]))

    def refresh_watermarks(self):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._refresh_watermarks(c)
            c.commit()

    def filter_expired_replays(self, batch):
        """Suppress a same-revision import after its raw row has expired."""
        items = batch.get("observations", [])
        if not items:
            return batch, 0
        from hashlib import sha256
        source_id = batch["source_id"]
        keep = []
        suppressed = 0
        with self.db.connect() as c:
            for item in items:
                oid = sha256(f"{source_id}\0{item['thread_id']}\0{item['message_id']}".encode()).hexdigest()
                tombstone = c.execute("SELECT 1 FROM expired_observations WHERE observation_id=? AND revision=?",
                                      (oid, item["revision"])).fetchone()
                if tombstone:
                    suppressed += 1
                else:
                    keep.append(item)
        return {**batch, "observations": keep}, suppressed

    def filter_excluded(self, batch):
        """Enforce durable exclusions before handing an adapter batch to intake."""
        items = batch.get("observations", [])
        if not items:
            return batch, 0
        source_id = batch["source_id"]
        keep = []
        with self.db.connect() as c:
            source_excluded = c.execute("SELECT 1 FROM excluded_sources WHERE source_id=?", (source_id,)).fetchone() is not None
            excluded_threads = {r[0] for r in c.execute("SELECT thread_id FROM excluded_threads WHERE source_id=?", (source_id,))}
        suppressed = 0
        for item in items:
            if source_excluded or item.get("thread_id") in excluded_threads:
                suppressed += 1
            else:
                keep.append(item)
        return {**batch, "observations": keep}, suppressed

    def _apply_delete(self, c, event):
        project_id = event["project_id"]
        affected_projects = set(event.get("affected_project_ids", []))
        threads = {(item["source_id"], item["thread_id"]) for item in event.get("threads", [])}
        # A restored older snapshot can contain another project newly linked
        # to one of the deleted threads. Scrub that direct impact without
        # traversing through its unrelated threads into further projects.
        if threads:
            clauses = " OR ".join("(source_id=? AND thread_id=?)" for _ in threads)
            args = tuple(value for pair in sorted(threads) for value in pair)
            affected_projects.update(r[0] for r in c.execute(
                f"SELECT DISTINCT project_id FROM project_threads WHERE {clauses}", args) if r[0] != project_id)
        projects = {project_id} | affected_projects

        thread_obs = set()
        for source_id, thread_id in threads:
            thread_obs.update(r[0] for r in c.execute(
                "SELECT observation_id FROM observations WHERE source_id=? AND thread_id=?", (source_id, thread_id)))
            thread_obs.update(r[0] for r in c.execute(
                "SELECT observation_id FROM expired_observations WHERE source_id=? AND thread_id=?", (source_id, thread_id)))
        delivery_ids = set(event.get("delivery_ids", []))
        if projects:
            marks = ",".join("?" for _ in projects)
            delivery_ids.update(r[0] for r in c.execute(
                f"SELECT delivery_id FROM outbox WHERE project_id IN ({marks})", tuple(projects)))
        # Derived stores can include cached reviewer input or scheduled payloads.
        # Remove those inside the same transaction before core rows disappear.
        extra_capture_ids = set()
        for hook in self._purge_hooks:
            values = hook(c, project_ids=sorted(projects), threads=sorted(threads),
                          observation_ids=sorted(thread_obs), delivery_ids=sorted(delivery_ids))
            if values is not None:
                if isinstance(values, str):
                    extra_capture_ids.add(values)
                else:
                    extra_capture_ids.update(value for value in values if isinstance(value, str))
        for source_id, thread_id in threads:
            c.execute("DELETE FROM observations WHERE source_id=? AND thread_id=?", (source_id, thread_id))
            # Deletion keeps only the minimal exclusion identifier. Explicit
            # re-enable authorizes collection of the conversation again.
            c.execute("DELETE FROM expired_observations WHERE source_id=? AND thread_id=?",
                      (source_id, thread_id))
            c.execute("DELETE FROM thread_progress_watermarks WHERE source_id=? AND thread_id=?", (source_id, thread_id))
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchkeeper_thread_coverage'").fetchone():
                c.execute("DELETE FROM watchkeeper_thread_coverage WHERE source_id=? AND thread_id=?",
                          (source_id, thread_id))
            c.execute("INSERT OR IGNORE INTO excluded_threads VALUES(?,?,?)",
                      (source_id, thread_id, event["created_at"]))
            c.execute("DELETE FROM project_threads WHERE source_id=? AND thread_id=?", (source_id, thread_id))
            c.execute("DELETE FROM thread_ownership WHERE source_id=? AND thread_id=?", (source_id, thread_id))
        if affected_projects:
            marks = ",".join("?" for _ in affected_projects)
            for row in c.execute(f"SELECT project_id,evidence_json FROM projects WHERE project_id IN ({marks})",
                                 tuple(affected_projects)).fetchall():
                evidence = json.loads(row["evidence_json"])
                remaining = [item for item in evidence
                             if (item.get("source_id"), item.get("thread_id")) not in threads]
                content = {"outcome": "Project summary removed after shared conversation deletion",
                           "next_action": "Review the remaining evidence and reassess this project",
                           "reasoning": "A shared conversation was deleted from Watchkeeper; derived project text was cleared."}
                refs = [item.get("observation_id") for item in remaining]
                fingerprint = hashlib.sha256(json.dumps({"project_id": row["project_id"], **content,
                                                          "evidence_refs": refs}, sort_keys=True).encode()).hexdigest()
                c.execute("""UPDATE projects SET outcome=?,next_action=?,reasoning=?,evidence_json=?,fingerprint=?,
                    assessment_kind='proposal',eligible=0,eligibility_reason='requires a fresh assessment after shared-thread deletion',
                    assessed_at=NULL,status='uncertain_grouping',grouping_confidence='uncertain',updated_at=?
                    WHERE project_id=?""",
                          (content["outcome"], content["next_action"], content["reasoning"],
                           json.dumps(remaining, ensure_ascii=False, sort_keys=True), fingerprint,
                           event["created_at"], row["project_id"]))
        marks = ",".join("?" for _ in projects)
        c.execute(f"DELETE FROM outbox WHERE project_id IN ({marks})", tuple(projects))
        c.execute("DELETE FROM project_threads WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM thread_ownership WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
        proposals = c.execute("SELECT proposal_id,requested_project_id,evidence_json FROM review_proposals").fetchall()
        for proposal in proposals:
            affected = proposal["requested_project_id"] in projects
            if not affected:
                evidence = json.loads(proposal["evidence_json"])
                affected = any(item.get("observation_id") in thread_obs or
                               (item.get("source_id"), item.get("thread_id")) in threads for item in evidence)
            if affected:
                c.execute("DELETE FROM review_proposals WHERE proposal_id=?", (proposal["proposal_id"],))
        return extra_capture_ids

    def _purge_capture_files(self, delivery_ids):
        if not delivery_ids or self.email_dir is None:
            return
        safe_ids = [re.sub(r"[^A-Za-z0-9._-]", "_", value) for value in delivery_ids]
        for safe_id in safe_ids:
            for suffix in (".eml", ".tmp"):
                path = self.email_dir / f"{safe_id}{suffix}"
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
