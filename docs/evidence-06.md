# Ticket 06 verification

The coordinator independently passed the 64-test stack, then all 23 recovery
checks after the final sizing and retry refinements. The implementation agent
also passed the 67-test recovery branch after separating recent capture ordering
from the historical cursor. The coordinator independently passed all 24 recovery
checks, 32 combined recovery/privacy/evidence checks, and the 85-test integrated
suite under the pinned Python 3.12 container with networking disabled, read-only
source/root, non-root UID and ResourceWarning errors enabled. Compilation and
diff checks passed.

Deterministic application checks exercise bounded source-ordered windows,
whole-thread context when it fits, escaped-JSON byte limits, the final unconsumed
row, recent activity alongside independent backfill, revision/lease races,
time zones, missed-day coalescing, typed quota/authentication backoff, repeated
failure/recovery cycles, and meaningful notification deduplication.

Digest checks cover one aggregate message, durable membership, definite
nonacceptance retries, changed actions before retry, stale source/evidence,
accepted local capture after a lifecycle change, ambiguous acceptance including
notice-only messages, and privacy-hook cleanup of shared aggregate batches.

The coordinator ran the revised one-shot worker in the actual pinned OpenClaw
container with no network, no credentials, read-only root/configuration,
non-root UID, dropped capabilities and no-new-privileges. It exited 4 with
only an authentication failure status, before reading evidence. Subscription
login, renewal, actual quotas and model judgment remain unverified.

The final combined 93-test suite, privacy replay, retained-evidence scheduling,
container restart and actual SMTP acceptance checks passed independently. See
[portable milestone evidence](evidence-07.md).
