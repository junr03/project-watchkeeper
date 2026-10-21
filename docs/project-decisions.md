# Persistent project decisions

The authenticated control page and its JSON endpoints are the user boundary
for project lifecycle, snooze, archive, exclusions, and thread grouping. Page
loads and email links are read-only. Every state-changing endpoint requires
Basic authentication, the CSRF cookie/header pair, and an `Origin` matching
`WATCHKEEPER_PUBLIC_BASE_URL`.

Project actions are `complete`, `abandon`, `reopen`, `snooze`, `archive`, and
`restore` at `POST /api/projects/{project_id}/actions/{action}`. The initial
`POST /api/projects/{project_id}/snooze` endpoint remains supported. Complete
and abandon are durable lifecycle choices. Only `reopen` returns either to
the open lifecycle. Archive is a separate reversible flag: it hides a record
from the active view and pauses reviewer input and reminders without changing
its lifecycle or deleting its evidence. Restore and reopen clear current
eligibility until a fresh review has assessed current evidence.

`POST /api/sources/{source_id}/exclude|include` and
`POST /api/threads/{source_id}/{thread_id}/exclude|include` persist source and
conversation exclusions. The corresponding tables are `excluded_sources`
and `excluded_threads`; collectors must skip excluded observations before
writing them while still committing a successful checkpoint. The service
also filters old stored observations at review time, so exclusions take
effect before any collection code is updated. Minimal source/thread identity
and the decision timestamp are retained to prevent rediscovery. Re-enabling
removes that exclusion row; it does not delete retained observations.

Discovered grouping is many-to-many in `project_threads`: one conversation
can support several outcomes. `thread_ownership`, keyed by `(source_id,
thread_id)`, records only an explicit user correction. `POST
/api/threads/{source_id}/{thread_id}/assign` can move a conversation into an
existing open project (merge) or give it a new project identifier (split).
Group corrections invalidate pending reminders and affected assessments
until new evidence is reviewed. Review proposals that conflict with an
explicit owner are saved separately and do not rewrite any project's outcome,
action, evidence, or user disposition. A new proposal that reuses evidence
from a closed, archived, or snoozed project is suppressed without storing new active content. Reviewer
proposals cannot claim ownership unless policy has established an eligible,
actionable assessment.

## Assessment and notification precedence

The service stores policy-derived assessment kind, eligibility, reason,
status, grouping confidence, and progress time alongside each project. Only
`kind=attention` with `eligible=true`, an open disposition, no live snooze,
and no archive flag can queue a digest. A non-actionable proposal is visible
on the page but is not emailed. An ineligible attention assessment cancels
pending reminders without replacing the last concrete project finding.

A live snooze hides its observations from reviewer input and cancels pending
delivery. Its expiry does not itself send a reminder: the next assessment
after expiry is required to queue one. Complete, abandon, archive, and
exclusions cancel pending outbox entries. Delivery rechecks lifecycle,
archive, eligibility, and snooze state before writing the configured sink.

## Storage evolution and retention hooks

SQLite schema version 3 migrates version 1 through version 2 in transactions, preserving
existing observations, projects, sources, and outbox rows. A failed migration
rolls back and leaves `user_version` unchanged; databases newer than this
service are rejected. Version 2 adds assessment metadata, reversible archive
state, content-free exclusion identifiers, thread ownership, and a separate
review proposal table. The migration has no destructive rollback step; an
operator can restore the pre-migration database backup if downgrade is
needed.

Ticket 05 retention/deletion should remove affected observation text,
project evidence, review proposals, and pending/delivered notification data
according to its policy while preserving the minimal exclusion identifiers
needed to block re-import. `project_threads` provides the shared-thread impact
set for deletion preview; `thread_ownership` preserves explicit user
corrections. Archive is not a retention exemption. Backup
expiry and replay of deletion decisions remain separate operator tasks; this
service does not promise immediate erasure from old backups.
