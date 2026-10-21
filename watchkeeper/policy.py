"""Deterministic eligibility checks for structured reviewer proposals.

The reviewer supplies classifications and evidence references. This module
decides whether a proposal may enter the attention digest. A model label never
overrides a known source outage, active-work signal, external dependency, or
uncertain grouping/completion judgment.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


class ProposalError(ValueError):
    pass


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FIELDS = {"project_id", "outcome", "next_action", "reasoning", "evidence_refs",
           "evidence_revisions", "status", "grouping_confidence"}
_STATUSES = {"explicit_request", "inferred", "active_work", "external_dependency",
             "uncertain_completion", "uncertain_grouping", "no_action"}
_UNCERTAIN = {"uncertain_completion", "uncertain_grouping"}
_ACTION = re.compile(
    r"^(?:ask|choose|confirm|review|compare|decide|provide|select|approve|write|draft|edit|update|"
    r"test|check|verify|submit|contact|schedule|book|collect|send|read|inspect|request|create|build|"
    r"prepare|define|identify|add|remove|fix|resolve|coordinate|share|publish|outline|measure|"
    r"calculate|summarize|organize|label|mark|upload|install|configure|open|close)\b\s+\S.{1,}$",
    re.IGNORECASE)


def _date(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProposalError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProposalError(f"{label} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ProposalError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc)


def _source_is_available(source: Mapping[str, Any], now: datetime,
                         stale_after_seconds: int) -> bool:
    available = source.get("available")
    if available is not None and (type(available) not in (bool, int) or available not in (0, 1)):
        return False
    freshness = source.get("freshness")
    if freshness is not None and (not isinstance(freshness, str) or freshness not in {"fresh", "stale"}):
        return False
    if available is None and freshness is None:
        return False
    if source.get("available") is False or source.get("available") == 0:
        return False
    if freshness == "stale":
        return False
    last_seen = source.get("last_seen_at")
    if last_seen:
        try:
            seen_at = _date(last_seen, "source last_seen_at")
            return seen_at <= now and now - seen_at <= timedelta(seconds=stale_after_seconds)
        except ProposalError:
            return False
    return freshness == "fresh" or source.get("available") is True or source.get("available") == 1


def _trusted_active_thread(source: Mapping[str, Any], thread_id: str) -> bool:
    """Honor an explicit active signal supplied by a trusted source adapter."""
    threads = source.get("threads", {})
    if not isinstance(threads, Mapping):
        return False
    state = threads.get(thread_id)
    if isinstance(state, Mapping):
        status = state.get("status")
        activity = state.get("activity")
        return (state.get("active") is True or
                (isinstance(status, str) and status in {"active", "running", "in_progress"}) or
                (isinstance(activity, str) and activity in {"active", "running", "in_progress"}))
    return isinstance(state, str) and state in {"active", "running", "in_progress"}


def _thread_coverage_complete(source: Mapping[str, Any], thread_id: str) -> bool:
    """Require explicit, gap-free coverage for inferred inactivity claims."""
    threads = source.get("threads")
    if not isinstance(threads, Mapping):
        return False
    state = threads.get(thread_id)
    if not isinstance(state, Mapping):
        return False
    if state.get("coverage_complete") is not True and state.get("completeness") != "complete":
        return False
    gaps = state.get("gaps")
    return isinstance(gaps, (list, tuple)) and len(gaps) == 0


def _source_coverage_complete(source: Mapping[str, Any]) -> bool:
    coverage = source.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("state") != "complete":
        return False
    gaps = coverage.get("gaps")
    return isinstance(gaps, list) and len(gaps) == 0


def validate_proposal(payload: Any, stored_evidence: set[str] | Mapping[str, Any], *,
                      now: datetime | None = None, sources: Mapping[str, Any] | None = None,
                      inferred_after_days: int = 3,
                      source_stale_after_seconds: int = 3600) -> dict[str, Any]:
    """Validate a proposal and compute its canonical attention eligibility.

    The reviewer must provide a status and grouping confidence. Progress age
    is derived from the newest source timestamp among stored evidence
    references; the reviewer cannot supply a timestamp to bypass the threshold.
    """
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ProposalError("proposal must contain exactly the supported assessment fields")
    project_id = payload["project_id"]
    if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
        raise ProposalError("project_id is invalid")
    values: dict[str, str] = {}
    for key in ("outcome", "next_action", "reasoning"):
        value = payload[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ProposalError(f"{key} must be a non-empty string of at most 4000 characters")
        values[key] = value.strip()
    refs = payload["evidence_refs"]
    if (not isinstance(refs, list) or not refs or len(refs) > 500 or
            any(not isinstance(ref, str) or not ref or len(ref) > 256 for ref in refs)):
        raise ProposalError("evidence_refs must be a non-empty list of stored observation ids")
    if not isinstance(stored_evidence, Mapping):
        raise ProposalError("stored evidence metadata is required")
    evidence_ids = set(stored_evidence)
    if len(set(refs)) != len(refs) or not set(refs).issubset(evidence_ids):
        raise ProposalError("every evidence reference must identify a distinct stored observation")
    revisions = payload["evidence_revisions"]
    if (not isinstance(revisions, dict) or set(revisions) != set(refs) or
            any(not isinstance(value, str) or not value for value in revisions.values())):
        raise ProposalError("evidence_revisions must map every evidence reference to its current revision")
    for ref in refs:
        row = stored_evidence[ref]
        if (not isinstance(row, Mapping) or
                any(not isinstance(row.get(key), str) or not row[key] for key in
                    ("source_id", "thread_id", "revision", "source_timestamp"))):
            raise ProposalError("stored evidence metadata is incomplete")
        if revisions[ref] != row["revision"]:
            raise ProposalError("evidence revision changed since review input was read")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ProposalError("policy clock must be timezone-aware")
    current = current.astimezone(timezone.utc)
    status = payload["status"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise ProposalError("status is not a supported reviewer classification")
    confidence = payload["grouping_confidence"]
    if not isinstance(confidence, str) or confidence not in {"strong", "uncertain"}:
        raise ProposalError("grouping_confidence must be strong or uncertain")
    if inferred_after_days < 0 or source_stale_after_seconds < 0:
        raise ValueError("policy thresholds must be non-negative")

    evidence = {ref: stored_evidence[ref] for ref in refs} if isinstance(stored_evidence, Mapping) else {}
    source_map = sources if isinstance(sources, Mapping) else {}
    referenced_sources = {row["source_id"] for row in evidence.values()}
    unavailable = sorted(source_id for source_id in referenced_sources
                         if source_id not in source_map or not isinstance(source_map[source_id], Mapping) or
                         not _source_is_available(source_map[source_id], current, source_stale_after_seconds))
    trusted_active = any(source_id in source_map and isinstance(source_map[source_id], Mapping) and
                         _trusted_active_thread(source_map[source_id], row["thread_id"])
                         for row in evidence.values() for source_id in [row["source_id"]])
    coverage_complete = all(
        source_id in source_map and isinstance(source_map[source_id], Mapping) and
        _source_coverage_complete(source_map[source_id]) and
        _thread_coverage_complete(source_map[source_id], thread_id)
        for source_id, thread_id in {(row["source_id"], row["thread_id"]) for row in evidence.values()})

    eligible = False
    reason = "proposal_only"
    referenced_pairs = {(row["source_id"], row["thread_id"]) for row in evidence.values()}
    thread_evidence = [row for row in stored_evidence.values() if isinstance(row, Mapping) and
                       (row.get("source_id"), row.get("thread_id")) in referenced_pairs]
    progress_dates = []
    for row in thread_evidence:
        source_timestamp = row.get("source_timestamp")
        if not isinstance(source_timestamp, str) or not source_timestamp:
            raise ProposalError("stored source evidence has no valid timestamp")
        progress_at = _date(source_timestamp, "source_timestamp")
        if progress_at > current:
            raise ProposalError("stored source evidence cannot be in the future")
        progress_dates.append(progress_at)
    watermark_invalid = False
    watermark_dates = []
    newer_watermark = False
    for source_id, thread_id in {(row["source_id"], row["thread_id"]) for row in evidence.values()}:
        source = source_map.get(source_id, {})
        thread_map = source.get("threads", {}) if isinstance(source, Mapping) else {}
        thread = thread_map.get(thread_id, {}) if isinstance(thread_map, Mapping) else {}
        watermark = thread.get("last_progress_at") if isinstance(thread, Mapping) else None
        if watermark is None:
            continue
        try:
            watermark_at = _date(watermark, "last_progress_at")
            captured = _date(thread.get("last_progress_captured_at"), "last_progress_captured_at")
            revision = thread.get("last_progress_revision")
            if not isinstance(revision, str) or not revision or watermark_at > current or captured > current:
                raise ProposalError("invalid or future source progress watermark")
            watermark_dates.append(watermark_at)
            thread_rows = [row for row in thread_evidence if row.get("source_id") == source_id and
                           row.get("thread_id") == thread_id]
            if thread_rows:
                latest_row = max(thread_rows, key=lambda row: (
                    _date(row.get("source_timestamp"), "source_timestamp"),
                    _date(row.get("captured_at"), "captured_at")))
                raw_at = _date(latest_row.get("source_timestamp"), "source_timestamp")
                raw_captured = _date(latest_row.get("captured_at"), "captured_at")
                if (watermark_at > raw_at or
                        (watermark_at == raw_at and captured > raw_captured) or
                        (revision != latest_row.get("revision") and captured > raw_captured)):
                    newer_watermark = True
        except (ProposalError, TypeError, ValueError):
            watermark_invalid = True
    progress_dates.extend(watermark_dates)
    latest_progress = max(progress_dates) if progress_dates else None
    canonical_progress = (latest_progress.isoformat(timespec="seconds").replace("+00:00", "Z")
                          if latest_progress else None)
    if trusted_active:
        reason = "active_work"
    elif status == "explicit_request":
        if watermark_invalid:
            reason = "invalid_progress_watermark"
        elif newer_watermark:
            reason = "newer_progress_not_reviewed"
        elif unavailable:
            reason = "source_unavailable"
        elif confidence == "uncertain":
            reason = "uncertain_grouping"
        else:
            eligible = True
            reason = "explicit_request"
    elif status == "inferred":
        if not _ACTION.fullmatch(values["next_action"].strip()):
            reason = "missing_concrete_action"
        elif latest_progress is None:
            reason = "missing_progress_evidence"
        elif unavailable:
            reason = "source_unavailable"
        elif watermark_invalid:
            reason = "invalid_progress_watermark"
        elif newer_watermark:
            reason = "newer_progress_not_reviewed"
        elif not coverage_complete:
            reason = "coverage_incomplete"
        else:
            if confidence == "uncertain":
                reason = "uncertain_grouping"
            elif current - latest_progress < timedelta(days=inferred_after_days):
                reason = "progress_too_recent"
            else:
                eligible = True
                reason = "inferred_threshold_met"
    elif status == "active_work":
        reason = "active_work"
    elif status == "external_dependency":
        reason = "external_dependency"
    elif status in _UNCERTAIN:
        reason = status
    elif status == "no_action":
        reason = "no_action"

    if unavailable and eligible:
        eligible = False
        reason = "source_unavailable"

    return {
        "project_id": project_id,
        **values,
        "evidence_refs": sorted(refs),
        "evidence_revisions": {ref: revisions[ref] for ref in sorted(refs)},
        "status": status,
        "last_progress_at": canonical_progress,
        "grouping_confidence": confidence,
        "kind": "attention" if eligible else "proposal",
        "eligible": eligible,
        "eligibility_reason": reason,
    }
