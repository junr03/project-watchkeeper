# 04: Make project decisions persist across reviews

**What to build:** Let the user control projects through the authenticated page: complete, abandon, reopen, snooze, archive/restore, exclude, and correct thread groupings. Persist those decisions and apply them to later reviews and notification eligibility, including when the reviewer proposes a conflicting change.

**Blocked by:** [01: Demonstrate a Codex project becoming an attention item](01-codex-attention-demo.md).

**Business decisions and activation gates:** None. Test through the existing synthetic application boundary; actual source and reviewer integrations are not prerequisites.

**Demo:** Change each project disposition through the page, restart, and replay conflicting review proposals. Complete and abandoned projects stay closed; snoozed projects return only after reassessment at expiry; archived projects disappear from the active view without losing their record and restore to their prior disposition. Corrected groupings and exclusions remain effective.

**Status:** ready-for-agent

**Execution:** Implementation checkpoint verified — Luna `decisions_luna`; coordinator independently ran eight application/migration tests and real Chromium controls. Archive/retention acceptance remains pending ticket 05.

**Branch:** `jnr/04-project-decisions`; isolated worktree; review stack will include ticket 02 for intake-side exclusion enforcement.

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Implement authenticated project actions and durable user-decision precedence | None | Luna `decisions_luna` / coordinator verification | Verified checkpoint | Explicit page actions persist through restart and defeat conflicting later proposals |
| W2 | Carry lifecycle, snooze, archive, grouping, and exclusion decisions into review and delivery behavior | W1 | Luna `decisions_luna` / coordinator verification | Verified checkpoint | The acceptance scenario shows correct active views, reassessment, and reminder suppression for each decision |
| W3 | Verify reversible controls and unauthorized/link-preview behavior at the application boundary | W1, W2 | Luna `decisions_luna` / coordinator verification | Verified checkpoint | Archive/restore preserves disposition; unauthenticated, forged, and preview requests cannot change state |

## Acceptance criteria

- [x] Complete and abandoned projects stay that way until explicitly reopened; old or fresh inferred proposals cannot undo the user decision.
- [x] Snooze persists until its selected expiry, then reassesses current evidence before allowing another reminder.
- [x] Archive hides the project from the active view and pauses attention reviews/reminders without deleting records or changing completion/abandonment disposition.
- [ ] Restore preserves the prior disposition and reassesses only when eligible; archive does not exempt transcripts from retention.
- [x] Known archived or excluded work is not rediscovered as a new candidate solely because it is hidden.
- [x] Grouping corrections and conversation/source exclusions persist and affect subsequent review results.
- [x] State-changing actions require authentication, explicit intent, and request-forgery protection; GET/link previews remain read-only.
- [x] The user-decision enforcement is exercised with controlled proposals and does not depend on the model obeying instructions.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)

## Verification

Coordinator: `python3 -m unittest discover -s tests -v` — 8 passed.
Pinned Chromium container: unauthenticated access denied; authenticated capture,
complete/reopen, archive/restore, snooze worked; zero JavaScript errors.
See `docs/evidence-04.md`. Ticket remains incomplete until ordinary transcript
expiry on archived records is verified with ticket 05.
