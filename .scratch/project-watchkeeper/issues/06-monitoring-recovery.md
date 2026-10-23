# 06: Keep monitoring reliable through backlog and interruptions

**What to build:** Make bounded historical discovery and scheduled reviews recover from ordinary device sleep, interrupted work, subscription limits, expired access, and delivery failures. Show accurate coverage/freshness and separate collection notices while maintaining useful, deduplicated daily digests.

**Blocked by:** [02: Collect actual Codex threads with visible coverage](02-codex-collection-coverage.md) and [03: Assess unfinished outcomes through restricted OpenClaw review](03-restricted-openclaw-review.md), for the source and reviewer interfaces and failure signals.

**Business decisions and activation gates:** No unresolved business decisions. Real subscription quotas, private recipient credentials, and production scheduling are activation concerns. Inject failure responses and use the local email sink to verify recovery deterministically.

**Demo:** Process a finite historical backlog across several capped runs, interrupt ingestion and review, simulate quota/login failures and Mac sleep/wake, and restart during email acceptance. Observe resumed progress, honest gaps, one meaningful collection notice per unchanged problem, and no loss of persisted decisions or successful-delivery history.

**Status:** ready-for-agent

**Execution:** Implemented; coordinator-verified recovery checkpoint. Combined lifecycle/privacy acceptance remains in ticket 07.

**Branch:** `jnr/06-monitoring-recovery`, based on verified collection/reviewer checkpoint `87cf00e`.

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Run bounded newest-first historical discovery and daily review with durable checkpoints | None | Luna `collection_luna` / coordinator verification | Verified synthetic checkpoint | Multiple capped runs converge on available history and resume after interruption without falsely declaring complete coverage |
| W2 | Turn source/reviewer failures into visible health state and deduplicated collection notices | W1 | Luna `collection_luna` / coordinator verification | Verified synthetic checkpoint | Sleep, quota, expired login, and persistent retrieval failures produce the specified distinct UI/digest behavior |
| W3 | Make digest scheduling and outbox recovery preserve meaningful deduplication | W1, W2 | Luna `collection_luna` / coordinator verification | Verified synthetic checkpoint | Crash and ambiguous-acceptance scenarios preserve delivery evidence and avoid treating failed review as empty success |

## Acceptance criteria

- [x] Discovery processes all available history incrementally, newest updated threads first, within configurable work limits and with explicit coverage gaps.
- [x] Daily review and digest schedules/time zones are configurable; unchanged non-actionable runs stay silent.
- [x] Source sleep preserves observations and marks them stale; missing observations do not strengthen unsupported no-progress claims.
- [x] Quota exhaustion, expired login, and persistent retrieval failures preserve checkpoints and resume when access/capacity returns.
- [x] Persistent failures appear as separate deduplicated collection notices; a failed review is never recorded as successful with zero findings.
- [x] No paid model API fallback or silent billing/authentication change occurs.
- [x] The durable outbox deduplicates meaningful findings against successful delivery and records ambiguous email acceptance honestly.
- [x] Restart recovery preserves user decisions and notification history established by the application contract.
- [ ] Use existing decision interfaces without taking ownership of another ticket's in-flight changes; verify combined lifecycle/privacy scenarios in ticket 07.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)

## Verification

See `docs/evidence-06.md`. No private account, hosted model, external recipient,
or Electricpeak service was activated. Combined privacy and container acceptance
is recorded separately in ticket 07.
