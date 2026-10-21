# Transcript retention and deletion

Watchkeeper retains raw conversation text for 30 days by default. Set
`WATCHKEEPER_TRANSCRIPT_RETENTION_DAYS` to an integer from 1 through 36,500 to
change that period. `WATCHKEEPER_PRIVACY_JOURNAL` selects the append-only
privacy journal; when omitted, it is stored beside the database as
`<database>.privacy.jsonl`. Keep that journal outside ordinary database
rollback snapshots, back it up independently, and restrict its parent
directory and file permissions. The journal contains source, thread, and
delivery identifiers plus timestamps, never conversation text or credentials.

Unresolved project evidence remains available to support reassessment, even
when the project is archived. This includes its specific evidence snapshots;
those snapshots are removed during later maintenance after the project is
completed or abandoned and their original capture age exceeds retention.
Repeated import of an already-expired revision does not reset its capture age.
A newer revision can be captured after any applicable conversation exclusion
has been explicitly lifted.

Project deletion is a two-step authenticated action. The preview identifies the
project's known conversations and other projects that directly share them. A
confirmation token is tied to those current associations; if they change, the
server rejects the old confirmation. Deletion removes the selected project,
captured content for its known conversations, and derived findings/evidence
that depend on those conversations in directly affected projects. Their
user-selected disposition and archive state remain. Other evidence and
conversation associations remain intact. The deleted conversations are
excluded from future collection. A separate authenticated re-enable action is
recorded in the journal and permits later collection, including revisions
previously observed before deletion. Deletion retains only the source/thread
exclusion identifiers in SQLite; normal retention tombstones are removed for
those deleted conversations.

The journal is fsynced before a deletion or exclusion change is applied to
SQLite. On startup, missing journal events are replayed before the service
accepts requests. If SQLite says it has applied a sequence beyond the
available journal, startup fails closed. Restores therefore require both a
database backup and the current privacy journal. Do not replace the journal
with a copy from the same older backup as the database.

When recovery finds additional captured-email IDs in a restored database, it
adds a content-free cleanup event before committing deletion. These cleanup
events remain as retry records, so a crash between the SQLite commit and file
unlink is repaired on the next startup. Registered purge hooks can also return
additional capture IDs for aggregate emails that include affected projects.

SQLite enables `secure_delete`, but this is logical deletion, not a guarantee
of forensic erasure. WAL files, filesystem snapshots, storage-device behavior,
host backups, copied email, and mail already delivered outside Watchkeeper can
retain prior bytes. Keep database, WAL, email-capture, and backup storage under
the operator's retention and access controls. Deletion removes matching
app-controlled captured `.eml` files when an email-capture directory is
configured; it cannot recall messages already delivered elsewhere.

The service exposes `expire_retention()` for a later maintenance scheduler.
Deletion also provides a `privacy_purge_hooks` constructor seam. Each hook runs
inside the deletion transaction with the directly affected `project_ids`,
`threads` (`(source_id, thread_id)` pairs), `observation_ids`, and
`delivery_ids`, so scheduler caches and delivery-attempt metadata can be
removed atomically with core records. A hook may return additional capture
IDs; the service journals those IDs before commit and removes their `.eml`
files afterward.
