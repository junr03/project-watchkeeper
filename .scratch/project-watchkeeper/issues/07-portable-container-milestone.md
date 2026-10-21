# 07: Run the complete milestone from portable containers

**What to build:** Provide a configurable portable container application that demonstrates the complete Codex-only milestone and supplies an actionable deployment handoff. Exercise the user journey, authentication, isolated review, lifecycle/privacy controls, recovery, and email delivery in a local test environment.

**Blocked by:** [05: Expire and delete stored project information](05-retention-and-deletion.md) and [06: Keep monitoring reliable through backlog and interruptions](06-monitoring-recovery.md). These transitively include tickets 01–04.

**Business decisions and activation gates:** No unresolved business decisions. Private Envoy routes, host secret provisioning, recipient/mail transport, production account activation, and Electricpeak rollout belong to a separate deployment owner. They do not block building and testing portable artifacts here.

**Demo:** From a clean environment, start the versioned container application with synthetic Codex fixtures, private-ingress test configuration, test authentication, and a local mail sink. Complete the project-to-digest-to-user-decision journey, exercise deletion and restart recovery, and demonstrate denied direct/unauthenticated access and restricted reviewer capabilities.

**Status:** ready-for-agent

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Package the complete service with validated public-safe configuration and persistent-state/credential interfaces | None | Unassigned | Planned | A clean local startup runs the full Codex scenario using only documented portable artifacts and test credentials |
| W2 | Verify the combined acceptance journey and container/private-ingress boundaries | W1 | Unassigned | Planned | Recorded application-level checks cover lifecycle, privacy, recovery, real-tool restrictions, and bypass attempts |
| W3 | Provide versioned artifact and operator handoff documentation with verified lifecycle procedures | W1, W2 | Unassigned | Planned | Startup, health/readiness, shutdown, state compatibility, and rollback procedures work locally; deployment-only values are identified |

## Acceptance criteria

- [ ] Portable versioned container artifacts and Compose examples run without editing Electricpeak or requiring real private accounts for the synthetic demo.
- [ ] Configuration validates source settings, work limits, schedules/time zones, retention, private base URL, proxy trust/origins, authentication, mail transport/recipient, state paths, and credential-file references.
- [ ] The configured outbound transport works against a local email sink and cannot be redirected by reviewer output.
- [ ] Authenticated private-ingress tests and direct-upstream tests demonstrate that routing cannot bypass application authentication.
- [ ] The reviewer has narrow mounts, protected configuration/state boundaries, no privileged runtime or container-runtime socket, and enforced allowed network/tool access.
- [ ] The complete Codex-only acceptance suite passes, including combined user decisions, archive/restore, retention/deletion, interruption recovery, and deduplicated delivery.
- [ ] Readiness/health checks and documented shutdown/restart procedures preserve project state and user decisions.
- [ ] Publish operating and rollback instructions with explicit state-migration compatibility and backup/deletion limits.
- [ ] Private values stay outside tracked examples; no source-account credentials or transcripts enter public fixtures/logs.
- [ ] The handoff describes the interfaces the Electricpeak owner must supply; this ticket performs no Electricpeak repository changes, host rebuilds, route changes, or production activation.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)
