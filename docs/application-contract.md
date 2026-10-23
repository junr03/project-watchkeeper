# First-slice application contract

This contract is the stable local boundary for the synthetic Codex demo. It
uses only the Python standard library and SQLite. The service owns durable
observation, proposal validation, user decisions, freshness, and delivery
state. It does not connect to a source account or a model.

## Authentication roles

| Role | Credential | Allowed endpoints |
| --- | --- | --- |
| Collector | `Authorization: Bearer <intake token>` | `POST /api/observations` |
| Reviewer | `Authorization: Bearer <reviewer token>` | `GET /api/review-input`, `POST /api/reviews` |
| User | HTTP Basic over private TLS ingress, plus CSRF cookie/header and same-origin `Origin` on POST | `GET /`, `GET /api/state`, project snooze, `POST /api/deliver` |

The intake and reviewer tokens must be nonempty and different. The UI
credentials have no defaults. Configure all four secrets before starting the
service. Basic authentication is suitable only behind the later private TLS
ingress. Email's control-page link is informational; opening it performs a
read-only GET. Only an explicit authenticated POST changes state.

## Observation intake

`POST /api/observations` accepts one JSON object with exactly these fields:

```json
{
  "source_id": "codex-installation-1",
  "cursor": "opaque-source-checkpoint",
  "available": true,
  "observations": [
    {
      "thread_id": "thread-123",
      "message_id": "message-456",
      "revision": "3",
      "source_timestamp": "2026-10-20T17:00:00Z",
      "locator": "Codex thread thread-123, message message-456",
      "text": "Evidence captured from the source thread."
    }
  ]
}
```

The durable observation identity is the tuple `(source_id, thread_id,
message_id)`; the returned `observation_id` is its opaque stable digest.
`revision`, source time, capture time, locator, and text are retained as
provenance. Replaying the same revision is idempotent and preserves its
capture time. A new revision replaces the stored message and capture time.
Observations and a successful source cursor update commit in one transaction.
An unavailable source marks freshness stale immediately and preserves its
last successful cursor. Freshness is also time-based. Sleep therefore keeps
the project register visible while wake can safely replay the same source
messages.

## Reviewer boundary

`GET /api/review-input` returns up to 500 recent observations and a small
project-register snapshot, without credentials or runtime configuration.
`POST /api/reviews` accepts exactly:

```json
{
  "project_id": "garden-guide",
  "outcome": "Publish a tested garden guide",
  "next_action": "Ask a local nursery to confirm planting dates",
  "reasoning": "The draft exists, while the captured evidence says its plans are untested.",
  "evidence_refs": ["opaque-observation-id"]
}
```

The service rejects unknown fields (including recipient or execution
instructions), unrecognized evidence references, and malformed proposals.
The initial `watchkeeper.policy.validate_proposal(payload, stored_evidence,
*, now=None, sources=None)` function returns a canonical assessment with
`kind="attention"` and `eligible=true`. The clock and source snapshot are
explicit policy inputs so later threshold and freshness rules can extend this
boundary without changing the HTTP contract. A project identity is distinct
from every contributing thread ID; its current evidence snapshot is retained
with the project.

## User action and delivery

`POST /api/projects/{project_id}/snooze` accepts only
`{"until":"<timezone-aware ISO-8601 timestamp>"}`. It requires Basic auth,
the CSRF cookie issued by `GET /`, a matching `X-CSRF-Token`, and an `Origin`
matching the configured public base URL. Snoozing persists across restart and
cancels pending outbox entries for that project.

`POST /api/deliver` has the same user authentication and CSRF requirements.
It is the only operation that writes pending digests. It rechecks project
disposition and snooze state before each delivery. The recipient is fixed by
configuration; the reviewer cannot choose it. A persistent outbox deduplicates
unchanged project findings. The first sink writes a private `.eml` file, not
real mail. Durable transport ambiguity and scheduled delivery policy remain
later work.

## Running the slice

Use Python 3.12 or newer. The production service requires
`WATCHKEEPER_INTAKE_TOKEN`, `WATCHKEEPER_REVIEWER_TOKEN`,
`WATCHKEEPER_UI_USERNAME`, and `WATCHKEEPER_UI_PASSWORD`; configure state and
email paths and `WATCHKEEPER_PUBLIC_BASE_URL` for the deployment as well.
`python3 -m watchkeeper` starts the HTTP service. `python3 -m watchkeeper.demo`
runs the fully synthetic two-thread sequence against a real local HTTP
listener, reopens persistent SQLite state, marks a simulated sleeping source
stale, wakes it, and confirms that an unchanged review produces no second
email. Demo output is stored under `watchkeeper-demo-data/` by default.

The synthetic Codex locator is plain text; it does not claim a verified live
deep-link format or compatibility with a real Codex reader.
