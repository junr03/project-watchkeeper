"""Application behavior; dependencies are injectable at external boundaries."""

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from .db import Database
from .delivery import fingerprint
from .intake import Intake
from .policy import validate_proposal
from .privacy import PrivacyManager


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class WatchkeeperService:
    def __init__(self, database: Database, email_sink, clock=None, stale_after_seconds=3600,
                 inferred_after_days=3, transcript_retention_days=30, privacy_journal=None,
                 privacy_purge_hooks=None, captured_email_dir=None):
        self.db = database
        self.email_sink = email_sink
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = stale_after_seconds
        self.inferred_after_days = inferred_after_days
        self.intake = Intake(database, self.clock, stale_after_seconds)
        self.privacy = PrivacyManager(database,
            privacy_journal or f"{database.path}.privacy.jsonl", captured_email_dir,
            transcript_retention_days, self.clock, purge_hooks=privacy_purge_hooks)
        self.privacy.replay()
        self.privacy.expire()

    def ingest(self, batch: dict) -> dict:
        self.privacy.expire()
        required = {"source_id", "cursor", "available", "observations"}
        optional = {"coverage", "thread_coverage"}
        if (isinstance(batch, dict) and required <= set(batch) and set(batch) <= required | optional and
                isinstance(batch.get("observations"), list) and
                all(isinstance(item, dict) and all(isinstance(item.get(k), str)
                    for k in ("thread_id", "message_id", "revision")) for item in batch["observations"])):
            batch, excluded = self.privacy.filter_excluded(batch)
            batch, suppressed = self.privacy.filter_expired_replays(batch)
        else:
            suppressed = 0
            excluded = 0
        result = self.intake.ingest(batch)
        self.privacy.refresh_watermarks()
        result["expired_replays_suppressed"] = suppressed + result.get("expired_replays_suppressed", 0)
        result["excluded_observations_skipped"] = excluded
        return result

    def expire_retention(self):
        """Public maintenance hook for the later daily scheduler."""
        return self.privacy.expire()

    def deletion_preview(self, project_id):
        return self.privacy.preview(project_id)

    def delete_project(self, project_id, impact_token, *, confirmed):
        return self.privacy.delete_project(project_id, impact_token, confirmed=confirmed)

    def _excluded(self, c, source_id, thread_id):
        return (c.execute("SELECT 1 FROM excluded_sources WHERE source_id=?", (source_id,)).fetchone() is not None or
                c.execute("SELECT 1 FROM excluded_threads WHERE source_id=? AND thread_id=?",
                          (source_id, thread_id)).fetchone() is not None)

    def _source_context(self):
        """Normalize adapter facts into the policy's trusted source shape."""
        if hasattr(self.intake, "context"):
            context = self.intake.context()
            values = context.get("sources", context) if isinstance(context, dict) else {}
        else:
            with self.db.connect() as c:
                values = {row["source_id"]: dict(row) for row in c.execute("SELECT * FROM sources").fetchall()}
        if not isinstance(values, dict):
            return {}
        normalized = {}
        for source_id, value in values.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            coverage = item.get("coverage", {})
            if not isinstance(coverage, dict):
                coverage = {}
            complete = (coverage.get("state", item.get("coverage_state")) == "complete" and
                        coverage.get("message_completeness") == "full" and
                        coverage.get("archived_scanned") is True and coverage.get("gaps") == [])
            activity = coverage.get("current_activity", item.get("activity", "unknown"))
            supplied_threads = item.get("threads", {})
            if not isinstance(supplied_threads, dict):
                supplied_threads = {}
            with self.db.connect() as c:
                thread_ids = {r[0] for r in c.execute("SELECT thread_id FROM observations WHERE source_id=? UNION "
                                                       "SELECT thread_id FROM thread_progress_watermarks WHERE source_id=?",
                                                       (source_id, source_id))}
                thread_ids.update(supplied_threads)
                watermarks = {r["thread_id"]: dict(r) for r in c.execute(
                    "SELECT thread_id,last_progress_at,revision,last_captured_at FROM thread_progress_watermarks WHERE source_id=?",
                    (source_id,))}
            trusted_threads = {}
            for thread_id in thread_ids:
                thread = supplied_threads.get(thread_id, {})
                thread = dict(thread) if isinstance(thread, dict) else {"activity": thread}
                thread_complete = thread.get("coverage_complete")
                if thread_complete is None:
                    # Per-thread metadata is more specific than aggregate
                    # source coverage, including an explicit partial/unknown
                    # report from an otherwise complete scan.
                    if "completeness" in thread:
                        thread_complete = thread.get("completeness") == "complete"
                    else:
                        thread_complete = complete
                thread_activity = thread.get("activity", activity)
                thread["coverage_complete"] = bool(thread_complete)
                thread.setdefault("gaps", [] if thread_complete else ["Source coverage is incomplete"])
                thread["active"] = thread.get("active") is True or thread.get("status") in {"active", "running", "in_progress"} or thread_activity == "observed-running"
                watermark = watermarks.get(thread_id)
                if watermark:
                    thread["last_progress_at"] = watermark["last_progress_at"]
                    thread["last_progress_revision"] = watermark["revision"]
                    thread["last_progress_captured_at"] = watermark["last_captured_at"]
                trusted_threads[thread_id] = thread
            item["threads"] = trusted_threads
            normalized[source_id] = item
        return normalized

    def review(self, proposal: dict) -> dict:
        self.privacy.expire()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM observations").fetchall()
            all_obs = {row["observation_id"]: dict(row) for row in rows}
            sources = self._source_context()
            # Exclusions are enforced here as well as at intake, so already stored
            # observations cannot be smuggled back into a review proposal.
            obs = {oid: row for oid, row in all_obs.items()
                   if not self._excluded(c, row["source_id"], row["thread_id"])}
            expired_ids = {r[0] for r in c.execute("SELECT observation_id FROM expired_observations")}
            retained_projects = c.execute("""SELECT evidence_json FROM projects WHERE disposition='open'
                AND archived=0 AND (snooze_until IS NULL OR snooze_until<=?)""", (iso(self.clock()),)).fetchall()
            for project in retained_projects:
                for item in json.loads(project["evidence_json"]):
                    oid = item.get("observation_id")
                    if (oid in expired_ids and oid not in obs and
                            not self._excluded(c, item.get("source_id"), item.get("thread_id"))):
                        obs[oid] = item
            assessment = validate_proposal(proposal, obs, now=self.clock(), sources=sources,
                                           inferred_after_days=self.inferred_after_days,
                                           source_stale_after_seconds=self.stale_after_seconds)
            evidence_rows = [obs[ref] for ref in assessment["evidence_refs"]]
            owners = {r["project_id"] for r in c.execute("SELECT source_id,thread_id,project_id FROM thread_ownership").fetchall()
                      if any(e["source_id"] == r["source_id"] and e["thread_id"] == r["thread_id"] for e in evidence_rows)}
            associations = c.execute("""SELECT t.source_id,t.thread_id,p.project_id,p.disposition,p.archived,p.snooze_until
                FROM project_threads t JOIN projects p ON p.project_id=t.project_id""").fetchall()
            related = [row for row in associations if any(
                e["source_id"] == row["source_id"] and e["thread_id"] == row["thread_id"] for e in evidence_rows)]
            requested_id = assessment["project_id"]
            owner_mismatch = any(owner != requested_id for owner in owners)
            grouping_conflict = len(owners) > 1
            hidden_alias = any(row["project_id"] != requested_id and
                               (row["disposition"] != "open" or row["archived"] or
                                (row["snooze_until"] is not None and row["snooze_until"] > iso(self.clock())))
                               for row in related)
            evidence = [{k: row[k] for k in ("observation_id", "source_id", "thread_id", "message_id", "revision",
                                               "source_timestamp", "captured_at", "locator", "text")}
                        for row in evidence_rows]
            previous = c.execute("SELECT * FROM projects WHERE project_id=?", (requested_id,)).fetchone()
            archived = bool(previous["archived"]) if previous else False
            disposition = previous["disposition"] if previous else "open"
            snoozed = previous is not None and previous["snooze_until"] is not None and previous["snooze_until"] > iso(self.clock())
            if hidden_alias:
                c.commit()
                return {"project_id": requested_id, "status": "suppressed", "kind": "proposal",
                        "eligible": False, "eligibility_reason": "evidence belongs to a closed, archived, or snoozed project",
                        "delivery_created": False}
            if previous is not None and (archived or disposition != "open" or snoozed):
                c.commit()
                reason = ("project is archived" if archived else f"project is {disposition}" if disposition != "open"
                          else "project is snoozed")
                return {"project_id": requested_id, "status": "suppressed", "kind": "proposal",
                        "eligible": False, "eligibility_reason": reason, "delivery_created": False}
            reason = assessment.get("eligibility_reason", "")
            eligible = bool(assessment.get("eligible", assessment.get("kind") == "attention"))
            kind = assessment.get("kind", "attention")
            # User grouping is authoritative. Persist a conflicting inference
            # separately so it cannot rewrite either established project.
            if owner_mismatch or grouping_conflict:
                reason = ("evidence is already assigned by a user to another project" if owner_mismatch else
                          "evidence spans projects with different user-assigned owners")
                self._save_review_proposal(c, requested_id, assessment, evidence, reason)
                c.commit()
                return {"project_id": requested_id, "status": "stored", "kind": "proposal",
                        "eligible": False, "eligibility_reason": reason, "delivery_created": False}
            project_id = requested_id
            # A lifecycle decision is authoritative even for hostile proposals.
            locked = previous is not None and disposition in ("completed", "abandoned")
            if archived:
                kind, eligible = "proposal", False
                reason = "project is archived; restore it before reviewing new evidence"
            if locked:
                kind, eligible = "proposal", False
                reason = f"project is {disposition}; reopen it before applying new evidence"
            if snoozed:
                kind, eligible = "proposal", False
                reason = "project is snoozed; reassess after its expiry"
            if kind != "attention" or not eligible:
                self._save_review_proposal(c, requested_id, assessment, evidence,
                                           reason or assessment.get("eligibility_reason", "assessment is not eligible for attention"))
            fp = fingerprint(assessment)
            now = iso(self.clock())
            delivery_created = False
            if kind == "attention" and eligible and not (archived or locked or snoozed):
                if previous and previous["fingerprint"] != fp:
                    c.execute("UPDATE outbox SET status='cancelled' WHERE project_id=? AND status='pending'", (project_id,))
                c.execute("""INSERT INTO projects(project_id,outcome,next_action,reasoning,evidence_json,fingerprint,
                    disposition,snooze_until,updated_at,archived,assessment_kind,eligible,eligibility_reason,assessed_at,
                    status,grouping_confidence,last_progress_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                    outcome=excluded.outcome,next_action=excluded.next_action,reasoning=excluded.reasoning,
                    evidence_json=excluded.evidence_json,fingerprint=excluded.fingerprint,
                    assessment_kind=excluded.assessment_kind,eligible=excluded.eligible,
                    eligibility_reason=excluded.eligibility_reason,assessed_at=excluded.assessed_at,
                    status=excluded.status,grouping_confidence=excluded.grouping_confidence,
                    last_progress_at=excluded.last_progress_at,
                    updated_at=excluded.updated_at""",
                    (project_id, assessment["outcome"], assessment["next_action"], assessment["reasoning"],
                     self.db.json(evidence), fp, disposition, previous["snooze_until"] if previous else None,
                     now, int(archived), kind, int(eligible), reason or "", now,
                     assessment.get("status", "explicit_request"), assessment.get("grouping_confidence", "strong"),
                     assessment.get("last_progress_at")))
            elif previous and not eligible and not (archived or locked or snoozed) and not (
                    assessment.get("status") in {"uncertain_completion", "uncertain_grouping"} or
                    assessment.get("grouping_confidence") == "uncertain"):
                # A current active-work/freshness result revokes delivery
                # eligibility without replacing the last concrete finding.
                # Uncertain grouping/completion remains a separate proposal and
                # does not cancel a stable existing attention item.
                c.execute("UPDATE outbox SET status='cancelled' WHERE project_id=? AND status='pending'", (project_id,))
                c.execute("""UPDATE projects SET assessment_kind=?,eligible=0,
                    eligibility_reason=?,assessed_at=?,status=?,grouping_confidence=?,
                    last_progress_at=?,updated_at=? WHERE project_id=?""",
                          (kind, reason or "assessment is not eligible for attention", now,
                           assessment.get("status", previous["status"]),
                           assessment.get("grouping_confidence", previous["grouping_confidence"]),
                           assessment.get("last_progress_at", previous["last_progress_at"]), now, project_id))

            # Claim threads only when this is an actionable, stable grouping.
            # Existing ownership can be changed only through an authenticated user action.
            if kind == "attention" and eligible and not archived and not locked:
                for row in evidence_rows:
                    c.execute("INSERT OR IGNORE INTO project_threads(source_id,thread_id,project_id,assigned_at) VALUES(?,?,?,?)",
                              (row["source_id"], row["thread_id"], project_id, now))
            suppress = (kind != "attention" or not eligible or archived or disposition != "open")
            snooze_until = previous["snooze_until"] if previous else None
            # Expiry alone is not authorization to notify: this review is the
            # fresh assessment that makes a post-expiry finding eligible again.
            suppress = suppress or (snooze_until is not None and snooze_until > now)
            delivery_fp = fp if snooze_until is None else hashlib.sha256(f"{fp}:{snooze_until}".encode()).hexdigest()
            if not suppress:
                existing = c.execute("SELECT delivery_id FROM outbox WHERE fingerprint=?", (delivery_fp,)).fetchone()
                if existing is None:
                    delivery_id = str(uuid.uuid4())
                    c.execute("INSERT INTO outbox VALUES(?,?,?,?,?,NULL)",
                              (delivery_id, project_id, delivery_fp, now, "pending"))
                    delivery_created = True
            c.commit()
        return {"project_id": project_id, "status": "stored", "kind": kind,
                "eligible": eligible, "eligibility_reason": reason or None,
                "delivery_created": delivery_created}

    def _save_review_proposal(self, c, project_id, assessment, evidence, reason):
        c.execute("""INSERT INTO review_proposals(proposal_id,requested_project_id,outcome,next_action,reasoning,
                   evidence_json,status,grouping_confidence,reason,created_at)
                   SELECT ?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (
                     SELECT 1 FROM review_proposals WHERE requested_project_id=? AND outcome=? AND next_action=?
                       AND evidence_json=? AND status=? AND grouping_confidence=? AND reason=?)""",
                  (str(uuid.uuid4()), project_id, assessment["outcome"], assessment["next_action"],
                   assessment["reasoning"], self.db.json(evidence), assessment.get("status", "uncertain_grouping"),
                   assessment.get("grouping_confidence", "uncertain"), reason, iso(self.clock()),
                   project_id, assessment["outcome"], assessment["next_action"], self.db.json(evidence),
                   assessment.get("status", "uncertain_grouping"), assessment.get("grouping_confidence", "uncertain"), reason))

    def review_input(self) -> dict:
        """Return observations eligible for review and a secret-free register view."""
        sources = self._source_context()
        with self.db.connect() as c:
            rows = c.execute("""SELECT DISTINCT o.observation_id,o.source_id,o.thread_id,o.message_id,o.revision,
                o.source_timestamp,o.captured_at,o.locator,o.text
                FROM observations o LEFT JOIN project_threads t ON
                  t.source_id=o.source_id AND t.thread_id=o.thread_id
                WHERE NOT EXISTS (SELECT 1 FROM excluded_sources x WHERE x.source_id=o.source_id)
                  AND NOT EXISTS (SELECT 1 FROM excluded_threads x WHERE x.source_id=o.source_id AND x.thread_id=o.thread_id)
                  AND (t.project_id IS NULL OR EXISTS (SELECT 1 FROM project_threads live_t
                       JOIN projects p ON p.project_id=live_t.project_id
                       WHERE live_t.source_id=o.source_id AND live_t.thread_id=o.thread_id
                         AND p.disposition='open' AND p.archived=0
                         AND (p.snooze_until IS NULL OR p.snooze_until<=?)))
                ORDER BY o.captured_at DESC LIMIT 501""", (iso(self.clock()),)).fetchall()
            projects = c.execute("""SELECT project_id,outcome,next_action,disposition,snooze_until,archived,
                assessment_kind,eligible,eligibility_reason,updated_at,evidence_json FROM projects WHERE disposition='open'
                AND archived=0 AND (snooze_until IS NULL OR snooze_until<=?) ORDER BY project_id""",
                (iso(self.clock()),)).fetchall()
            proposals = c.execute("SELECT proposal_id,requested_project_id,outcome,next_action,reasoning,evidence_json,status,grouping_confidence,reason,created_at FROM review_proposals ORDER BY created_at DESC LIMIT 100").fetchall()
        has_more = len(rows) > 500
        rows = rows[:500]
        visible = {row["observation_id"] for row in rows}
        active_projects = []
        for row in projects:
            project = dict(row)
            evidence = json.loads(project.pop("evidence_json"))
            retained = [item for item in evidence if item.get("observation_id") not in visible and
                        not self._excluded_from_context(item)]
            project["retained_evidence"] = retained
            active_projects.append(project)
            for item in retained:
                if len(rows) >= 500:
                    has_more = True
                    continue
                rows.append(item)
                visible.add(item.get("observation_id"))
        return {"observations": [dict(row) for row in rows], "projects": active_projects,
                "proposals": [{**{k: v for k, v in dict(row).items() if k != "evidence_json"},
                               "evidence": json.loads(row["evidence_json"])} for row in proposals],
                "sources": sources, "current_time": iso(self.clock()),
                "coverage": {"observation_limit": 500, "observations_truncated": has_more,
                             "next_before": rows[-1]["captured_at"] if has_more and rows else None}}

    def _excluded_from_context(self, evidence):
        with self.db.connect() as c:
            return self._excluded(c, evidence.get("source_id"), evidence.get("thread_id"))

    def deliver_pending(self) -> dict:
        """Deliver pending outbox rows through the configured fixed-recipient sink."""
        self.privacy.expire()
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at,delivery_id").fetchall()
        delivered = 0
        for row in rows:
            with self.db.connect() as c:
                source = c.execute("SELECT * FROM projects WHERE project_id=?", (row["project_id"],)).fetchone()
                now = iso(self.clock())
                if (source is None or source["disposition"] != "open" or source["archived"] or not source["eligible"] or
                        source["assessment_kind"] != "attention" or
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

    def _project(self, c, project_id):
        row = c.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError("project not found")
        return row

    def decide(self, project_id: str, action: str, *, until=None) -> dict:
        now = iso(self.clock())
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self._project(c, project_id)
            disposition, archived, snooze_until = row["disposition"], row["archived"], row["snooze_until"]
            if action in ("complete", "abandon"):
                disposition = "completed" if action == "complete" else "abandoned"
                snooze_until = None
            elif action == "reopen":
                disposition, snooze_until = "open", None
            elif action == "archive":
                archived = 1
            elif action == "restore":
                archived = 0
            elif action == "snooze":
                try:
                    parsed = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("until must be an ISO-8601 timestamp") from exc
                if parsed.tzinfo is None or parsed <= self.clock():
                    raise ValueError("until must be a future timestamp with a timezone")
                if disposition != "open":
                    raise ValueError("only an open project can be snoozed")
                if archived:
                    raise ValueError("restore the project before snoozing")
                snooze_until = iso(parsed)
            else:
                raise ValueError("unknown project action")
            c.execute("UPDATE projects SET disposition=?,archived=?,snooze_until=?,updated_at=? WHERE project_id=?",
                      (disposition, archived, snooze_until, now, project_id))
            if action in ("reopen", "restore"):
                c.execute("UPDATE projects SET eligible=0,assessment_kind='proposal',eligibility_reason='requires a fresh assessment after user restore/reopen',assessed_at=NULL WHERE project_id=?",
                          (project_id,))
            if action in ("complete", "abandon", "snooze", "archive", "reopen", "restore"):
                c.execute("UPDATE outbox SET status='cancelled' WHERE project_id=? AND status='pending'", (project_id,))
            c.commit()
        return {"project_id": project_id, "disposition": disposition, "archived": bool(archived),
                "snooze_until": snooze_until}

    def snooze(self, project_id: str, until: str) -> dict:
        result = self.decide(project_id, "snooze", until=until)
        return {"project_id": project_id, "snooze_until": result["snooze_until"]}

    def exclude_source(self, source_id: str, excluded: bool) -> dict:
        if not source_id or len(source_id) > 200:
            raise ValueError("invalid source_id")
        self.privacy.set_source_excluded(source_id, excluded)
        return {"source_id": source_id, "excluded": excluded}

    def exclude_thread(self, source_id: str, thread_id: str, excluded: bool) -> dict:
        if not source_id or not thread_id or len(source_id) > 200 or len(thread_id) > 500:
            raise ValueError("invalid source or thread identity")
        self.privacy.set_thread_excluded(source_id, thread_id, excluded)
        return {"source_id": source_id, "thread_id": thread_id, "excluded": excluded}

    def assign_thread(self, source_id: str, thread_id: str, project_id: str, from_project_id=None) -> dict:
        if not source_id or not thread_id or len(source_id) > 200 or len(thread_id) > 500:
            raise ValueError("invalid source or thread identity")
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", project_id):
            raise ValueError("invalid project_id")
        if from_project_id is not None and (not isinstance(from_project_id, str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", from_project_id)):
            raise ValueError("invalid from_project_id")
        now = iso(self.clock())
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            obs = [dict(r) for r in c.execute("SELECT * FROM observations WHERE source_id=? AND thread_id=?",
                                               (source_id, thread_id)).fetchall()]
            if not obs:
                raise KeyError("conversation not found")
            association_ids = {r[0] for r in c.execute("SELECT project_id FROM project_threads WHERE source_id=? AND thread_id=?",
                                                        (source_id, thread_id))}
            affected_ids = set(association_ids) | {project_id}
            lock = c.execute("SELECT project_id FROM thread_ownership WHERE source_id=? AND thread_id=?",
                             (source_id, thread_id)).fetchone()
            origin_id = from_project_id or (lock["project_id"] if lock else None)
            if origin_id is None and len(association_ids) == 1:
                origin_id = next(iter(association_ids))
            if project_id not in {r[0] for r in c.execute("SELECT project_id FROM projects")} and origin_id is None:
                raise KeyError("new grouping requires one source project; select the project to split")
            origin_ids = association_ids - {project_id}
            if origin_id:
                if origin_id not in association_ids:
                    raise ValueError("source project does not contain this conversation")
                origin_ids.add(origin_id)
            moved = []
            source_rows = {}
            for old_id in origin_ids:
                source_project = self._project(c, old_id)
                if source_project["archived"] or source_project["disposition"] != "open":
                    raise ValueError("reopen and restore source projects before moving a conversation")
                source_rows[old_id] = source_project
                moved.extend(e for e in json.loads(source_project["evidence_json"])
                             if e["source_id"] == source_id and e["thread_id"] == thread_id)
            if not moved:
                moved = [{k: r[k] for k in ("observation_id", "source_id", "thread_id", "message_id", "revision",
                                                "source_timestamp", "captured_at", "locator", "text")} for r in obs]
            target = c.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if target is None:
                source_project = source_rows[origin_id]
                split_fp = fingerprint({"project_id": project_id, "outcome": source_project["outcome"],
                                        "next_action": source_project["next_action"],
                                        "evidence_refs": [e["observation_id"] for e in moved]})
                c.execute("""INSERT INTO projects(project_id,outcome,next_action,reasoning,evidence_json,fingerprint,
                    disposition,snooze_until,updated_at,archived,assessment_kind,eligible,eligibility_reason,assessed_at,
                    status,grouping_confidence,last_progress_at) VALUES(?,?,?,?,?,?,?,?,?,0,'proposal',0,?,?,?, ?,?)""",
                    (project_id, source_project["outcome"], source_project["next_action"], source_project["reasoning"],
                     self.db.json(moved), split_fp, "open", None, now,
                     "requires fresh assessment after grouping correction", None, source_project["status"],
                     "uncertain", source_project["last_progress_at"]))
            else:
                if target["archived"] or target["disposition"] != "open":
                    raise ValueError("target project must be open and active")
                target_evidence = json.loads(target["evidence_json"])
                known_ids = {e["observation_id"] for e in target_evidence}
                target_evidence.extend(e for e in moved if e["observation_id"] not in known_ids)
                target_fp = fingerprint({"project_id": project_id, "outcome": target["outcome"],
                                         "next_action": target["next_action"],
                                         "evidence_refs": [e["observation_id"] for e in target_evidence]})
                c.execute("UPDATE projects SET evidence_json=?,fingerprint=?,eligible=0,assessment_kind='proposal',eligibility_reason='requires fresh assessment after grouping correction',assessed_at=NULL,updated_at=? WHERE project_id=?",
                          (self.db.json(target_evidence), target_fp, now, project_id))
            for old_id, old_project in source_rows.items():
                remaining = [e for e in json.loads(old_project["evidence_json"])
                             if not (e["source_id"] == source_id and e["thread_id"] == thread_id)]
                old_fp = fingerprint({"project_id": old_id, "outcome": old_project["outcome"],
                                      "next_action": old_project["next_action"],
                                      "evidence_refs": [e["observation_id"] for e in remaining]})
                c.execute("UPDATE projects SET evidence_json=?,fingerprint=?,eligible=0,assessment_kind='proposal',eligibility_reason='requires fresh assessment after grouping correction',assessed_at=NULL,updated_at=? WHERE project_id=?",
                          (self.db.json(remaining), old_fp, now, old_id))
            c.execute("DELETE FROM project_threads WHERE source_id=? AND thread_id=?", (source_id, thread_id))
            c.execute("INSERT INTO project_threads VALUES(?,?,?,?)", (source_id, thread_id, project_id, now))
            c.execute("""INSERT INTO thread_ownership(source_id,thread_id,project_id,assigned_at) VALUES(?,?,?,?)
                       ON CONFLICT(source_id,thread_id) DO UPDATE SET project_id=excluded.project_id,assigned_at=excluded.assigned_at""",
                      (source_id, thread_id, project_id, now))
            placeholders = ",".join("?" for _ in affected_ids)
            if affected_ids:
                c.execute(f"UPDATE outbox SET status='cancelled' WHERE status='pending' AND project_id IN ({placeholders})",
                          tuple(affected_ids))
            c.commit()
        return {"source_id": source_id, "thread_id": thread_id, "project_id": project_id}

    def state(self) -> dict:
        now = self.clock()
        with self.db.connect() as c:
            projects = c.execute("SELECT * FROM projects ORDER BY project_id").fetchall()
            proposals = c.execute("SELECT proposal_id,requested_project_id,outcome,next_action,reasoning,evidence_json,status,grouping_confidence,reason,created_at FROM review_proposals ORDER BY created_at DESC").fetchall()
            excluded_sources = {r[0] for r in c.execute("SELECT source_id FROM excluded_sources")}
            excluded_threads = {(r[0], r[1]) for r in c.execute("SELECT source_id,thread_id FROM excluded_threads")}
            ownership = {(r[0], r[1]): r[2] for r in c.execute("SELECT source_id,thread_id,project_id FROM thread_ownership")}
            associations = {(r[0], r[1], r[2]) for r in c.execute("SELECT source_id,thread_id,project_id FROM project_threads")}
        result = []
        for row in projects:
            item = dict(row)
            result.append({"project_id": item["project_id"], "outcome": item["outcome"], "next_action": item["next_action"],
                           "reasoning": item["reasoning"], "evidence": json.loads(item["evidence_json"]),
                           "disposition": item["disposition"], "snooze_until": item["snooze_until"],
                           "archived": bool(item["archived"]), "assessment_kind": item["assessment_kind"],
                           "eligible": bool(item["eligible"]), "eligibility_reason": item["eligibility_reason"],
                           "status": item["status"], "grouping_confidence": item["grouping_confidence"],
                           "last_progress_at": item["last_progress_at"],
                           "updated_at": item["updated_at"]})
        source_state = self.intake.state()
        trusted_sources = self._source_context()
        for source in source_state:
            source["threads"] = trusted_sources.get(source["source_id"], {}).get("threads", {})
        return {"generated_at": iso(now), "sources": source_state, "projects": result,
                "active_projects": [p for p in result if not p["archived"]],
                "archived_projects": [p for p in result if p["archived"]],
                "excluded_sources": sorted(excluded_sources),
                "excluded_threads": [{"source_id": s, "thread_id": t} for s, t in sorted(excluded_threads)],
                "proposals": [{**{k: v for k, v in dict(r).items() if k != "evidence_json"},
                               "evidence": json.loads(r["evidence_json"])} for r in proposals],
                "thread_ownership": [{"source_id": s, "thread_id": t, "project_id": p}
                                     for (s, t), p in sorted(ownership.items())],
                "project_threads": [{"source_id": s, "thread_id": t, "project_id": p}
                                    for s, t, p in sorted(associations)]}
