"""Durable, content-free scheduling and recovery state.

The scheduler stores identities, cursors and typed outcomes only. Review text is
assembled from the live observation store when a worker claims a lease.
"""

from datetime import datetime, timedelta, timezone
import json
import secrets
import hashlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _now(value):
    if value.tzinfo is None:
        raise ValueError("scheduler clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class MonitoringScheduler:
    def __init__(self, database, clock, *, timezone="UTC", review_time="08:00",
                 digest_time="08:15", lease_seconds=300, window_limit=100,
                 windows_per_day=4, window_byte_limit=180_000):
        try:
            self.zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("invalid scheduler time zone") from exc
        self._parse_time(review_time)
        self._parse_time(digest_time)
        self.db, self.clock = database, clock
        self.review_time, self.digest_time = review_time, digest_time
        self.lease_seconds = max(30, min(1800, int(lease_seconds)))
        if type(window_limit) is not int or not 1 <= window_limit <= 500:
            raise ValueError("review window limit must be between 1 and 500")
        if type(windows_per_day) is not int or not 2 <= windows_per_day <= 100:
            raise ValueError("daily review window count must be between 2 and 100")
        self.window_limit, self.windows_per_day = window_limit, windows_per_day
        if type(window_byte_limit) is not int or not 180_000 <= window_byte_limit <= 220_000:
            raise ValueError("review window byte limit must be between 180000 and 220000")
        self.window_byte_limit = window_byte_limit
        with self.db.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS monitoring_runs (
                    run_id TEXT PRIMARY KEY, schedule_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, cursor_json TEXT, snapshot_json TEXT,
                    lease_token TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    failure_kind TEXT, retry_after TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_progress (
                    stream TEXT PRIMARY KEY, cursor_json TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS health_incidents (
                    incident_id TEXT PRIMARY KEY, category TEXT NOT NULL,
                    subject TEXT NOT NULL, failure_kind TEXT NOT NULL,
                    status TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    occurrences INTEGER NOT NULL, signal_key TEXT NOT NULL,
                    notified_at TEXT, retry_after TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox_attempts (
                    attempt_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL, result TEXT NOT NULL,
                    attempted_at TEXT NOT NULL, UNIQUE(delivery_id,attempt_number)
                );
                CREATE TABLE IF NOT EXISTS digest_batches (
                    batch_id TEXT PRIMARY KEY, schedule_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, item_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS delivery_batches (
                    batch_id TEXT PRIMARY KEY, digest_id TEXT NOT NULL UNIQUE,
                    delivery_ids_json TEXT NOT NULL, notice_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_after TEXT
                );
            """)
            columns = {row[1] for row in c.execute("PRAGMA table_info(monitoring_runs)")}
            if "retry_after" not in columns:
                c.execute("ALTER TABLE monitoring_runs ADD COLUMN retry_after TEXT")
            health_columns = {row[1] for row in c.execute("PRAGMA table_info(health_incidents)")}
            if "retry_after" not in health_columns:
                c.execute("ALTER TABLE health_incidents ADD COLUMN retry_after TEXT")
            batch_columns = {row[1] for row in c.execute("PRAGMA table_info(delivery_batches)")}
            if "attempts" not in batch_columns:
                c.execute("ALTER TABLE delivery_batches ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "retry_after" not in batch_columns:
                c.execute("ALTER TABLE delivery_batches ADD COLUMN retry_after TEXT")

    @staticmethod
    def _parse_time(value):
        try:
            hour, minute = (int(part) for part in value.split(":"))
        except (ValueError, AttributeError) as exc:
            raise ValueError("schedule time must be HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("schedule time must be HH:MM")
        return hour, minute

    def due(self, kind):
        now = self.clock().astimezone(self.zone)
        h, m = self._parse_time(self.review_time if kind == "review" else self.digest_time)
        return now.hour * 60 + now.minute >= h * 60 + m

    def local_key(self, kind):
        return f"{kind}:{self.clock().astimezone(self.zone).date().isoformat()}"

    def ensure_daily(self):
        """Create at most one review and one digest record for the local day."""
        now = self.clock().astimezone(self.zone)
        stamp = _now(now)
        created = []
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if self.due("review"):
                daily_key = self.local_key("review")
                date_key = daily_key.split(":", 1)[1]
                # A device that wakes after several missed days resumes from
                # the last committed cursor instead of draining stale queued
                # windows in a burst.
                c.execute("UPDATE monitoring_runs SET status='superseded',snapshot_json=NULL,lease_token=NULL,lease_until=NULL WHERE schedule_key LIKE 'review:%' AND schedule_key NOT LIKE ? AND status IN ('pending','retry','leased')",
                          ("review:" + date_key + "%",))
                key = f"{daily_key}:recent"
                run_id = secrets.token_hex(16)
                c.execute("INSERT OR IGNORE INTO monitoring_runs(run_id,schedule_key,status,cursor_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                          (run_id, key, "pending", None, stamp, stamp))
                if c.execute("SELECT changes()").fetchone()[0]:
                    created.append(run_id)
            if self.due("digest"):
                key = self.local_key("digest")
                batch_id = secrets.token_hex(16)
                c.execute("INSERT OR IGNORE INTO digest_batches(batch_id,schedule_key,status,item_ids_json,created_at) VALUES(?,?,?,?,?)",
                          (batch_id, key, "pending", "[]", stamp))
                if c.execute("SELECT changes()").fetchone()[0]:
                    created.append(batch_id)
            c.commit()
        return created

    def claim(self, review_input_builder):
        """Lease one due/retryable review window and return its live snapshot."""
        self.ensure_daily()
        now = self.clock().astimezone(self.zone)
        stamp = _now(now)
        lease_until = _now(now + timedelta(seconds=self.lease_seconds))
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM monitoring_runs WHERE ((status IN ('pending','retry') AND (retry_after IS NULL OR retry_after<=?)) OR (status='leased' AND lease_until<=?)) ORDER BY created_at LIMIT 1",
                            (stamp, stamp)).fetchone()
            if row is None:
                c.commit()
                return None
            if row["status"] == "leased":
                c.execute("UPDATE monitoring_runs SET status='retry',retry_after=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE run_id=?",
                          (_now(now + timedelta(seconds=60)), stamp, row["run_id"]))
                c.commit()
                return None
            token = secrets.token_urlsafe(32)
            c.execute("UPDATE monitoring_runs SET status='leased',lease_token=?,lease_until=?,attempts=attempts+1,retry_after=NULL,updated_at=? WHERE run_id=?",
                      (token, lease_until, stamp, row["run_id"]))
            c.commit()
        cursor = json.loads(row["cursor_json"]) if row["cursor_json"] else None
        review_input = review_input_builder(cursor=cursor, limit=self.window_limit,
                                            byte_limit=self.window_byte_limit)
        refs = {item["observation_id"]: {"revision": item["revision"],
                                         "source_id": item["source_id"],
                                         "thread_id": item["thread_id"]}
                for item in review_input["observations"]}
        pairs = {(item["source_id"], item["thread_id"]) for item in review_input["observations"]}
        thread_guard = {}
        partial_threads = []
        with self.db.connect() as c:
            for source_id, thread_id in pairs:
                thread_rows = c.execute("SELECT observation_id,revision FROM reviewable_observations WHERE source_id=? AND thread_id=? ORDER BY observation_id,revision",
                                        (source_id, thread_id)).fetchall()
                thread_row = c.execute("SELECT COUNT(*) AS n,MAX(captured_at) AS captured,MAX(source_timestamp) AS source_time FROM reviewable_observations WHERE source_id=? AND thread_id=?",
                                       (source_id, thread_id)).fetchone()
                thread_fingerprint = hashlib.sha256(json.dumps(
                    [(item["observation_id"], item["revision"]) for item in thread_rows],
                    separators=(",", ":")).encode()).hexdigest()
                thread = review_input.get("sources", {}).get(source_id, {}).get("threads", {}).get(thread_id, {})
                thread_guard[f"{source_id}\0{thread_id}"] = {"count": thread_row["n"],
                    "captured_at": thread_row["captured"], "source_timestamp": thread_row["source_time"],
                    "fingerprint": thread_fingerprint,
                    "watermark_at": thread.get("last_progress_at") if isinstance(thread, dict) else None,
                    "watermark_revision": thread.get("last_progress_revision") if isinstance(thread, dict) else None,
                    "watermark_captured_at": thread.get("last_progress_captured_at") if isinstance(thread, dict) else None}
                if not isinstance(thread, dict) or thread.get("coverage_complete") is not True:
                    partial_threads.append([source_id, thread_id])
        schedule_parts = row["schedule_key"].split(":")
        stream = "history" if len(schedule_parts) > 2 and schedule_parts[2] == "history" else "recent"
        snapshot = {"stream": stream, "revisions": refs, "thread_guard": thread_guard,
                    "partial_threads": partial_threads,
                    "next_cursor": review_input.get("coverage", {}).get("next_cursor"),
                    "has_more": review_input.get("coverage", {}).get("backlog_remaining") is True}
        with self.db.connect() as c:
            c.execute("UPDATE monitoring_runs SET snapshot_json=?,updated_at=? WHERE run_id=? AND lease_token=?",
                      (json.dumps(snapshot, sort_keys=True), stamp, row["run_id"], token))
        return {"run_id": row["run_id"], "lease_token": token,
                "review_input": review_input, "cursor": cursor}

    def validate_lease(self, c, run_id, token, now):
        row = c.execute("SELECT * FROM monitoring_runs WHERE run_id=?", (run_id,)).fetchone()
        if (row is None or row["status"] != "leased" or row["lease_token"] != token or
                row["lease_until"] is None or row["lease_until"] <= _now(now)):
            return None
        return row

    def fail(self, run_id, token, failure_kind):
        if failure_kind not in {"quota", "authentication", "unavailable", "invalid_output", "protocol"}:
            raise ValueError("unsupported reviewer failure kind")
        stamp = _now(self.clock())
        with self.db.connect() as c:
            row = c.execute("SELECT attempts FROM monitoring_runs WHERE run_id=?", (run_id,)).fetchone()
            attempts = row["attempts"] if row else 1
            base = 900 if failure_kind in {"quota", "authentication"} else 60
            delay = min(21600, base * (2 ** min(max(attempts - 1, 0), 5)))
            retry_after = _now(self.clock() + timedelta(seconds=delay))
            c.execute("UPDATE monitoring_runs SET status='retry',failure_kind=?,retry_after=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE run_id=? AND status='leased' AND lease_token=? AND lease_until>?",
                      (failure_kind, retry_after, stamp, run_id, token, stamp))
        with self.db.connect() as c:
            row = c.execute("SELECT attempts FROM monitoring_runs WHERE run_id=?", (run_id,)).fetchone()
        attempt = row["attempts"] if row else 0
        self.record_failure("reviewer", "openclaw", failure_kind, f"{run_id}:{attempt}:{failure_kind}")

    def record_failure(self, category, subject, failure_kind, signal_key):
        stamp = _now(self.clock())
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM health_incidents WHERE category=? AND subject=? AND failure_kind=? AND status='open'",
                                 (category, subject, failure_kind)).fetchone()
            if existing:
                if existing["signal_key"] != signal_key:
                    occurrences = existing["occurrences"] + 1
                    base = 900 if failure_kind in {"quota", "authentication"} else 60
                    delay = min(21600, base * (2 ** min(occurrences - 1, 5)))
                    retry_after = _now(self.clock() + timedelta(seconds=delay))
                    c.execute("UPDATE health_incidents SET last_seen=?,occurrences=?,signal_key=?,retry_after=? WHERE incident_id=?",
                              (stamp, occurrences, signal_key, retry_after, existing["incident_id"]))
                else:
                    retry_after = existing["retry_after"]
            else:
                incident_id = secrets.token_hex(16)
                base = 900 if failure_kind in {"quota", "authentication"} else 60
                delay = base
                retry_after = _now(self.clock() + timedelta(seconds=delay))
                c.execute("INSERT INTO health_incidents(incident_id,category,subject,failure_kind,status,first_seen,last_seen,occurrences,signal_key,notified_at,retry_after) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (incident_id, category, subject, failure_kind, "open", stamp, stamp, 1, signal_key, None, retry_after))
            c.commit()
        retry_at = datetime.fromisoformat(retry_after.replace("Z", "+00:00")) if retry_after else self.clock()
        return {"retry_after_seconds": max(0, int((retry_at - self.clock().astimezone(timezone.utc)).total_seconds()))}

    def recover(self, category, subject):
        stamp = _now(self.clock())
        with self.db.connect() as c:
            c.execute("UPDATE health_incidents SET status='resolved',last_seen=?,retry_after=NULL WHERE category=? AND subject=? AND status='open'",
                      (stamp, category, subject))

    def health_backoff_seconds(self, category, subject):
        now = self.clock().astimezone(timezone.utc)
        with self.db.connect() as c:
            rows = c.execute("SELECT retry_after FROM health_incidents WHERE category=? AND subject=? AND status='open' AND retry_after IS NOT NULL",
                             (category, subject)).fetchall()
        delays = []
        for row in rows:
            try:
                retry_at = datetime.fromisoformat(row["retry_after"].replace("Z", "+00:00")).astimezone(timezone.utc)
                delays.append(max(0, int((retry_at - now).total_seconds())))
            except (TypeError, ValueError):
                continue
        return {"retry_after_seconds": max(delays, default=0)}

    def complete(self, run_id, token, now, next_cursor, *, has_more):
        stamp = _now(now)
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self.validate_lease(c, run_id, token, now)
            if row is None:
                c.rollback()
                return False
            key = row["schedule_key"]
            snapshot = json.loads(row["snapshot_json"] or "{}")
            stream = snapshot.get("stream", "history")
            progress = c.execute("SELECT cursor_json FROM review_progress WHERE stream='history'").fetchone()
            history_cursor = progress[0] if progress else None
            if stream == "history":
                history_cursor = json.dumps(next_cursor, sort_keys=True) if has_more and next_cursor else None
                c.execute("INSERT INTO review_progress(stream,cursor_json,updated_at) VALUES('history',?,?) ON CONFLICT(stream) DO UPDATE SET cursor_json=excluded.cursor_json,updated_at=excluded.updated_at",
                          (history_cursor, stamp))
            elif history_cursor is None and has_more and next_cursor:
                history_cursor = json.dumps(next_cursor, sort_keys=True)
                c.execute("INSERT INTO review_progress(stream,cursor_json,updated_at) VALUES('history',?,?) ON CONFLICT(stream) DO UPDATE SET cursor_json=excluded.cursor_json,updated_at=excluded.updated_at",
                          (history_cursor, stamp))
            c.execute("UPDATE monitoring_runs SET status='complete',cursor_json=?,snapshot_json=NULL,lease_token=NULL,lease_until=NULL,failure_kind=NULL,updated_at=? WHERE run_id=?",
                      (None, stamp, run_id))
            date_key = key.split(":", 2)[1]
            daily_prefix = f"review:{date_key}:"
            total_count = c.execute("SELECT COUNT(*) FROM monitoring_runs WHERE schedule_key LIKE ?",
                                    (daily_prefix + "%",)).fetchone()[0]
            history_count = c.execute("SELECT COUNT(*) FROM monitoring_runs WHERE schedule_key LIKE ?",
                                      (daily_prefix + "history:%",)).fetchone()[0]
            if history_cursor and total_count < self.windows_per_day:
                page_key = f"{daily_prefix}history:{history_count}"
                c.execute("INSERT OR IGNORE INTO monitoring_runs(run_id,schedule_key,status,cursor_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                          (secrets.token_hex(16), page_key, "pending", history_cursor, stamp, stamp))
            c.commit()
        self.recover("reviewer", "openclaw")
        return True
