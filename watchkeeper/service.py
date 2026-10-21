"""Application behavior; dependencies are injectable at external boundaries."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
import threading
from functools import wraps

from .db import Database
from .delivery import DeliveryNotAccepted, fingerprint
from .intake import Intake
from .policy import validate_proposal
from .scheduler import MonitoringScheduler


def _serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._mutation_lock:
            return method(self, *args, **kwargs)
    return wrapped


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class WatchkeeperService:
    def __init__(self, database: Database, email_sink, clock=None, stale_after_seconds=3600, inferred_after_days=3,
                 *, schedule_timezone="UTC", review_time="08:00", digest_time="08:15",
                 review_window_limit=100, review_windows_per_day=4, review_lease_seconds=300,
                 review_window_bytes=180_000):
        self.db = database
        self.email_sink = email_sink
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = stale_after_seconds
        self.inferred_after_days = inferred_after_days
        self.intake = Intake(database, self.clock, stale_after_seconds)
        self._mutation_lock = threading.RLock()
        self.scheduler = MonitoringScheduler(database, self.clock, timezone=schedule_timezone,
                                             review_time=review_time, digest_time=digest_time,
                                             window_limit=review_window_limit,
                                             windows_per_day=review_windows_per_day,
                                             lease_seconds=review_lease_seconds,
                                             window_byte_limit=review_window_bytes)
        with self.db.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS project_assessment_guards (project_id TEXT PRIMARY KEY, guard_json TEXT NOT NULL, updated_at TEXT NOT NULL)")

    @_serialized
    def ingest(self, batch: dict) -> dict:
        result = self.intake.ingest(batch)
        coverage = batch.get("coverage", {}) if isinstance(batch, dict) else {}
        failure = coverage.get("failure", "none") if isinstance(coverage, dict) else "none"
        source_id = batch.get("source_id", "unknown") if isinstance(batch, dict) else "unknown"
        if failure != "none":
            self.scheduler.record_failure("source", source_id, failure, uuid.uuid4().hex)
        else:
            self.scheduler.recover("source", source_id)
        return result

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
                thread_ids = [r[0] for r in c.execute("SELECT DISTINCT thread_id FROM observations WHERE source_id=?",
                                                       (source_id,))]
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
                trusted_threads[thread_id] = thread
            item["threads"] = trusted_threads
            normalized[source_id] = item
        return normalized

    @_serialized
    def review(self, proposal: dict, *, _incomplete_threads=()) -> dict:
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM observations").fetchall()
            all_obs = {row["observation_id"]: dict(row) for row in rows}
            sources = self._source_context()
            for source_id, thread_id in _incomplete_threads:
                source = sources.get(source_id)
                if isinstance(source, dict):
                    thread = source.setdefault("threads", {}).setdefault(thread_id, {})
                    thread["coverage_complete"] = False
                    thread["completeness"] = "partial"
                    thread["gaps"] = list(thread.get("gaps", [])) + ["leased review window did not contain full thread context"]
            # Exclusions are enforced here as well as at intake, so already stored
            # observations cannot be smuggled back into a review proposal.
            obs = {oid: row for oid, row in all_obs.items()
                   if not self._excluded(c, row["source_id"], row["thread_id"])}
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

            if kind == "attention" and eligible and not (archived or locked or snoozed):
                pairs = sorted({(row["source_id"], row["thread_id"]) for row in evidence_rows})
                guard = {}
                for source_id, thread_id in pairs:
                    thread_rows = c.execute("SELECT observation_id,revision FROM observations WHERE source_id=? AND thread_id=? ORDER BY observation_id,revision",
                                            (source_id, thread_id)).fetchall()
                    digest = hashlib.sha256(json.dumps([(r["observation_id"], r["revision"]) for r in thread_rows],
                                                       separators=(",", ":")).encode()).hexdigest()
                    guard[f"{source_id}\0{thread_id}"] = digest
                c.execute("INSERT INTO project_assessment_guards VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET guard_json=excluded.guard_json,updated_at=excluded.updated_at",
                          (project_id, json.dumps(guard, sort_keys=True), now))

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
                else:
                    status = c.execute("SELECT status FROM outbox WHERE delivery_id=?", (existing["delivery_id"],)).fetchone()[0]
                    if status == "cancelled":
                        c.execute("UPDATE outbox SET status='pending',created_at=?,delivered_at=NULL WHERE delivery_id=?",
                                  (now, existing["delivery_id"]))
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

    def review_input(self, *, cursor=None, limit=500, byte_limit=180_000) -> dict:
        """Return observations eligible for review and a secret-free register view."""
        sources = self._source_context()
        with self.db.connect() as c:
            cursor_clause = ""
            parameters = [iso(self.clock())]
            if cursor is not None:
                if (not isinstance(cursor, dict) or set(cursor) != {"source_timestamp", "captured_at", "observation_id"} or
                        any(not isinstance(cursor[k], str) for k in cursor)):
                    raise ValueError("invalid review cursor")
                cursor_clause = " AND (julianday(o.source_timestamp) < julianday(?) OR (julianday(o.source_timestamp)=julianday(?) AND (o.captured_at < ? OR (o.captured_at=? AND o.observation_id<?))))"
                parameters.extend([cursor["source_timestamp"], cursor["source_timestamp"],
                                   cursor["captured_at"], cursor["captured_at"], cursor["observation_id"]])
            if type(limit) is not int or not 1 <= limit <= 500:
                raise ValueError("review window limit must be between 1 and 500")
            if type(byte_limit) is not int or not 180_000 <= byte_limit <= 220_000:
                raise ValueError("review window byte limit must be between 180000 and 220000")
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
                         AND (p.snooze_until IS NULL OR p.snooze_until<=?)))""" + cursor_clause +
                " ORDER BY julianday(o.source_timestamp) DESC,o.captured_at DESC,o.observation_id DESC LIMIT ?",
                (*parameters, limit + 1)).fetchall()
            projects = c.execute("""SELECT project_id,outcome,next_action,disposition,snooze_until,archived,
                assessment_kind,eligible,eligibility_reason,updated_at FROM projects WHERE disposition='open'
                AND archived=0 AND (snooze_until IS NULL OR snooze_until<=?) ORDER BY project_id LIMIT 5""",
                (iso(self.clock()),)).fetchall()
            proposals = c.execute("SELECT proposal_id,requested_project_id,outcome,next_action,status,grouping_confidence,reason,created_at FROM review_proposals ORDER BY created_at DESC LIMIT 5").fetchall()
        has_more = len(rows) > limit
        seeds = [dict(row) for row in rows[:limit]]
        project_values = [{k: (v[:120] if isinstance(v, str) else v) for k, v in dict(row).items()}
                          for row in projects]
        proposal_values = [{k: (row[k][:120] if isinstance(row[k], str) else row[k])
                            for k in row.keys()} for row in proposals]

        def compact_sources(selected_rows):
            result = {}
            for source_id, thread_id in sorted({(r["source_id"], r["thread_id"]) for r in selected_rows}):
                source = sources.get(source_id, {})
                coverage = source.get("coverage", {}) if isinstance(source, dict) else {}
                thread = source.get("threads", {}).get(thread_id, {}) if isinstance(source, dict) else {}
                entry = result.setdefault(source_id, {
                    "available": source.get("available") is True,
                    "freshness": source.get("freshness", "unknown"),
                    "last_seen_at": source.get("last_seen_at"),
                    "coverage": {**{k: coverage.get(k) for k in ("state", "message_completeness", "archived_scanned", "current_activity")},
                                 "gaps": [str(gap)[:160] for gap in (coverage.get("gaps") or [])[:3]]},
                    "threads": {}})
                entry["threads"][thread_id] = {k: thread.get(k) for k in (
                    "coverage_complete", "completeness", "activity", "active",
                    "last_progress_at", "last_progress_revision", "last_progress_captured_at")}
                entry["threads"][thread_id]["gaps"] = [str(gap)[:160] for gap in (thread.get("gaps") or [])[:3]]
                if entry["threads"][thread_id].get("coverage_complete") is not True:
                    entry["coverage"]["gaps"] = (entry["coverage"].get("gaps") or []) + [
                        f"{thread_id}: {entry['threads'][thread_id]['gaps'][0] if entry['threads'][thread_id]['gaps'] else 'partial thread context'}"]
            for entry in result.values():
                if "gaps" not in entry["coverage"]:
                    entry["coverage"]["gaps"] = []
            return result

        # Primary rows advance the cursor. Full context for a small thread is
        # expanded within the same row/byte budget, even when its messages are
        # interleaved with other threads. Older expanded context never moves
        # the primary cursor backwards.
        selected = []
        partial_pairs = set()
        processed_pairs = set()
        cache = {}
        nested_budget = limit * 3
        last_seed = None
        stopped_early = False

        def mark_partial(pair, reason="bounded review window contains only part of this thread"):
            partial_pairs.add(pair)
            thread = sources.get(pair[0], {}).get("threads", {}).get(pair[1], {})
            if isinstance(thread, dict):
                thread["coverage_complete"] = False
                thread["completeness"] = "partial"
                thread["gaps"] = [reason]

        def payload_for(candidate_rows, cursor_row, more):
            cursor_value = ({"source_timestamp": cursor_row["source_timestamp"],
                             "captured_at": cursor_row["captured_at"],
                             "observation_id": cursor_row["observation_id"]} if more and cursor_row else None)
            return {"observations": candidate_rows, "projects": project_values, "proposals": proposal_values,
                    "sources": compact_sources(candidate_rows), "current_time": iso(self.clock()),
                    "coverage": {"observation_limit": limit, "observation_byte_limit": byte_limit,
                                 "observations_truncated": False, "backlog_remaining": more,
                                 "next_cursor": cursor_value,
                                 "next_before": cursor_row["captured_at"] if more and cursor_row else None}}

        for index, seed in enumerate(seeds):
            pair = (seed["source_id"], seed["thread_id"])
            if pair in processed_pairs:
                if seed["observation_id"] not in {row["observation_id"] for row in selected}:
                    candidate = selected + [seed]
                    candidate.sort(key=lambda row: (datetime.fromisoformat(row["source_timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc),
                                                    row["captured_at"], row["observation_id"]), reverse=True)
                    more = has_more or index < len(seeds) - 1
                    payload = payload_for(candidate, seed, more)
                    if len(candidate) > limit or len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > byte_limit:
                        stopped_early = True
                        break
                    selected = candidate
                last_seed = seed
                if len(selected) >= limit and index < len(seeds) - 1:
                    stopped_early = True
                    break
                continue
            processed_pairs.add(pair)
            if pair not in cache:
                if nested_budget <= 0:
                    mark_partial(pair, "bounded review window history-read budget reached")
                    additions = [seed] if seed["observation_id"] not in {row["observation_id"] for row in selected} else []
                    candidate = selected + additions
                    candidate.sort(key=lambda row: (datetime.fromisoformat(row["source_timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc),
                                                    row["captured_at"], row["observation_id"]), reverse=True)
                    candidate_fits = (len(candidate) <= limit and len(json.dumps(payload_for(
                        candidate, seed, has_more or index < len(seeds) - 1),
                        ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= byte_limit)
                    if candidate_fits:
                        selected = candidate
                        last_seed = seed
                    else:
                        stopped_early = True
                        break
                    stopped_early = has_more or index < len(seeds) - 1
                    break
                fetch_limit = min(limit + 1, nested_budget)
                with self.db.connect() as c:
                    thread_rows = [dict(row) for row in c.execute(
                        "SELECT observation_id,source_id,thread_id,message_id,revision,source_timestamp,captured_at,locator,text FROM observations WHERE source_id=? AND thread_id=? ORDER BY julianday(source_timestamp) DESC,captured_at DESC,observation_id DESC LIMIT ?",
                        (*pair, fetch_limit)).fetchall()]
                nested_budget -= len(thread_rows)
                complete_thread = len(thread_rows) <= limit and len(thread_rows) < fetch_limit
                cache[pair] = (thread_rows, complete_thread)
            thread_rows, full_thread_known = cache[pair]
            already = {row["observation_id"] for row in selected}
            additions = [row for row in thread_rows if row["observation_id"] not in already]
            can_expand = full_thread_known and len(selected) + len(additions) <= limit
            if not can_expand:
                additions = [seed] if seed["observation_id"] not in already else []
                mark_partial(pair)
            candidate = selected + additions
            candidate.sort(key=lambda row: (datetime.fromisoformat(row["source_timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc),
                                            row["captured_at"], row["observation_id"]), reverse=True)
            more = has_more or index < len(seeds) - 1 or index == len(seeds) - 1 and len(rows) > limit
            payload = payload_for(candidate, seed, more)
            if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > byte_limit:
                # If full context does not fit the byte budget, keep one seed
                # and explicitly mark this thread partial to preserve progress.
                fallback = [seed] if seed["observation_id"] not in already else []
                mark_partial(pair, "bounded review window byte budget contains only part of this thread")
                candidate = selected + fallback
                candidate.sort(key=lambda row: (datetime.fromisoformat(row["source_timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc),
                                                row["captured_at"], row["observation_id"]), reverse=True)
                fallback_more = has_more or index < len(seeds) - 1
                payload = payload_for(candidate, seed, fallback_more)
                if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > byte_limit:
                    if not selected:
                        raise ValueError("review window context exceeds configured byte limit")
                    stopped_early = True
                    break
                stopped_early = has_more or index < len(seeds) - 1
            selected = candidate
            last_seed = seed
            if len(selected) >= limit and index < len(seeds) - 1:
                stopped_early = True
                break
            if stopped_early:
                break
        if not selected and seeds:
            raise ValueError("review window context exceeds configured byte limit")
        for source_id, thread_id in partial_pairs:
            thread = sources.get(source_id, {}).get("threads", {}).get(thread_id, {})
            if isinstance(thread, dict):
                thread["coverage_complete"] = False
                thread["completeness"] = "partial"
                thread["gaps"] = ["bounded review window contains only part of this thread"]
        has_more = has_more or stopped_early
        next_cursor = ({"source_timestamp": last_seed["source_timestamp"],
                        "captured_at": last_seed["captured_at"], "observation_id": last_seed["observation_id"]}
                       if has_more and last_seed else None)
        result = {"observations": selected, "projects": project_values, "proposals": proposal_values,
                  "sources": compact_sources(selected), "current_time": iso(self.clock()),
                  "coverage": {"observation_limit": limit, "observation_byte_limit": byte_limit,
                               "observations_truncated": False, "backlog_remaining": has_more,
                               "next_cursor": next_cursor,
                               "next_before": last_seed["captured_at"] if has_more and last_seed else None}}
        encoded_size = lambda: len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        context_trimmed = False
        while encoded_size() > byte_limit and proposal_values:
            proposal_values.pop()
            context_trimmed = True
        while encoded_size() > byte_limit and project_values:
            project_values.pop()
            context_trimmed = True
        if context_trimmed:
            result["coverage"]["register_context_truncated"] = True
        if encoded_size() > byte_limit:
            for source in result["sources"].values():
                if source["coverage"].get("gaps"):
                    source["coverage"]["gaps"] = ["source coverage has reported gaps"]
                for thread in source["threads"].values():
                    if thread.get("coverage_complete") is not True:
                        thread["gaps"] = ["partial thread context"]
        if encoded_size() > byte_limit:
            raise ValueError("bounded review metadata exceeds configured byte limit")
        return result

    @_serialized
    def claim_review_job(self):
        return self.scheduler.claim(lambda **kw: self.review_input(**kw))

    @_serialized
    def complete_review_job(self, run_id, lease_token, proposals):
        if not isinstance(proposals, list) or len(proposals) > 50:
            raise ValueError("job result must contain at most 50 proposals")
        now = self.clock()
        current_sources = self._source_context()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            job = self.scheduler.validate_lease(c, run_id, lease_token, now)
            if job is None:
                c.rollback()
                return {"status": "stale", "reason": "review lease expired or was replaced"}
            snapshot = json.loads(job["snapshot_json"] or "{}")
            revisions = snapshot.get("revisions", {}) if isinstance(snapshot, dict) else {}
            allowed_refs = set(revisions)
            current = {row["observation_id"]: row["revision"] for row in c.execute(
                "SELECT observation_id,revision FROM observations").fetchall()}
            if any(current.get(observation_id) != value.get("revision")
                   for observation_id, value in revisions.items() if isinstance(value, dict)):
                c.execute("UPDATE monitoring_runs SET status='retry',snapshot_json=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE run_id=?",
                          (iso(now), run_id))
                c.commit()
                return {"status": "stale", "reason": "evidence changed while review was running"}
            for pair_key, guard in snapshot.get("thread_guard", {}).items():
                source_id, thread_id = pair_key.split("\0", 1)
                thread_rows = c.execute("SELECT observation_id,revision FROM observations WHERE source_id=? AND thread_id=? ORDER BY observation_id,revision",
                                        (source_id, thread_id)).fetchall()
                latest = c.execute("SELECT COUNT(*) AS n,MAX(captured_at) AS captured,MAX(source_timestamp) AS source_time FROM observations WHERE source_id=? AND thread_id=?",
                                   (source_id, thread_id)).fetchone()
                digest = hashlib.sha256(json.dumps([(r["observation_id"], r["revision"]) for r in thread_rows],
                                                   separators=(",", ":")).encode()).hexdigest()
                if (digest != guard.get("fingerprint") or latest["n"] != guard.get("count") or latest["captured"] != guard.get("captured_at") or
                        latest["source_time"] != guard.get("source_timestamp")):
                    c.execute("UPDATE monitoring_runs SET status='retry',snapshot_json=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE run_id=?",
                              (iso(now), run_id))
                    c.commit()
                    return {"status": "stale", "reason": "thread evidence changed while review was running"}
                thread = current_sources.get(source_id, {}).get("threads", {}).get(thread_id, {})
                if any(thread.get(key) != guard.get(snapshot_key) for key, snapshot_key in (
                        ("last_progress_at", "watermark_at"),
                        ("last_progress_revision", "watermark_revision"),
                        ("last_progress_captured_at", "watermark_captured_at"))):
                    c.execute("UPDATE monitoring_runs SET status='retry',snapshot_json=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE run_id=?",
                              (iso(now), run_id))
                    c.commit()
                    return {"status": "stale", "reason": "trusted thread progress changed while review was running"}
            if any(not isinstance(proposal, dict) or
                   not isinstance(proposal.get("evidence_refs"), list) or
                   not set(proposal["evidence_refs"]).issubset(allowed_refs) for proposal in proposals):
                c.rollback()
                return {"status": "stale", "reason": "review result referenced evidence outside its leased window"}
            c.execute("UPDATE monitoring_runs SET lease_until=?,updated_at=? WHERE run_id=? AND lease_token=?",
                      (iso(now + timedelta(seconds=self.scheduler.lease_seconds)), iso(now), run_id, lease_token))
            c.commit()
        results = [self.review(proposal, _incomplete_threads=snapshot.get("partial_threads", []))
                   for proposal in proposals]
        # review() validates each referenced revision and current user decision;
        # the mutation lock prevents another service mutation during this method.
        completed = self.scheduler.complete(run_id, lease_token, self.clock(),
                                             snapshot.get("next_cursor"),
                                             has_more=snapshot.get("has_more") is True)
        return {"status": "success" if completed else "stale", "proposals": results,
                "completed": completed}

    def fail_review_job(self, run_id, lease_token, failure_kind):
        self.scheduler.fail(run_id, lease_token, failure_kind)

    def _delivery_is_current(self, c, project, now, sources):
        if (project is None or project["disposition"] != "open" or project["archived"] or
                not project["eligible"] or project["assessment_kind"] != "attention" or
                (project["snooze_until"] is not None and project["snooze_until"] > now)):
            return False
        evidence = json.loads(project["evidence_json"])
        saved_guard_row = c.execute("SELECT guard_json FROM project_assessment_guards WHERE project_id=?",
                                    (project["project_id"],)).fetchone()
        if saved_guard_row is None:
            return False
        saved_guard = json.loads(saved_guard_row["guard_json"])
        by_thread = {}
        for item in evidence:
            source_id, thread_id = item["source_id"], item["thread_id"]
            if c.execute("SELECT 1 FROM excluded_sources WHERE source_id=?", (source_id,)).fetchone() or c.execute(
                    "SELECT 1 FROM excluded_threads WHERE source_id=? AND thread_id=?", (source_id, thread_id)).fetchone():
                return False
            current = c.execute("SELECT revision FROM observations WHERE observation_id=?", (item["observation_id"],)).fetchone()
            if current is None or current["revision"] != item["revision"]:
                return False
            source = sources.get(source_id)
            if not isinstance(source, dict) or source.get("available") is not True or source.get("freshness") != "fresh":
                return False
            thread = source.get("threads", {}).get(thread_id, {})
            if not isinstance(thread, dict) or thread.get("active") is True:
                return False
            by_thread.setdefault((source_id, thread_id), []).append(item)
        for (source_id, thread_id), rows in by_thread.items():
            key = f"{source_id}\0{thread_id}"
            current_rows = c.execute("SELECT observation_id,revision,source_timestamp,captured_at FROM observations WHERE source_id=? AND thread_id=? ORDER BY observation_id,revision",
                                     (source_id, thread_id)).fetchall()
            current_digest = hashlib.sha256(json.dumps([(r["observation_id"], r["revision"]) for r in current_rows],
                                                        separators=(",", ":")).encode()).hexdigest()
            if saved_guard.get(key) != current_digest:
                return False
            source = sources[source_id]
            thread = source.get("threads", {}).get(thread_id, {})
            watermark = thread.get("last_progress_at")
            if watermark:
                try:
                    watermark_at = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                    watermark_capture = datetime.fromisoformat(thread["last_progress_captured_at"].replace("Z", "+00:00"))
                    raw = max(current_rows, key=lambda item: (
                        datetime.fromisoformat(item["source_timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc),
                        datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00")).astimezone(timezone.utc)))
                    raw_source_at = datetime.fromisoformat(raw["source_timestamp"].replace("Z", "+00:00"))
                    raw_capture = datetime.fromisoformat(raw["captured_at"].replace("Z", "+00:00"))
                    if (watermark_at > raw_source_at or
                            (watermark_at == raw_source_at and watermark_capture > raw_capture) or
                            (thread.get("last_progress_revision") != raw["revision"] and watermark_capture > raw_capture)):
                        return False
                except (ValueError, TypeError, KeyError):
                    return False
        return True

    @_serialized
    def deliver_pending(self) -> dict:
        """Deliver one digest for the current eligible queue and open notices."""
        now = iso(self.clock())
        sources = self._source_context()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            batch = c.execute("SELECT * FROM delivery_batches WHERE status IN ('sending','retryable') ORDER BY created_at LIMIT 1").fetchone()
            retrying_batch = batch is not None
            if batch is not None and batch["retry_after"] and batch["retry_after"] > now:
                c.commit()
                return {"delivered": 0, "emails": 0, "notices": 0, "status": "retry_deferred"}
            if batch is not None and batch["status"] == "sending" and not getattr(self.email_sink, "idempotent_acceptance", False):
                delivery_ids = json.loads(batch["delivery_ids_json"])
                c.execute("UPDATE delivery_batches SET status='ambiguous',updated_at=? WHERE batch_id=?", (now, batch["batch_id"]))
                if delivery_ids:
                    c.executemany("UPDATE outbox SET status='ambiguous' WHERE delivery_id=? AND status='sending'",
                                  [(delivery_id,) for delivery_id in delivery_ids])
                c.execute("UPDATE outbox_attempts SET result='ambiguous' WHERE result='started' AND delivery_id IN (SELECT value FROM json_each(?))",
                          (json.dumps(delivery_ids),))
                c.commit()
                return {"delivered": 0, "emails": 0, "notices": 0, "status": "ambiguous"}
            if batch is None:
                delivery_ids = []
                for row in c.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at,delivery_id").fetchall():
                    project = c.execute("SELECT * FROM projects WHERE project_id=?", (row["project_id"],)).fetchone()
                    if self._delivery_is_current(c, project, now, sources):
                        delivery_ids.append(row["delivery_id"])
                    else:
                        c.execute("UPDATE outbox SET status='cancelled' WHERE delivery_id=? AND status='pending'",
                                  (row["delivery_id"],))
                notice_ids = [row["incident_id"] for row in c.execute(
                    """SELECT incident_id FROM health_incidents h WHERE status='open' AND occurrences>=2
                       AND notified_at IS NULL AND NOT EXISTS (
                         SELECT 1 FROM delivery_batches b, json_each(b.notice_ids_json) n
                         WHERE b.status='ambiguous' AND n.value=h.incident_id)
                       ORDER BY first_seen,incident_id""").fetchall()]
                if not delivery_ids and not notice_ids:
                    c.commit()
                    return {"delivered": 0, "emails": 0, "notices": 0}
                membership = sorted(delivery_ids + notice_ids)
                digest_id = hashlib.sha256(("watchkeeper-digest\0" + "\0".join(membership)).encode()).hexdigest()
                batch_id = uuid.uuid4().hex
                c.execute("INSERT INTO delivery_batches(batch_id,digest_id,delivery_ids_json,notice_ids_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                          (batch_id, digest_id, json.dumps(delivery_ids), json.dumps(notice_ids), "sending", now, now))
                c.executemany("UPDATE outbox SET status='sending' WHERE delivery_id=? AND status='pending'",
                              [(delivery_id,) for delivery_id in delivery_ids])
                for delivery_id in delivery_ids:
                    next_attempt = c.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM outbox_attempts WHERE delivery_id=?",
                                             (delivery_id,)).fetchone()[0]
                    c.execute("INSERT INTO outbox_attempts VALUES(?,?,?,?,?)",
                              (uuid.uuid4().hex, delivery_id, next_attempt, "started", now))
                batch = c.execute("SELECT * FROM delivery_batches WHERE batch_id=?", (batch_id,)).fetchone()
            delivery_ids = json.loads(batch["delivery_ids_json"])
            notice_ids = json.loads(batch["notice_ids_json"])
            projects = []
            membership_current = True
            for delivery_id in delivery_ids:
                row = c.execute("SELECT p.*,o.fingerprint AS delivery_fingerprint FROM outbox o JOIN projects p ON p.project_id=o.project_id WHERE o.delivery_id=?",
                                (delivery_id,)).fetchone()
                if row is not None:
                    expected_fingerprint = row["fingerprint"]
                    if row["snooze_until"] is not None:
                        expected_fingerprint = hashlib.sha256(
                            f"{expected_fingerprint}:{row['snooze_until']}".encode()).hexdigest()
                    membership_current = membership_current and row["delivery_fingerprint"] == expected_fingerprint
                    projects.append({"project_id": row["project_id"], "outcome": row["outcome"],
                                     "next_action": row["next_action"], "reasoning": row["reasoning"],
                                     "evidence": json.loads(row["evidence_json"])})
                else:
                    membership_current = False
            notices = [dict(row) for row in c.execute(
                "SELECT * FROM health_incidents WHERE incident_id IN (SELECT value FROM json_each(?))",
                (json.dumps(notice_ids),)).fetchall()]
            retry_stale = (retrying_batch and (not membership_current or
                any(not self._delivery_is_current(
                    c, c.execute("SELECT * FROM projects WHERE project_id=?", (project["project_id"],)).fetchone(),
                    now, sources) for project in projects) or
                len(notices) != len(notice_ids) or any(n["status"] != "open" or n["notified_at"] is not None or
                    n["occurrences"] < 2 for n in notices)))
            if retry_stale:
                accepted = bool(getattr(self.email_sink, "has_delivery", lambda _id: False)(batch["digest_id"]))
                if accepted:
                    c.execute("UPDATE delivery_batches SET status='accepted',updated_at=? WHERE batch_id=?",
                              (now, batch["batch_id"]))
                    c.executemany("UPDATE outbox SET status='delivered',delivered_at=? WHERE delivery_id=? AND status='sending'",
                                  [(now, delivery_id) for delivery_id in delivery_ids])
                    for delivery_id in delivery_ids:
                        c.execute("UPDATE outbox_attempts SET result='accepted' WHERE delivery_id=? AND attempt_number=(SELECT MAX(attempt_number) FROM outbox_attempts WHERE delivery_id=?)",
                                  (delivery_id, delivery_id))
                    c.executemany("UPDATE health_incidents SET notified_at=? WHERE incident_id=?",
                                  [(now, incident_id) for incident_id in notice_ids])
                    c.commit()
                    return {"delivered": len(delivery_ids), "emails": 0, "notices": len(notice_ids),
                            "status": "previously_accepted"}
                c.execute("UPDATE delivery_batches SET status='cancelled',updated_at=? WHERE batch_id=?",
                          (now, batch["batch_id"]))
                c.executemany("UPDATE outbox SET status='cancelled' WHERE delivery_id=? AND status='sending'",
                              [(delivery_id,) for delivery_id in delivery_ids])
                c.executemany("UPDATE outbox_attempts SET result='cancelled' WHERE delivery_id=? AND result='started'",
                              [(delivery_id,) for delivery_id in delivery_ids])
                c.commit()
                return {"delivered": 0, "emails": 0, "notices": 0, "status": "cancelled_stale"}
            c.execute("UPDATE delivery_batches SET status='sending',attempts=attempts+1,retry_after=NULL WHERE batch_id=?", (batch["batch_id"],))
            batch_attempt = c.execute("SELECT attempts FROM delivery_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0]
            c.commit()
            digest_id = batch["digest_id"]
            batch_id = batch["batch_id"]
            if retrying_batch:
                for delivery_id in delivery_ids:
                    next_attempt = c.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM outbox_attempts WHERE delivery_id=?",
                                             (delivery_id,)).fetchone()[0]
                    c.execute("INSERT INTO outbox_attempts VALUES(?,?,?,?,?)",
                              (uuid.uuid4().hex, delivery_id, next_attempt, "started", now))
        try:
            self.email_sink.deliver_digest(digest_id, projects, notices)
        except Exception as exc:
            retryable = bool(getattr(self.email_sink, "idempotent_acceptance", False)) or isinstance(exc, DeliveryNotAccepted)
            next_status = "retryable" if retryable else "ambiguous"
            retry_after = (iso(self.clock() + timedelta(seconds=min(3600, 60 * (2 ** min(max(batch_attempt - 1, 0), 5)))))
                           if retryable else None)
            with self.db.connect() as c:
                c.execute("UPDATE delivery_batches SET status=?,retry_after=?,updated_at=? WHERE batch_id=?",
                          (next_status, retry_after, now, batch_id))
                if not retryable:
                    c.executemany("UPDATE outbox SET status='ambiguous' WHERE delivery_id=? AND status='sending'",
                                  [(delivery_id,) for delivery_id in delivery_ids])
                if delivery_ids:
                    c.executemany("UPDATE outbox_attempts SET result=? WHERE delivery_id=? AND result='started'",
                                  [("retryable" if retryable else "ambiguous", delivery_id) for delivery_id in delivery_ids])
            return {"delivered": 0, "emails": 0, "notices": 0,
                    "status": "retryable" if retryable else "ambiguous"}
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE delivery_batches SET status='accepted',updated_at=? WHERE batch_id=?", (now, batch_id))
            c.executemany("UPDATE outbox SET status='delivered',delivered_at=? WHERE delivery_id=? AND status='sending'",
                          [(now, delivery_id) for delivery_id in delivery_ids])
            if delivery_ids:
                c.executemany("UPDATE outbox_attempts SET result='accepted' WHERE delivery_id=? AND result='started'",
                              [(delivery_id,) for delivery_id in delivery_ids])
            if notice_ids:
                c.executemany("UPDATE health_incidents SET notified_at=? WHERE incident_id=?", [(now, incident_id) for incident_id in notice_ids])
            c.commit()
        return {"delivered": len(delivery_ids), "emails": 1, "notices": len(notice_ids)}

    def _project(self, c, project_id):
        row = c.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError("project not found")
        return row

    @_serialized
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

    @_serialized
    def exclude_source(self, source_id: str, excluded: bool) -> dict:
        if not source_id or len(source_id) > 200:
            raise ValueError("invalid source_id")
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if excluded:
                c.execute("INSERT OR IGNORE INTO excluded_sources VALUES(?,?)", (source_id, iso(self.clock())))
            else:
                c.execute("DELETE FROM excluded_sources WHERE source_id=?", (source_id,))
            # Any source-level exclusion change invalidates pending work for
            # every project associated with that source, including discovered
            # many-to-many links and explicit user locks.
            c.execute("""UPDATE outbox SET status='cancelled' WHERE status='pending' AND project_id IN
                (SELECT project_id FROM project_threads WHERE source_id=? UNION
                 SELECT project_id FROM thread_ownership WHERE source_id=?)""", (source_id, source_id))
            c.execute("UPDATE review_progress SET cursor_json=NULL,updated_at=? WHERE stream='history'", (iso(self.clock()),))
            c.execute("UPDATE monitoring_runs SET status='invalidated',snapshot_json=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE status IN ('pending','retry','leased')",
                      (iso(self.clock()),))
            c.commit()
        return {"source_id": source_id, "excluded": excluded}

    @_serialized
    def exclude_thread(self, source_id: str, thread_id: str, excluded: bool) -> dict:
        if not source_id or not thread_id or len(source_id) > 200 or len(thread_id) > 500:
            raise ValueError("invalid source or thread identity")
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if excluded:
                c.execute("INSERT OR IGNORE INTO excluded_threads VALUES(?,?,?)", (source_id, thread_id, iso(self.clock())))
            else:
                c.execute("DELETE FROM excluded_threads WHERE source_id=? AND thread_id=?", (source_id, thread_id))
            c.execute("""UPDATE outbox SET status='cancelled' WHERE status='pending' AND project_id IN
                (SELECT project_id FROM project_threads WHERE source_id=? AND thread_id=? UNION
                 SELECT project_id FROM thread_ownership WHERE source_id=? AND thread_id=?)""",
                      (source_id, thread_id, source_id, thread_id))
            c.execute("UPDATE review_progress SET cursor_json=NULL,updated_at=? WHERE stream='history'", (iso(self.clock()),))
            c.execute("UPDATE monitoring_runs SET status='invalidated',snapshot_json=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE status IN ('pending','retry','leased')",
                      (iso(self.clock()),))
            c.commit()
        return {"source_id": source_id, "thread_id": thread_id, "excluded": excluded}

    @_serialized
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
            incidents = [dict(r) for r in c.execute("SELECT incident_id,category,subject,failure_kind,status,first_seen,last_seen,occurrences,notified_at FROM health_incidents ORDER BY first_seen DESC")]
            runs = [dict(r) for r in c.execute("SELECT run_id,schedule_key,status,cursor_json,attempts,failure_kind,created_at,updated_at FROM monitoring_runs ORDER BY created_at DESC LIMIT 20")]
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
                                    for s, t, p in sorted(associations)],
                "monitoring": {"health_incidents": incidents, "review_runs": runs,
                               "open_notices": [i for i in incidents if i["status"] == "open" and i["occurrences"] >= 2]}}

    def scheduled_tick(self):
        """Create today's durable jobs and send the due daily digest."""
        self.scheduler.ensure_daily()
        if self.scheduler.due("digest"):
            key = self.scheduler.local_key("digest")
            with self.db.connect() as c:
                batch = c.execute("SELECT * FROM digest_batches WHERE schedule_key=? AND status='pending'",
                                   (key,)).fetchone()
            if batch is not None:
                result = self.deliver_pending()
                result_status = result.get("status")
                status = ("pending" if result_status in {"retryable", "retry_deferred"} else
                          "ambiguous" if result_status == "ambiguous" else
                          "delivered" if result.get("emails") or result_status == "previously_accepted" else
                          "empty")
                with self.db.connect() as c:
                    c.execute("UPDATE digest_batches SET status=?,delivered_at=? WHERE batch_id=? AND status='pending'",
                              (status, None if status == "pending" else iso(self.clock()), batch["batch_id"]))
                return result
        return {"delivered": 0, "emails": 0, "notices": 0}

    def privacy_purge_hook(self, c, *, project_ids, threads, observation_ids, delivery_ids):
        """Erase content-free recovery references affected by a privacy purge."""
        pairs = set(threads)
        obs = set(observation_ids)
        project_ids = set(project_ids)
        captures = set()
        if delivery_ids:
            delivery_placeholders = ",".join("?" for _ in delivery_ids)
            for batch in c.execute("SELECT batch_id,digest_id,delivery_ids_json FROM delivery_batches").fetchall():
                members = set(json.loads(batch["delivery_ids_json"]))
                affected = members.intersection(delivery_ids)
                if affected:
                    captures.add(batch["digest_id"])
                    remaining = members - set(delivery_ids)
                    if remaining:
                        member_placeholders = ",".join("?" for _ in remaining)
                        c.execute(f"UPDATE outbox SET status='cancelled' WHERE status='sending' AND delivery_id IN ({member_placeholders})",
                                  tuple(remaining))
                        c.execute(f"UPDATE outbox_attempts SET result='cancelled' WHERE result IN ('started','retryable') AND delivery_id IN ({member_placeholders})",
                                  tuple(remaining))
                    c.execute("DELETE FROM delivery_batches WHERE batch_id=?", (batch["batch_id"],))
        for row in c.execute("SELECT run_id,snapshot_json FROM monitoring_runs WHERE snapshot_json IS NOT NULL").fetchall():
            try:
                snapshot = json.loads(row["snapshot_json"])
            except (TypeError, json.JSONDecodeError):
                snapshot = {}
            refs = snapshot.get("revisions", {}) if isinstance(snapshot, dict) else {}
            if any(oid in obs or (value.get("source_id"), value.get("thread_id")) in pairs
                   for oid, value in refs.items() if isinstance(value, dict)):
                c.execute("UPDATE monitoring_runs SET status='invalidated',snapshot_json=NULL,lease_token=NULL,lease_until=NULL WHERE run_id=?",
                          (row["run_id"],))
        if pairs or obs or project_ids:
            c.execute("UPDATE review_progress SET cursor_json=NULL WHERE stream='history'")
        if project_ids:
            project_placeholders = ",".join("?" for _ in project_ids)
            c.execute(f"DELETE FROM project_assessment_guards WHERE project_id IN ({project_placeholders})",
                      tuple(project_ids))
        if delivery_ids:
            c.execute(f"DELETE FROM outbox_attempts WHERE delivery_id IN ({delivery_placeholders})", tuple(delivery_ids))
        return sorted(captures)
