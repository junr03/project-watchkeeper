# Durable application service with external adapters

Use Python 3.12 or newer and SQLite for the single-user application. Keep
runtime dependencies in the standard library. Observations and structured
assessment proposals enter authenticated HTTP boundaries; explicit user
actions govern the durable project register. The page and captured email
are the primary acceptance outputs.

Collectors and the OpenClaw reviewer use separate credentials and deployment
permissions. Neither can invoke user actions or choose email recipients.
The service owns deterministic eligibility, user-decision precedence, and
delivery records. Account/subscription login belongs to the reviewer runtime;
the service never requires a model API key.

SQLite transactions couple observation persistence and source checkpoints.
Use a persistent outbox for delivery, distinguishing successful delivery
from ambiguous transport acceptance. A configured file sink provides the
first synthetic email boundary; production transport remains explicit.

Tests may replace source transport, reviewer responses, time/availability,
and email transport. They exercise the running HTTP application and durable
state through its public interfaces. Test clock controls are constructor
dependencies, never unauthenticated production endpoints.

This choice keeps the initial deployment small and portable. It trades
horizontal scaling for a single durable service with straightforward local
recovery, appropriate to the approved single-user milestone. Database schema
changes must have explicit version and rollback behavior before packaging.
