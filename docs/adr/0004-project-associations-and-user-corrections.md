# 0004: Separate inferred associations from user grouping corrections

Status: Accepted for the Codex-only milestone.

## Context

The glossary allows a thread to contain evidence toward several outcomes.
Deletion must explain shared-thread consequences. An exclusive assignment
created automatically by the reviewer would prevent legitimate shared
evidence and conflate a model inference with an authoritative user decision.

## Decision

Represent inferred project/thread associations as many-to-many. Persist an
explicit user grouping correction separately; future model proposals cannot
undo that correction. Moving a conversation updates affected project evidence
and invalidates pending attention until a fresh assessment.

Uncertain or conflicting proposals remain separate from established project
content. A new candidate that could merely rename an archived, closed, or
snoozed project is suppressed rather than bypassing the existing decision.
Shared observations remain reviewable when an existing eligible project still
needs them. Source and conversation exclusions remain authoritative across
all project associations.

Deletion previews identify every project sharing the affected conversations.
The retention/deletion implementation must purge or invalidate affected
derived evidence consistently and preserve minimal re-import exclusions.

## Consequences

Project identity stays distinct from thread identity. User corrections and
lifecycle choices take precedence without pretending every conversation has
exactly one outcome. Conservative alias suppression can require an explicit grouping correction;
it cannot silently reactivate a hidden project.
