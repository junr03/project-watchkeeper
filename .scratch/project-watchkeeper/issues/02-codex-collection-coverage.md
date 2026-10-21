# 02: Collect actual Codex threads with visible coverage

**What to build:** Feed actual Codex-thread observations from a Mac collector into the existing project/page/digest flow. Show what history was observed, where it came from, and what could not be read. Keep controlled reviewer responses available so this slice does not depend on the real reviewer.

**Blocked by:** [01: Demonstrate a Codex project becoming an attention item](01-codex-attention-demo.md).

**Business decisions and activation gates:** No unresolved business decisions. Availability of a supported Codex version and controlled source sessions gates live compatibility evidence. Private owner accounts are a later opt-in activation step, not a prerequisite to implement the collector. Missing live checks must be reported as untested.

**Demo:** Create controlled Codex sessions, collect them into the application, and view their evidence and source locators in a project and captured digest. Edit or extend a session, revisit it, and replay a page; show updated evidence without duplicate observations. Interrupt a paginated scan and show honest partial coverage.

**Status:** ready-for-agent

**Execution:** Implemented; synthetic verification passed independently. Linux legacy and modern binary flows, synthetic archive and pagination verified; macOS compatibility remains unverified.

**Branch:** `jnr/02-codex-collection`; isolated worktree, base ticket 01 (`e9edc33`, PR #2).

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Implement the read-only Codex collector against a pinned, inspected thread interface | None | Luna `collection_luna` / coordinator verification | Implemented / synthetic verified | Controlled sessions produce correctly identified observations with provenance, source times, capture times, and locators |
| W2 | Carry revisions, pagination, and durable cursors through intake to visible coverage | W1 | Luna `collection_luna` / coordinator verification | Implemented / synthetic verified | Interrupted and repeated pages display accurate coverage and converge without lost or duplicated observations |
| W3 | Verify source compatibility and narrow source permissions through the full flow | W1, W2 | Luna `collection_luna` / coordinator verification | Partially verified; live gates remain | A recorded pinned-version compatibility run reaches page and email sink; forbidden source writes are unavailable |

## Acceptance criteria

- [ ] The collector reads Codex threads on the Mac only; no OpenCode or other-provider implementation is introduced.
- [x] Source installation/account, thread, and message identity prevent collisions; provenance and usable locators reach the control page.
- [x] Edited threads and newly available messages are revisited; overlapping pages and retries are idempotent.
- [x] A cursor advances only after associated observations are durable.
- [x] Listing, archived-history access, pagination, message completeness, and current activity capabilities are documented as supported, unsupported, or untested.
- [x] Partial history and absent current-activity information remain explicit; lack of a process signal is not reported as idle.
- [x] The collector has only the needed read access, with source credentials isolated from the reviewer and logs.
- [ ] Controlled live-source compatibility evidence is checked before claiming live support; fixture-only results cannot satisfy that claim.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)

## Verification

See `docs/evidence-02.md`: coordinator independently passed 15 tests and actual Codex 0.162.1 Linux
legacy/modern transcript-to-page-to-digest flows. The extended actual-binary
harness also passed active listing, nonempty archived history, turn and item
pagination. Mac and owner history compatibility remain unverified. No private account activation was attempted.
