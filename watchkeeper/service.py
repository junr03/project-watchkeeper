"""Application behavior; dependencies are injectable at external boundaries."""

from datetime import datetime, timezone
import hashlib
import json
import uuid

from .db import Database
from .delivery import fingerprint
from .intake import Intake
from .policy import validate_proposal


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class WatchkeeperService:
    def __init__(self, database: Database, email_sink, clock=None, stale_after_seconds=3600):
        self.db = database
        self.email_sink = email_sink
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = stale_after_seconds
        self.intake = Intake(database, self.clock, stale_after_seconds)

    def ingest(self, batch: dict) -> dict:
        return self.intake.ingest(batch)

    def review(self, proposal: dict) -> dict:
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM observations").fetchall()
            obs = {row["observation_id"]: dict(row) for row in rows}
            sources = {row["source_id"]: dict(row) for row in c.execute("SELECT * FROM sources").fetchall()}
            assessment = validate_proposal(proposal, obs, now=self.clock(), sources=sources)
            evidence = [obs[ref] for ref in assessment["evidence_refs"]]
            evidence = [{k: row[k] for k in ("observation_id", "source_id", "thread_id", "message_id", "revision", "source_timestamp", "captured_at", "locator", "text")} for row in evidence]
            project = {**assessment, "evidence": evidence}
            fp = fingerprint(assessment)
            now = iso(self.clock())
            previous = c.execute("SELECT disposition,snooze_until,fingerprint FROM projects WHERE project_id=?", (assessment["project_id"],)).fetchone()
            disposition = previous["disposition"] if previous else "open"
            snooze_until = previous["snooze_until"] if previous else None
            if previous and previous["fingerprint"] != fp:
                c.execute("UPDATE outbox SET status='cancelled' WHERE project_id=? AND status='pending'",
                          (assessment["project_id"],))
            c.execute("""INSERT INTO projects(project_id,outcome,next_action,reasoning,evidence_json,fingerprint,disposition,snooze_until,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                outcome=excluded.outcome,next_action=excluded.next_action,reasoning=excluded.reasoning,
                evidence_json=excluded.evidence_json,fingerprint=excluded.fingerprint,updated_at=excluded.updated_at""",
                (assessment["project_id"], assessment["outcome"], assessment["next_action"], assessment["reasoning"],
                 self.db.json(evidence), fp, disposition, snooze_until, now))
            suppressed = disposition != "open" or (snooze_until is not None and snooze_until > now)
            delivery_id = None
            delivery_fp = fp if snooze_until is None else hashlib.sha256(f"{fp}:{snooze_until}".encode()).hexdigest()
            if not suppressed:
                existing = c.execute("SELECT delivery_id FROM outbox WHERE fingerprint=?", (delivery_fp,)).fetchone()
                if existing is None:
                    delivery_id = str(uuid.uuid4())
                    c.execute("INSERT INTO outbox VALUES(?,?,?,?,?,NULL)", (delivery_id, assessment["project_id"], delivery_fp, now, "pending"))
            c.commit()
        return {"project_id": assessment["project_id"], "status": "stored", "delivery_created": bool(delivery_id)}

    def review_input(self) -> dict:
        """Return a bounded, secret-free view for the dedicated reviewer role."""
        with self.db.connect() as c:
            observations = c.execute("""SELECT observation_id,source_id,thread_id,message_id,revision,
                source_timestamp,captured_at,locator,text FROM observations ORDER BY captured_at DESC LIMIT 500""").fetchall()
            projects = c.execute("SELECT project_id,outcome,next_action,disposition,snooze_until,updated_at FROM projects ORDER BY project_id").fetchall()
        return {"observations": [dict(row) for row in observations], "projects": [dict(row) for row in projects]}

    def deliver_pending(self) -> dict:
        """Deliver pending outbox rows through the configured fixed-recipient sink."""
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at,delivery_id").fetchall()
        delivered = 0
        for row in rows:
            with self.db.connect() as c:
                source = c.execute("SELECT * FROM projects WHERE project_id=?", (row["project_id"],)).fetchone()
                now = iso(self.clock())
                if (source is None or source["disposition"] != "open" or
                        (source["snooze_until"] is not None and source["snooze_until"] > now)):
                    c.execute("UPDATE outbox SET status='cancelled' WHERE delivery_id=? AND status='pending'", (row["delivery_id"],))
                    continue
            project = {"outcome": source["outcome"], "next_action": source["next_action"],
                       "reasoning": source["reasoning"], "evidence": json.loads(source["evidence_json"])}
            self.email_sink.deliver(row["delivery_id"], project)
            with self.db.connect() as c:
                c.execute("UPDATE outbox SET status='delivered', delivered_at=? WHERE delivery_id=? AND status='pending'",
                          (iso(self.clock()), row["delivery_id"]))
            delivered += 1
        return {"delivered": delivered}

    def snooze(self, project_id: str, until: str) -> dict:
        try:
            parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise ValueError("until must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed <= self.clock():
            raise ValueError("until must be a future timestamp with a timezone")
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            updated = c.execute("UPDATE projects SET snooze_until=?,updated_at=? WHERE project_id=? AND disposition='open'",
                                (iso(parsed), iso(self.clock()), project_id)).rowcount
            if updated:
                c.execute("UPDATE outbox SET status='cancelled' WHERE project_id=? AND status='pending'", (project_id,))
            c.commit()
        if not updated:
            raise KeyError("project not found or not open")
        return {"project_id": project_id, "snooze_until": iso(parsed)}

    def state(self) -> dict:
        now = self.clock()
        with self.db.connect() as c:
            projects = c.execute("SELECT * FROM projects ORDER BY project_id").fetchall()
        result = []
        for row in projects:
            item = dict(row)
            result.append({"project_id": item["project_id"], "outcome": item["outcome"], "next_action": item["next_action"],
                           "reasoning": item["reasoning"], "evidence": json.loads(item["evidence_json"]),
                           "disposition": item["disposition"], "snooze_until": item["snooze_until"], "updated_at": item["updated_at"]})
        return {"generated_at": iso(now), "sources": self.intake.state(), "projects": result}
