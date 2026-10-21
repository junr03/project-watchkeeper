# Ticket 02 verification

Coordinator independently ran `python3 -m unittest discover -s tests -v`: 12
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

This narrow binary check does **not** establish macOS support, modern rollout
compatibility, nonempty archived history, or actual multi-page binary cursors.
Those live checks remain unverified; fixture results are listed separately in
`codex-reader.md`. Ticket 02 is not marked complete.
