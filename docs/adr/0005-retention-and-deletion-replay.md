# 0005: Bound deletion by known threads and replay privacy decisions

Status: Accepted for the Codex-only milestone.

## Decision

Keep raw transcript retention separate from project evidence. Expire raw
captures after the configured period, retaining content-free revision/capture
metadata so rereading an identical old revision cannot reset its age. Preserve
specific evidence supporting unresolved attention items, including archived
open projects, until deletion.

Deleting a project removes its record and the captured content of its known
threads. The confirmation identifies other projects sharing those threads.
Clear their affected derived finding text and eligibility, remove matching
evidence and queued delivery, and preserve their user disposition, archive
flag, unrelated evidence and unrelated thread associations. Do not recursively
expand deletion through every project connected by a shared thread.

Persist content-free deletion/exclusion/re-enablement events in a separately
protected append-only journal before applying their SQLite mutations. Replay
missing events before serving a restored database. If the database's applied
journal position is ahead of the available journal, fail closed instead of
silently accepting an incomplete privacy history. Keep this journal current
and separate from database rollback snapshots.

Deletion removes retention revision tombstones for its conversations and
leaves only their durable exclusion identifiers in SQLite. Explicit
re-enablement authorizes collection again. If recovery discovers captured
email IDs in a restored snapshot, append a content-free cleanup record before
committing the deletion so a crash before file unlink can be retried later.

## Consequences

A stale backup cannot silently restore deleted content when the current
journal is supplied. Old backup media still requires ordinary operator expiry;
mail already delivered outside Watchkeeper cannot be recalled. App-controlled
captured email files are part of deletion. Journal identifiers reveal which
source/thread identifiers were excluded, but contain no conversation text,
project summaries, credentials, or notification bodies.

The service can invalidate a shared project's finding without erasing unrelated
source work or overriding its lifecycle decision. A fresh assessment of its
remaining evidence is required before it can generate another reminder.
