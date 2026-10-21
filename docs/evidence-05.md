# Ticket 05 verification

## Combined monitoring checkpoint

After integrating ticket 06, the coordinator independently passed all 78
existing checks, then 22 privacy/lifecycle checks with ResourceWarnings treated
as errors. Six new combined scenarios verify leased retained evidence after raw
expiry, persistent snooze, old citations with current versus expired newer
context, aggregate capture deletion, scheduler cleanup during restore replay,
scheduled retention maintenance, and existing shared projects alongside archive
alias protection. Deletion previews count actual app-controlled aggregate
captures once; external accepted mail cannot be recalled.

The coordinator independently ran `python3 -m unittest discover -s tests -v`
on 2026-10-21: all 30 tests passed against the integrated collection, decisions
and privacy code. The privacy cases exercise real SQLite state and application
logic with simulated time; HTTP cases use the authenticated application.

Verified behavior includes validated default 30-day retention, expiry without
capture-age renewal, atomic intake suppression of expired revisions with cursor
acknowledgment, newer revision capture, unresolved evidence preservation,
ordinary raw expiry for archived projects, and later cleanup of closed evidence.

Deletion previews report directly shared conversations and reject stale impact
tokens. Authenticated confirmation removes the selected project, scrubs affected
shared findings without cascading through unrelated threads, cancels delivery,
removes captures and prevents re-import. Explicit re-enable permits collection
again. Only content-free exclusion/replay identifiers remain for deleted work.

Controlled older-backup restores replay the current privacy journal. Tests
cover a missing journal, restore-only delivery IDs, hook-returned aggregate
capture IDs, a crash after the database commit before file cleanup, and
concurrent exclusion/replay. Cleanup identifiers are journaled before commit
so a later startup retries file removal. Source transcripts stay untouched.

The coordinator also used actual Chromium in a pinned, read-only, network-none
container: unauthenticated access returned 401; capture, complete/reopen,
archive/restore and snooze worked; cancelling deletion preserved the project;
confirming deletion removed it and excluded its thread. No browser errors.

This establishes the privacy slice and completes ticket 04's archive/retention
criterion. Combined scheduler cache, aggregate mail and reviewer lease deletion
checks follow in ticket 07. Backup-media expiry and external mail recall remain
operator limitations, documented in `privacy.md`; no production changes occurred.

Integration review added a retained-revision regression: an old project
snapshot cannot override a current raw row outside the input window or become
current evidence after a newer raw revision expires. The shared selector checks
the latest expiry metadata while preserving the original project provenance.
The coordinator independently reran all nine privacy service tests, passing
with ResourceWarnings treated as errors.
