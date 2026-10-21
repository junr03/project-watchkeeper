"""Observation persistence, source coverage, and freshness boundary."""

from datetime import datetime, timezone
import hashlib
import json

from .db import Database


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class Intake:
    def __init__(self, database: Database, clock=None, stale_after_seconds=3600):
        self.db = database
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = stale_after_seconds
        # Intake owns this additive schema. The base application schema remains
        # unchanged so the source adapter can be shipped independently.
        with self.db.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS watchkeeper_source_coverage (
                source_id TEXT PRIMARY KEY,
                coverage_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS watchkeeper_thread_coverage (
                source_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                completeness TEXT NOT NULL,
                activity TEXT NOT NULL DEFAULT 'unknown',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source_id,thread_id)
            )""")

    def ingest(self, batch: dict) -> dict:
        required = {"source_id", "cursor", "available", "observations"}
        optional = {"coverage", "thread_coverage"}
        if not isinstance(batch, dict) or not required.issubset(batch) or set(batch) - required - optional:
            raise ValueError("intake requires source_id, cursor, available, observations, and optional coverage")
        source = batch["source_id"]
        items = batch["observations"]
        if (not isinstance(source, str) or not source or len(source) > 200 or
                not isinstance(batch["cursor"], str) or len(batch["cursor"]) > 2000 or
                type(batch["available"]) is not bool or not isinstance(items, list) or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in source)):
            raise ValueError("invalid source or observations")
        coverage = batch.get("coverage")
        if coverage is not None:
            coverage = self._coverage(coverage)
        thread_coverage = batch.get("thread_coverage", [])
        if not isinstance(thread_coverage, list) or len(thread_coverage) > 500:
            raise ValueError("thread_coverage must be a bounded list")
        for record in thread_coverage:
            if (not isinstance(record, dict) or set(record) != {"thread_id", "completeness", "activity"} or
                    not isinstance(record["thread_id"], str) or not record["thread_id"] or len(record["thread_id"]) > 500 or
                    any(ord(ch) < 32 or ord(ch) == 127 for ch in record["thread_id"]) or
                    record["completeness"] not in ("complete", "partial", "unknown") or
                    record["activity"] not in ("unknown", "observed-running", "observed-not-running")):
                raise ValueError("invalid per-thread coverage record")
        captured = _iso(self.clock())
        inserted = 0
        expired_replays_suppressed = 0
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            has_tombstones = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='expired_observations'").fetchone() is not None
            for item in items:
                keys = ("thread_id", "message_id", "revision", "source_timestamp", "locator", "text")
                if not isinstance(item, dict) or set(item) != set(keys):
                    raise ValueError("each observation must include identity, revision, provenance, locator, and text")
                limits = {"thread_id": 500, "message_id": 500, "revision": 200, "source_timestamp": 80,
                          "locator": 2000, "text": 20000}
                if any(not isinstance(item[k], str) or not item[k] or len(item[k]) > limits[k] for k in keys):
                    raise ValueError("invalid observation field or text size")
                try:
                    timestamp = datetime.fromisoformat(item["source_timestamp"].replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("source_timestamp must be an ISO-8601 timestamp") from exc
                if timestamp.tzinfo is None:
                    raise ValueError("source_timestamp must include a timezone")
                if any(ord(ch) < 32 or ord(ch) == 127 for ch in source + item["thread_id"] + item["message_id"]):
                    raise ValueError("source and observation IDs cannot contain control characters")
                oid = hashlib.sha256(f"{source}\0{item['thread_id']}\0{item['message_id']}".encode()).hexdigest()
                if has_tombstones and c.execute(
                        "SELECT 1 FROM expired_observations WHERE observation_id=? AND revision=?",
                        (oid, item["revision"])).fetchone():
                    expired_replays_suppressed += 1
                    continue
                excluded_source = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='excluded_sources'").fetchone()
                if excluded_source and c.execute("SELECT 1 FROM excluded_sources WHERE source_id=?", (source,)).fetchone():
                    continue
                excluded_thread = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='excluded_threads'").fetchone()
                if excluded_thread and c.execute("SELECT 1 FROM excluded_threads WHERE source_id=? AND thread_id=?", (source, item["thread_id"])).fetchone():
                    continue
                prior = c.execute("SELECT revision FROM observations WHERE observation_id=?", (oid,)).fetchone()
                if prior is None:
                    c.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
                        (oid, source, item["thread_id"], item["message_id"], item["revision"],
                         item["source_timestamp"], captured, item["locator"], item["text"]))
                    inserted += 1
                elif prior["revision"] != item["revision"] and not _older_revision(item["revision"], prior["revision"]):
                    c.execute("""UPDATE observations SET revision=?,source_timestamp=?,captured_at=?,locator=?,text=?
                        WHERE observation_id=?""", (item["revision"], item["source_timestamp"], captured,
                        item["locator"], item["text"], oid))
                    inserted += 1
            c.execute("""INSERT INTO sources(source_id,cursor,last_seen_at,available,last_error)
                VALUES(?,?,?,?,NULL) ON CONFLICT(source_id) DO UPDATE SET
                cursor=CASE WHEN excluded.available=1 THEN excluded.cursor ELSE sources.cursor END,
                last_seen_at=excluded.last_seen_at,available=excluded.available,last_error=NULL""",
                (source, batch["cursor"], captured, int(bool(batch["available"]))))
            if coverage is not None:
                c.execute("""INSERT INTO watchkeeper_source_coverage(source_id,coverage_json,updated_at)
                    VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                    coverage_json=excluded.coverage_json,updated_at=excluded.updated_at""",
                    (source, json.dumps(coverage, sort_keys=True), captured))
            for record in thread_coverage:
                excluded_source = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='excluded_sources'").fetchone()
                if excluded_source and c.execute("SELECT 1 FROM excluded_sources WHERE source_id=?", (source,)).fetchone():
                    continue
                excluded_thread = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='excluded_threads'").fetchone()
                if excluded_thread and c.execute("SELECT 1 FROM excluded_threads WHERE source_id=? AND thread_id=?", (source, record["thread_id"])).fetchone():
                    continue
                c.execute("""INSERT INTO watchkeeper_thread_coverage(source_id,thread_id,completeness,activity,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(source_id,thread_id) DO UPDATE SET
                    completeness=excluded.completeness,activity=excluded.activity,updated_at=excluded.updated_at""",
                    (source, record["thread_id"], record["completeness"], record["activity"], captured))
            c.commit()
            persisted = c.execute("SELECT cursor FROM sources WHERE source_id=?", (source,)).fetchone()["cursor"]
        return {"source_id": source, "stored": inserted, "cursor": persisted, "captured_at": captured,
                "expired_replays_suppressed": expired_replays_suppressed}

    @staticmethod
    def _coverage(value):
        fields = {"state", "threads_listed", "threads_read", "messages_read", "scan_limit",
                  "archived_scanned", "message_completeness", "current_activity", "gaps", "source_version", "failure"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("coverage must include the complete coverage report")
        if value["state"] not in ("complete", "partial", "unknown"):
            raise ValueError("coverage state must be complete, partial, or unknown")
        for key in ("threads_listed", "threads_read", "messages_read", "scan_limit"):
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError(f"coverage {key} must be a non-negative integer")
        if type(value["archived_scanned"]) is not bool:
            raise ValueError("coverage archived_scanned must be boolean")
        if value["message_completeness"] not in ("full", "partial", "unknown"):
            raise ValueError("invalid message completeness")
        if value["current_activity"] not in ("unknown", "observed-running", "observed-not-running"):
            raise ValueError("invalid current activity state")
        if value["failure"] not in ("none", "authentication", "quota", "unsupported", "unavailable", "protocol"):
            raise ValueError("invalid collection failure kind")
        if not isinstance(value["gaps"], list) or len(value["gaps"]) > 50 or any(not isinstance(g, str) or len(g) > 500 for g in value["gaps"]):
            raise ValueError("coverage gaps must be a bounded list of strings")
        if not isinstance(value["source_version"], str) or len(value["source_version"]) > 100:
            raise ValueError("invalid source version")
        return value

    def state(self) -> list[dict]:
        now = self.clock()
        with self.db.connect() as c:
            source_rows = c.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
            coverage_rows = c.execute("SELECT source_id,coverage_json FROM watchkeeper_source_coverage").fetchall()
            thread_rows = c.execute("SELECT source_id,thread_id,completeness,activity FROM watchkeeper_thread_coverage ORDER BY thread_id").fetchall()
        coverage_by_source = {r["source_id"]: json.loads(r["coverage_json"]) for r in coverage_rows}
        threads_by_source = {}
        for row in thread_rows:
            threads_by_source.setdefault(row["source_id"], {})[row["thread_id"]] = {
                "completeness": row["completeness"], "activity": row["activity"],
                "active": row["activity"] == "observed-running",
                "status": "active" if row["activity"] == "observed-running" else
                          "idle" if row["activity"] == "observed-not-running" else "unknown"}
        sources = []
        for row in source_rows:
            item = dict(row)
            stale = not item["available"] or not item["last_seen_at"] or (
                now - datetime.fromisoformat(item["last_seen_at"])
            ).total_seconds() > self.stale_after_seconds
            coverage = coverage_by_source.get(item["source_id"], {
                "state": "unknown", "threads_listed": 0, "threads_read": 0, "messages_read": 0,
                "scan_limit": 0, "archived_scanned": False, "message_completeness": "unknown",
                "current_activity": "unknown", "gaps": ["No collection report received"], "source_version": "unknown",
                "failure": "none"})
            thread_state = threads_by_source.get(item["source_id"], {})
            incomplete_threads = [thread_id for thread_id, record in thread_state.items()
                                  if record["completeness"] != "complete"]
            if incomplete_threads:
                coverage = dict(coverage)
                coverage["state"] = "partial"
                coverage["message_completeness"] = "partial"
                gaps = list(coverage.get("gaps", []))
                for thread_id in incomplete_threads:
                    note = f"Transcript details incomplete for thread {thread_id}"
                    if note not in gaps and len(gaps) < 50:
                        gaps.append(note)
                coverage["gaps"] = gaps
            sources.append({"source_id": item["source_id"], "cursor": item["cursor"],
                            "last_seen_at": item["last_seen_at"], "available": bool(item["available"]),
                            "freshness": "stale" if stale else "fresh", "coverage": coverage,
                            "threads": thread_state})
        return sources

    def context(self) -> dict:
        """Trusted source facts for deterministic policy; activity defaults unknown."""
        values = {}
        for source in self.state():
            values[source["source_id"]] = {
                "available": source["available"], "freshness": source["freshness"],
                "last_seen_at": source["last_seen_at"],
                "activity": source["coverage"].get("current_activity", "unknown"),
                "coverage_state": source["coverage"].get("state", "unknown"),
                "coverage": source["coverage"],
                "threads": source["threads"],
            }
        return values


def _older_revision(incoming: str, current: str) -> bool:
    """Compare revisions when the source provides a timestamp/sequence prefix.

    Opaque tokens remain last-writer-wins; Codex revisions prefix a stable
    source message timestamp so a delayed older page cannot roll back a newer
    observation.
    """
    import re
    def key(value):
        if value.isdigit():
            return (2, int(value))
        match = re.match(r"^(\d{4}-\d\d-\d\dT[^|]+)\|", value)
        if match:
            return (1, match.group(1))
        return None
    left, right = key(incoming), key(current)
    if left is None or right is None or left[0] != right[0]:
        return False
    # Timestamp-prefixed revisions can be ordered without interpreting the
    # opaque content digest. Equal timestamps are accepted as a real edit.
    return left[1] < right[1]
