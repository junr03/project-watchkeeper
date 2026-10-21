# 0006: Separate review progress from notification acceptance

Status: Accepted for the Codex-only milestone.

## Decision

Persist daily review work, bounded history cursors, leases and typed failures
separately from project state and email delivery. Calendar schedules use an
explicit time zone; persisted instants use UTC. After sleep, resume committed
progress and coalesce missed days instead of replaying every missed schedule.
Authentication and quota failures back off without advancing the review cursor.
Reserve the first daily window for recent activity, then resume the independent
historical cursor within the remaining daily budget. Recent windows order by
capture time so edits to old source messages are promptly reconsidered; history
orders by source time. A recent result never advances the historical cursor. Require at least two
windows so recent activity and historical discovery both make progress.

Bound reviewer input by both observation count and serialized size. Keep thread
context together when it fits, and report partial context when it does not.
Persist content-free identities/revisions for the context a lease observed.
Reject results if that context or the lease changed. A result's citations need
not include every message that was considered, but all cited revisions must
still be current. Expired raw context must not turn an old retained request into
new evidence of inactivity or an unanswered request.
Expose current raw observations and valid retained project evidence through one
queryable relation. Raw rows take precedence; a retained snapshot is usable
only when it matches the latest expired revision. The relation stores no extra
conversation content and lets bounded cursors include retained evidence.

Aggregate eligible findings into one digest with fixed configured transport
and recipient. Persist the exact batch membership and identifier before sending.
Meaningful finding deduplication ignores a growing citation list and distinguishes
successful acceptance from cancellation or definite failure. An ambiguous
external transport outcome remains recorded and is not automatically resent.
There is no exactly-once SMTP guarantee.

The local capture sink can recognize a previously accepted batch by its stable
identifier. It fsyncs the captured file and directory before reporting acceptance.
On recovery, distinguish an already accepted capture from a new send, and
recheck current decisions, source freshness and evidence before a new send.

Serve a state directory from one application process. Serialize mutations,
review snapshot claims and bounded transport acceptance within that process;
the container entrypoint must enforce exclusive ownership of the state. Privacy
deletion waits for an active transport attempt, then invalidates affected leases,
assessment metadata, batches and attempts. Purge hooks return aggregate capture
identifiers to the privacy journal so cleanup also survives restore and crashes.

## Consequences

A failed or interrupted review cannot look like a successful empty review.
Persistent login/collection failures produce separate deduplicated notices;
ordinary laptop sleep only changes freshness. Project decisions remain
authoritative across review, notification and restart boundaries.

Database backups must retain the current external privacy journal during
restore. App-controlled captures can be removed; mail already accepted by an
external server cannot be recalled. Multiple independently serving application
processes against one state directory are outside this milestone's contract.
