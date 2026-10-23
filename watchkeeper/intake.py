"""Observation persistence and source freshness boundary."""

from datetime import datetime, timezone
import hashlib

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

    def ingest(self, batch: dict) -> dict:
        required = {"source_id", "cursor", "available", "observations"}
        if not isinstance(batch, dict) or set(batch) != required:
            raise ValueError("intake requires source_id, cursor, available, and observations")
        source = batch["source_id"]
        items = batch["observations"]
        if (not isinstance(source, str) or not source or len(source) > 200 or
                not isinstance(batch["cursor"], str) or len(batch["cursor"]) > 2000 or
                type(batch["available"]) is not bool or not isinstance(items, list)):
            raise ValueError("invalid source or observations")
        captured = _iso(self.clock())
        inserted = 0
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
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
                oid = hashlib.sha256(f"{source}\0{item['thread_id']}\0{item['message_id']}".encode()).hexdigest()
                prior = c.execute("SELECT revision FROM observations WHERE observation_id=?", (oid,)).fetchone()
                if prior is None:
                    c.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
                        (oid, source, item["thread_id"], item["message_id"], item["revision"],
                         item["source_timestamp"], captured, item["locator"], item["text"]))
                    inserted += 1
                elif prior["revision"] != item["revision"]:
                    c.execute("""UPDATE observations SET revision=?,source_timestamp=?,captured_at=?,locator=?,text=?
                        WHERE observation_id=?""", (item["revision"], item["source_timestamp"], captured,
                        item["locator"], item["text"], oid))
                    inserted += 1
            c.execute("""INSERT INTO sources(source_id,cursor,last_seen_at,available,last_error)
                VALUES(?,?,?,?,NULL) ON CONFLICT(source_id) DO UPDATE SET
                cursor=CASE WHEN excluded.available=1 THEN excluded.cursor ELSE sources.cursor END,
                last_seen_at=excluded.last_seen_at,available=excluded.available,last_error=NULL""",
                (source, batch["cursor"], captured, int(bool(batch["available"]))))
            c.commit()
        return {"source_id": source, "stored": inserted, "cursor": batch["cursor"], "captured_at": captured}

    def state(self) -> list[dict]:
        now = self.clock()
        with self.db.connect() as c:
            source_rows = c.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
        sources = []
        for row in source_rows:
            item = dict(row)
            stale = not item["available"] or not item["last_seen_at"] or (
                now - datetime.fromisoformat(item["last_seen_at"])
            ).total_seconds() > self.stale_after_seconds
            sources.append({"source_id": item["source_id"], "cursor": item["cursor"],
                            "last_seen_at": item["last_seen_at"], "available": bool(item["available"]),
                            "freshness": "stale" if stale else "fresh"})
        return sources
