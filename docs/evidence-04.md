# Ticket 04 verification

Coordinator independently ran `python3 -m unittest discover -s tests -v`
on 2026-10-21: eight tests passed. The suite covers authenticated user actions,
CSRF/origin and read-only GET boundaries, restart persistence, conflicting
fresh model proposals after closure, archived aliases, abandoned disposition
through archive/restore, snooze reassessment, grouping locks and exclusions,
and transactional schema upgrades/future-version rejection.

A separate real Chromium run used Playwright 1.64.1 and image
`mcr.microsoft.com/playwright@sha256:7852dc5a7d72f5fe458a597d690f6c42fbc8e9e26ddfa95f8e6ef552558d9bbf`.
With network disabled outside the container, read-only source/root, no
capabilities and uid 1000, it launched a synthetic local service and verified
401 without authentication, one captured digest from the actual button,
complete/reopen, archive/restore and snooze, with no JavaScript errors.
The reusable combined browser harness is delivered with ticket 07.

Intake exclusion enforcement is supplied by ticket 02 in this review stack.
Archive does not alter retention settings; actual expiry on archived records
remains unverified until ticket 05. No private sources or accounts were used.

After integrating ticket 02, the coordinator independently passed all 18
combined tests. A direct post-scenario storage check additionally confirmed
new source-excluded and thread-excluded message IDs were never persisted.
