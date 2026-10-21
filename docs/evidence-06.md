# Ticket 06 verification

The coordinator independently passed the 64-test stack, then all 23 recovery
checks after the final sizing and retry refinements. The implementation agent
also passed the final 66-test stack. Compilation and diff checks passed.

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

This checkpoint unblocks privacy integration and ticket 07 packaging. Combined
privacy replay, retained-evidence scheduling, container restart and SMTP
acceptance are verified separately before final milestone delivery.
