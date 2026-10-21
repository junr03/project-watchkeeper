# 05: Expire and delete stored project information

**What to build:** Give the user configurable transcript retention and explicit project deletion that consistently affects stored content, derived material, and pending notifications. Keep minimal exclusions to prevent re-import, while retaining archived project records under the normal retention policy.

**Blocked by:** [04: Make project decisions persist across reviews](04-persistent-project-decisions.md), for durable exclusions and project dispositions.

**Business decisions and activation gates:** No unresolved business decisions. Production backup destinations and schedules are deployment choices. Provide and test deletion replay with a controlled backup/restore fixture; do not change host backups.

**Demo:** Advance simulated time beyond the default retention period and inspect the remaining project evidence. Delete a project through the page, retry imports and queued delivery, then restore a controlled older snapshot and replay the deletion record. Deleted material does not return to active storage, search, the page, or outgoing digests.

**Status:** ready-for-agent

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Enforce configurable transcript expiry while preserving required unresolved-project evidence | None | Unassigned | Planned | Time-controlled acceptance checks expire raw transcripts after 30 days by default and retain the specified summaries/evidence |
| W2 | Implement confirmed deletion through content, derived stores, pending delivery, and exclusion state | W1 | Unassigned | Planned | Deleted content disappears from externally visible surfaces and imports remain excluded until re-enabled |
| W3 | Verify shared-thread consequences, archive interaction, and backup/restore deletion replay | W1, W2 | Unassigned | Planned | The UI explains shared impact before deletion; archive follows retention; an older restored snapshot cannot silently resurrect deleted content |

## Acceptance criteria

- [ ] Raw captured transcripts expire after 30 days by default; the retention setting is configurable and validated.
- [ ] Summaries and specific evidence supporting unresolved attention items remain until user deletion.
- [ ] Archive preserves the project record but does not prevent ordinary transcript expiry.
- [ ] Deletion requires an explicit authenticated action and explains known shared-thread effects before confirmation.
- [ ] Project summaries/evidence, affected derived caches/indexes, and queued notifications are removed or invalidated consistently.
- [ ] Only minimal content-free exclusion identifiers remain for the known threads; repeated import does not recreate deleted content.
- [ ] Re-enabling import requires an explicit user action; source conversations are never deleted or changed.
- [ ] Document backup-expiry limits and verify restore-time deletion replay using a local fixture rather than promising immediate erasure of every backup.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)
