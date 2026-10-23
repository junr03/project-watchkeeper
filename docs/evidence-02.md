# Ticket 02 verification

Coordinator independently ran `python3 -m unittest discover -s tests -v`: 15
tests passed on 2026-10-21. Real HTTP intake tests cover source/message
identity, revision replay, capture-time preservation, nested bounded cursors,
changed/deleted listing anchors, failures and later recovery, malformed and
unsupported items, and conservative coverage. A >2 MB source page is chunked
below 900 KB per HTTP request; losing the second ACK after durable intake and
replaying yields exactly 120 observations without refreshing prior captures.
The RPC deadline is total across notifications; copy/init failures clean up
disposable state.

The coordinator also ran the installed Codex 0.162.1 binary with an isolated
credential-free synthetic legacy rollout. `thread/list`, `thread/read`, and
`thread/turns/list` returned one thread with user and assistant text. The actual
collector sent both observations through HTTP intake, a controlled proposal
created a project, authenticated HTML showed the action and Codex locators,
and the configured file sink captured one digest. No inference, source
account, private transcript, or external email was used.

The coordinator additionally ran `tests/codex-modern-compat.py` against the
pinned binary. A localhost fake Responses server generated a modern Codex
session without accounts or inference. The validated standalone history
projection yielded one thread, one turn and two messages. An independent
collector-to-HTTP run then stored both observations with complete coverage,
created a controlled project and captured its digest.

The coordinator independently repeated the extended harness: two active
threads across two listing pages, one nonempty archived listing, two turns
across continuation, and two item pages all passed against the actual binary.

These synthetic binary checks do **not** establish macOS support or
compatibility with every real account history format.
Those live checks remain unverified; fixture results are listed separately in
`codex-reader.md`. Ticket 02 is not marked complete.

The coordinator independently passed all 22 tests after adding opt-in source
preparation, then repeated the actual-binary harness through the preparation
path with the same pagination/archive results. Synthetic WAL tests verify that
source database, WAL and SHM hashes remain unchanged. Preparation stages only
selected history, bounds file reads, rejects inconsistent snapshots, preserves
the intake cursor on failure, and removes the temporary mirror after the run.
