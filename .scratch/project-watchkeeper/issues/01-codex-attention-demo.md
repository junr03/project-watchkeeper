# 01: Demonstrate a Codex project becoming an attention item

**What to build:** Turn synthetic Codex threads into a persistent project, an evidence-supported attention item on an authenticated page, and a captured email digest. This is the first complete demo, before other ticket work expands in parallel. Include one persistent snooze action and retained visibility during simulated Mac sleep.

**Blocked by:** None (no code or decision blockers).

**Business decisions and activation gates:** No unresolved business decisions. Use fabricated conversations, controlled reviewer responses, simulated time/availability, and a local email sink. No private account login, actual recipient, or Electricpeak service is required.

**Demo:** Start the application with a fabricated unfinished project spanning two Codex threads. See its outcome, supporting evidence, and next action on the authenticated page and in one captured digest. Snooze it, restart, simulate sleep/wake, and rerun review: the decision and project survive without a duplicate reminder.

**Status:** ready-for-agent

**Execution:** Complete — all acceptance criteria independently verified by the coordinator on 2026-10-21. Implementation: Luna `demo_luna`.

**Branch:** `jnr/01-codex-attention-demo` (base `7cf3d7c`).

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Establish the smallest runnable flow from fixture observations through persisted projects to the authenticated page and email sink | None | Luna `demo_luna` / coordinator verification | Verified | A repeatable application-level scenario displays the expected evidence and captures one digest |
| W2 | Add durable snooze, basic notification deduplication, and source freshness for the demo | W1 | Luna `demo_luna` / coordinator verification | Verified | Restart and simulated sleep/wake preserve project/snooze state and suppress unchanged delivery |
| W3 | Document and verify the stable external boundaries used by later slices | W1, W2 | Luna `demo_luna` / coordinator verification | Verified | Acceptance tests operate through intake and user actions; source/reviewer/clock/email substitutes work without internal-state assertions |

## Acceptance criteria

- [x] A documented command or short procedure runs the complete synthetic demo from a clean local environment without private data.
- [x] Multiple Codex-thread fixtures produce one expected project and an attention item with outcome, next action, and evidence locators.
- [x] The authenticated page and a fixed-recipient local sink show the same finding; an unauthenticated request cannot read project data.
- [x] A persisted snooze and unchanged-review deduplication survive application restart.
- [x] Simulated Mac sleep retains project visibility and marks observations stale; waking does not duplicate previously imported observations or notifications.
- [x] Opening or previewing an email link cannot mutate project state.
- [x] Fixtures substitute only external inputs; real intake, register, user-action enforcement, and notification logic are exercised.
- [x] The demo establishes observation, assessment-proposal, and delivery boundaries for later tickets; it does not claim live Codex compatibility or real reviewer accuracy.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)

## Comments

Coordinator verification: `python3 -m unittest discover -s tests -v` (2 HTTP acceptance tests pass), `python3 -m watchkeeper.demo --data-dir /tmp/watchkeeper-demo`, `python3 -m compileall -q watchkeeper`, and `git diff --check`. Additional HTTP checks verified batch rollback and role/recipient isolation. See [evidence](../../../docs/evidence-01.md) and [stable application contract](../../../docs/application-contract.md). No live reader or model claim is made.
