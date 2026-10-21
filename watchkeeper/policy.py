"""Deterministic validation for reviewer assessment proposals.

validate_proposal() accepts only an evidence-backed attention proposal and
returns the canonical assessment used by the service. Later review work can
extend this contract without giving the reviewer delivery or action authority.
"""

import re
from datetime import datetime
from typing import Any, Mapping


class ProposalError(ValueError):
    pass


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FIELDS = {"project_id", "outcome", "next_action", "reasoning", "evidence_refs"}


def validate_proposal(payload: Any, stored_evidence: set[str] | Mapping[str, Any], *,
                      now: datetime | None = None, sources: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate proposal fields and evidence references against persisted evidence.

    `now` and `sources` are explicit policy inputs for later freshness and
    threshold rules. The initial slice accepts only direct, evidence-backed
    attention and therefore marks valid proposals eligible.
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
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
        raise ProposalError("evidence_refs must be a non-empty list of stored observation ids")
    evidence_ids = set(stored_evidence)
    if len(set(refs)) != len(refs) or not set(refs).issubset(evidence_ids):
        raise ProposalError("every evidence reference must identify a distinct stored observation")
    return {"project_id": project_id, **values, "evidence_refs": sorted(refs), "kind": "attention", "eligible": True}
