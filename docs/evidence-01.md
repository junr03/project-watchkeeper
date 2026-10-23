# Ticket 01 verification

Verified by the coordinator on 2026-10-21 against the running application.
All source content and credentials used here are fabricated.

```
python3 -m unittest discover -s tests -v
Ran 2 tests — OK

python3 -m watchkeeper.demo --data-dir /tmp/watchkeeper-demo
```

The independent demo run captured three observations from two Codex-thread
fixtures, one project with supporting evidence, and one fixed-recipient email.
An authenticated snooze survived application restart. Simulated sleep marked
the retained source stale; wake imported zero repeated revisions and delivered
zero additional emails. The captured email links to a read-only page.

The HTTP acceptance scenarios verify authentication, separate intake/reviewer
credentials, unchanged-review deduplication, persisted snooze, sleep/wake,
read-only previews, and rejection of a forged action origin. A second scenario
snoozes an already queued finding and verifies it is not delivered.

Additional independent HTTP checks submitted a batch with one invalid timestamp
and verified that neither its observations nor its cursor persisted. Adding a
recipient to a reviewer proposal was rejected without creating a project.
Intake credentials could not read reviewer input or submit review proposals.

The coordinator inspected the HTTP handlers, SQLite transactions, proposal
validation, user-action enforcement, and file email sink. Test substitutes are
restricted to observations, reviewer proposals, time/availability, and captured
email. SQLite state and application logic are real.

This evidence does **not** establish live Codex reader compatibility, actual
OpenClaw model judgment, SMTP acceptance, container isolation, or production
authentication/ingress. Those have their own tickets and activation gates.
