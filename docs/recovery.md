# Monitoring and recovery

Watchkeeper persists review progress, worker leases, typed health incidents,
and digest membership in additive SQLite tables. These records contain IDs,
revision tokens, cursors, and typed status only. A leased review snapshot is
rebuilt from current observations; the application does not persist a second
copy of transcript text for the model job.

## Schedules and bounded review

Configure `WATCHKEEPER_SCHEDULE_TIMEZONE` with an IANA time zone, plus
`WATCHKEEPER_REVIEW_TIME` and `WATCHKEEPER_DIGEST_TIME` as local `HH:MM` values.
The application creates at most one scheduled review and one digest batch per
local date. `WATCHKEEPER_REVIEW_WINDOW_LIMIT` bounds observations in one model
call, and `WATCHKEEPER_REVIEW_WINDOWS_PER_DAY` caps backfill windows per day.
`WATCHKEEPER_REVIEW_WINDOW_BYTES` bounds the serialized input as well; accepted
values range from 180000 to 220000 bytes so one maximum-size observation with
worst-case JSON escaping can fit. Project/proposal context is capped and
transcript rows advance through source-ordered windows when the byte ceiling is
reached. Each local day starts with a bounded newest-evidence window, then uses
remaining window slots for the persisted historical backfill cursor. At least
two windows per day are required so both streams can make progress.
After sleep, missed windows from earlier dates are superseded; the next daily
run resumes from the last successfully committed cursor instead of draining a
large stale queue.

The newest-evidence window is ordered by capture time, then source timestamp
and stable message identity, so a newly edited old message is reviewed promptly.
Its ordering never changes the independent historical cursor. Historical
backfill windows are ordered by source timestamp, then capture time and stable
message identity. If a window splits a thread, the worker receives explicit incomplete
thread coverage. Policy still rejects inferred inactivity claims against that
partial context. A thread larger than a window advances through older messages
over later windows, so it does not block other days indefinitely. The cursor
advances only after the submitted result is accepted against the active lease
and unchanged evidence snapshot. A failed review is retried with typed,
exponential backoff and never becomes an empty successful review.

The reviewer worker checks its configured subscription OAuth profile before it
claims or reads a review window. It polls the narrow Unix-socket broker for a
leased job, submits one bounded result, or reports one of the supported failure
kinds: `quota`, `authentication`, `unavailable`, `invalid_output`, or
`protocol`. Authentication preflight failures use a separate health report and
do not require a job or access to evidence. There is no API-key fallback.

An incoming deletion, exclusion, user decision, observation revision, new
message, or changed trusted thread-progress watermark invalidates an in-flight
result. Review submissions are rechecked against the current service state;
stale job results are rejected and their cursor is not advanced. Per-thread
progress watermarks from the retention layer also censor old retained evidence:
if newer progress exists outside the reviewed evidence, explicit reminders and
inferred inactivity claims remain proposals until current context is reviewed.

## Health notices

Collection health uses source-adapter failure kinds (`authentication`,
`quota`, `unsupported`, `unavailable`, `protocol`); a sleeping but otherwise
healthy source is shown as stale without inventing a retrieval failure.
Reviewer failures use the typed set above. A failure is recorded once per
distinct attempt. Two or more failures with the same source/reviewer and kind
open one notice. A healthy collection or accepted review resolves it. Notices
appear separately in the authenticated page and in a distinct section of the
digest, rather than being presented as project findings.

## Daily digest and delivery recovery

One scheduled digest aggregates all currently eligible new attention items
and open collection/review notices. The manual `/api/deliver` action captures
the current queue for local review; its `delivered` count is the number of
finding rows covered, while `emails` is the number of messages captured.
Finding fingerprints use project identity, outcome, and next action. They do
not change when evidence references grow. A cancelled item can be re-armed by
a fresh eligible review; delivered and ambiguous items remain deduplicated.

Before an item enters a batch, delivery checks current project eligibility,
lifecycle and snooze state, source freshness, exclusions, evidence revisions,
thread activity, newer progress watermarks, and a content-free fingerprint of
all current observations in every cited thread. A new or revised message after
assessment invalidates delivery, including same-second changes. A digest claims
an immutable membership list and deterministic delivery ID before transport. A sink that
supports idempotent acceptance can retry that exact membership and ID after a
restart. An SMTP-style sink without an idempotency key records uncertain
acceptance as `ambiguous` and does not resend automatically. The notification
history does not promise exactly-once SMTP delivery.

Project deletion purges attempt and batch-membership metadata and returns the
aggregate capture IDs to the privacy deletion queue so captured files can be
removed. The service serializes delivery and deletion within one process; stale
`sending` claims from a prior crash are purged during deletion recovery. Run one
serving Watchkeeper process per state directory. This lock does not coordinate
separate processes. Already accepted external mail cannot be recalled.

The application scheduler starts with the service. The separate reviewer
worker needs only the configured broker socket and reviewer token; it does not
mount the database or a container-runtime socket. Keep the broker token and
OAuth runtime configuration outside public source control.
