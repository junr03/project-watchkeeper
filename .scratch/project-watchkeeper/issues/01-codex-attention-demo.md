# 01: Demonstrate a Codex project becoming an attention item

**What to build:** Turn synthetic Codex threads into a persistent project, an evidence-supported attention item on an authenticated page, and a captured email digest. This is the first complete demo, before other ticket work expands in parallel. Include one persistent snooze action and retained visibility during simulated Mac sleep.

**Blocked by:** None (no code or decision blockers).

**Business decisions and activation gates:** No unresolved business decisions. Use fabricated conversations, controlled reviewer responses, simulated time/availability, and a local email sink. No private account login, actual recipient, or Electricpeak service is required.

**Demo:** Start the application with a fabricated unfinished project spanning two Codex threads. See its outcome, supporting evidence, and next action on the authenticated page and in one captured digest. Snooze it, restart, simulate sleep/wake, and rerun review: the decision and project survive without a duplicate reminder.

**Status:** ready-for-agent

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Establish the smallest runnable flow from fixture observations through persisted projects to the authenticated page and email sink | None | Unassigned | Planned | A repeatable application-level scenario displays the expected evidence and captures one digest |
| W2 | Add durable snooze, basic notification deduplication, and source freshness for the demo | W1 | Unassigned | Planned | Restart and simulated sleep/wake preserve project/snooze state and suppress unchanged delivery |
| W3 | Document and verify the stable external boundaries used by later slices | W1, W2 | Unassigned | Planned | Acceptance tests operate through intake and user actions; source/reviewer/clock/email substitutes work without internal-state assertions |

## Acceptance criteria

- [ ] A documented command or short procedure runs the complete synthetic demo from a clean local environment without private data.
- [ ] Multiple Codex-thread fixtures produce one expected project and an attention item with outcome, next action, and evidence locators.
- [ ] The authenticated page and a fixed-recipient local sink show the same finding; an unauthenticated request cannot read project data.
- [ ] A persisted snooze and unchanged-review deduplication survive application restart.
- [ ] Simulated Mac sleep retains project visibility and marks observations stale; waking does not duplicate previously imported observations or notifications.
- [ ] Opening or previewing an email link cannot mutate project state.
- [ ] Fixtures substitute only external inputs; real intake, register, user-action enforcement, and notification logic are exercised.
- [ ] The demo establishes observation, assessment-proposal, and delivery boundaries for later tickets; it does not claim live Codex compatibility or real reviewer accuracy.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)
