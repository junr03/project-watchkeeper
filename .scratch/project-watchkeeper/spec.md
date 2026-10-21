# Project Watchkeeper

Status: ready-for-agent

## Delivery Contract

- **Accepted scope:** A self-hosted monitor discovers unfinished projects across
  enabled AI sources, identifies evidence-supported next actions for the user,
  and delivers a daily email digest only when there is new actionable
  information. It provides persistent user controls, source coverage and
  collection notices, configurable retention, and account/subscription access
  without requiring model API keys. Start with Codex threads on the Mac only;
  add OpenCode, ChatGPT web, Claude, and Meta Muse after the first complete
  demonstration.
  All implementation stays in project-watchkeeper.
- **First demo:** Create a fabricated unfinished project represented by
  multiple Codex-thread fixtures. Import observations, identify the
  outcome and next action, capture a digest in a local email sink, and persist
  a user decision through the authenticated control page. Simulate Mac
  sleep/wake and repeat review to demonstrate retained visibility and silence
  when nothing actionable changes. No existing user project is required.
  The production Codex collector is part of the slice, but fixture results
  do not prove its compatibility with live Codex threads. Cross-provider and
  remote-server collection are later expansion, not first-slice requirements.
- **Seam reuse:** The repository currently contains documentation, with no
  executable code or existing test harness. Introduce one primary application
  acceptance boundary: observations and explicit user actions enter the
  running service; projects, control-page responses, and captured digests are
  observed externally. Use the agreed collection/intake interfaces and
  substitute source transport, clock/availability, reviewer responses, and
  outbound email at external boundaries. Separately verify actual reader
  compatibility and model judgment; do not build a test seam for every module.
- **Expected pull-request range:** No numeric range was agreed. Proposal for
  ticket breakdown: organize small reviewable PRs around one complete first
  slice, estimate the range there, and review later source expansion
  separately. The range is a review signal, not a hard cap.
- **Replan trigger:** Revisit delivery shape before materially expanding the
  accepted behavior or agreed range, replacing the reviewer architecture, or
  requiring credentials/infrastructure outside the accepted boundaries.
  Agent assignments do not automatically become tickets or PRs.
- **Dependencies — code prerequisites:** No pre-existing application code must
  land before implementation can start. Establish the application boundary,
  durable state, and constrained reviewer/reader interfaces within the first
  slice. Verify pinned reader and subscription-reviewer compatibility before
  claiming live support. A later adapter's feasibility blocks that adapter,
  not the synthetic first demonstration.
- **Dependencies — business decisions:** No unresolved product decision blocks
  the specified first slice. Choose routine technical details during
  implementation within these constraints. Propose the PR range during
  ticket breakdown; do not invent further user-visible requirements.
- **Dependencies — activation and rollout gates:** Actual account logins,
  private recipient and mail transport, production authentication, Envoy
  routing, host storage/secrets/network policy, and deployment belong to
  separate operator work. They are not blockers for the synthetic demo.
  Watchkeeper supplies portable artifacts and a handoff contract; changes to
  Electricpeak and its running services are excluded.

## Problem Statement

Projects stall because the user forgets to return to conversations scattered
across providers, applications, and machines. An agent may have finished its
turn normally while the intended project outcome remains unfinished. Neither
an idle-process dashboard nor a list of unanswered questions reliably reveals
that situation. Sleeping laptops and incomplete history access further obscure
what is happening.

The user needs visibility into unfinished outcomes that need their attention,
without registering every project manually or granting a monitoring agent
authority to operate their projects.

## Solution

Maintain a private project register inferred from enabled sources. Associate
threads only when evidence supports the relationship, preserve the user's
decisions, and surface a concrete next action with links to supporting threads.
Keep uncertain findings visible on the control page instead of emailing them
as established facts.

A dedicated restricted OpenClaw reviewer assesses observations supplied by
separately permissioned collectors. Deterministic services enforce user
decisions and notification rules. The service remains useful when a source
device sleeps and distinguishes old observations from failed collection.
Public-safe container packaging and configuration interfaces allow a separate
operator to deploy it behind Electricpeak's existing Envoy pattern.

## User Stories

1. As the user, I want projects discovered automatically across enabled sources,
   so that forgotten work is found without manual registration.
2. As the user, I want related threads grouped by intended outcome, so that
   work spanning providers and machines is understandable as a project.
3. As the user, I want an evidence-supported next action even when an agent
   ended normally, so that unfinished work becomes actionable.
4. As the user, I want a configurable daily digest that stays quiet without
   new actionable information, so that reminders remain useful.
5. As the user, I want to inspect evidence, coverage, freshness, and uncertain
   proposals, so that I can judge the findings and correct thread groupings.
6. As the user, I want to snooze, complete, abandon, exclude, and reopen
   projects or conversations as appropriate, so that reviews respect my intent.
7. As the user, I want to archive and restore projects without deleting them,
   so that I can clear the active view without losing the project record.
8. As the user, I want configurable retention and explicit deletion that
   prevents re-import, so that I control the stored conversation material.
9. As the user, I want projects to remain visible while my laptop sleeps and
   separate notices for persistent collection failures, so that missing
   observations are not mistaken for work waiting on me.
10. As the user, I want existing account/subscription access and explicit
    opt-in to unofficial readers, so that model API keys are not required.
11. As the user, I want a private authenticated control page and a reviewer
    limited to monitoring, so that project visibility does not grant source
    account or project execution authority.
12. As the deployment owner, I want portable containers and configurable
    private values, so that deployment can follow the host's conventions
    without publishing credentials or modifying host configuration here.

## Implementation Decisions

- Use a dedicated restricted OpenClaw reviewer. Reuse suitable existing
  readers; explicitly validate and expose their capabilities and limitations.
  Separate collectors, validated intake, private storage/register, review
  proposals, control actions, and deterministic digest delivery by responsibility
  and permission. These boundaries do not mandate a separate process for each.
- Collect near each source with narrow access. Source-account credentials stay
  with collectors; reviewer subscription login is separate. Expose read-only
  source operations. Explicitly enable unofficial consumer readers and show
  expired sessions, failed retrieval, and incomplete coverage.
- Identify observations by source installation/account and thread/message
  identity. Record provenance, timestamps, revisions, retrieval limits, and
  usable evidence locations. Ingestion is idempotent, revisits changed
  threads, and advances durable checkpoints only after successful persistence.
- Scan all available history incrementally, most recently updated first,
  within configurable work limits. Report actual coverage and gaps. Preserve
  prior observations while devices are unavailable. Unknown current activity
  is not idle, and missing observations do not prove a lack of progress.
- Review daily by default. An explicit unanswered request for user input is
  eligible for the next digest. Inferred unfinished work requires three days
  without observed progress plus evidence of a concrete user action;
  thresholds are configurable. Active agent work and known external
  dependencies are not presented as waiting on the user.
- Group threads automatically only with strong evidence. Keep uncertain
  matches and completion judgments as control-page proposals. A normal end
  of turn or an agent's claim of completion does not establish the project's
  intended outcome.
- Make user decisions authoritative. Complete and abandoned projects remain
  so until explicitly reopened. A snooze suppresses reminders until expiry,
  then triggers reassessment. Corrected groupings and exclusions persist.
- Archive is reversible: hide the project from the active view and pause its
  attention reviews/reminders without deleting its record or changing its
  completion/abandonment disposition. Restore that disposition and reassess
  when eligible. Archive does not override retention or permit rediscovery
  of the same hidden project as a new candidate.
- Expire captured transcripts after 30 days by default, with configurable
  retention. Keep project summaries and specific evidence supporting
  unresolved attention items until user deletion. The runtime register is
  private and separate from the repository's implementation issue tracker.
- Deletion removes stored project summaries/evidence and affected derived
  material and pending notifications, while retaining only minimal exclusion
  identifiers for known threads. Re-import requires explicit re-enablement.
  Source conversations stay untouched. Explain shared-thread consequences
  before deletion and account for backup expiry and deletion replay on restore.
- Provide a single-user authenticated page suitable for private ingress
  through Envoy. Configure proxy trust and origins. Email links display
  information only; explicit authenticated actions change state. Neither a
  link preview nor a direct upstream request may bypass the intended boundary.
- Send digest content through a fixed configured recipient and transport.
  Each attention item identifies the outcome, next action, and evidence
  links or honest source/thread locators. Deduplicate meaningful findings
  against successful delivery; renewed eligibility after snooze is allowed.
  Persist an outbox and handle ambiguous transport acceptance without
  promising exactly-once email delivery.
- Put persistent collection or login failures in separate deduplicated
  collection notices. Ordinary device sleep marks observations as stale.
  Preserve checkpoints on quota exhaustion or access loss and resume when
  restored. A failed review is not a successful empty review. Never silently
  fall back to paid model API access.
- Enforce restrictions around the whole reviewer application, including
  Gateway-side plugins/MCP. Use narrow mounts, controlled network reachability,
  protected configuration, and explicit tool permissions. Source replies,
  project execution, broad shell/browser access, delegation, recipient
  selection, and self-reconfiguration are unavailable to the reviewer.
  Conversation content is evidence, not operational authority.
- Supply versioned container artifacts, configurable Compose examples,
  validated configuration, credential-file interfaces, persistent-state
  requirements, and operating/rollback documentation. Keep private values
  outside the public repository. Host configuration changes and activation
  remain separate deployment work.

## Testing Decisions

- **Primary seam:** Exercise externally observable behavior through the
  running application's intake and authenticated user interface, observing
  project state and captured email. Test outcomes and permission boundaries,
  not internal function calls, table layouts, or component wiring.
- **Prior art:** There are no executable tests in this documentation-only
  repository. The already-reviewed synthetic demo and acceptance scenarios
  provide the behavioral starting point; the harness must be built.
- **Controlled external inputs:** Use source-shaped Codex-thread fixtures,
  simulated time and availability, controlled reviewer responses, and a local
  email sink. Exercise real intake, register, user-decision enforcement,
  retention, and notification logic. Use synthetic material in public fixtures.
- **Outcome checks:** Cover unfinished work after a normal agent response,
  grouping across Codex threads, uncertain evidence, explicit versus inferred
  attention thresholds, active work, and external dependencies. Verify no
  duplicate digest after repeated unchanged reviews.
- **User controls:** Verify persistence and precedence across subsequent
  reviews and restarts; snooze expiry; archive/restore without data loss
  beyond ordinary retention; deletion/exclusion preventing re-import;
  grouping corrections; and email link previews causing no mutation.
- **Recovery:** Simulate sleep/wake, partial pagination, revisions, interrupted
  ingestion, quota exhaustion, expired login, and restart during delivery.
  Check coverage, preserved checkpoints, durable user decisions, and distinct
  collection notices. Verify raw retention and deletion of derived material.
- **Containment:** Check unauthenticated and direct-upstream access, hostile
  transcript instructions, forbidden source writes/exec, fixed-recipient
  delivery, and protected credentials/configuration. Test application and
  container boundaries locally; production Envoy verification is a later
  operator gate.
- **Compatibility and judgment:** Separately test actual pinned readers with
  controlled source sessions and evaluate the real reviewer on representative
  synthetic cases, including false positives. A stubbed source or reviewer
  cannot prove live compatibility or semantic quality. Mark unavailable live
  checks as untested, not passing.
- Keep meaningful tests at coherent slice boundaries. Demonstrate the first
  complete flow before expanding to additional sources.

## Out of Scope

- Modifying Electricpeak's repository, Compose/Nix modules, Envoy routes,
  secrets, firewall, or running services; building/rebuilding or deploying
  the host as part of Watchkeeper implementation.
- Automatically continuing projects, replying in source threads, executing
  work, or granting the reviewer general-purpose account access.
- Connecting real private accounts, sending real notifications, or assuming
  an existing user project is available for this specification task.
- Public internet exposure, multiple-user collaboration, or additional
  notification channels.
- Requiring model API keys or silently switching billing/authentication modes.
- Treating timestamps, source outages, agent turn completion, or speculative
  findings as proof that a project is waiting on the user.
- OpenCode, other providers, and remote-server collection in the first slice.
  These remain later expansion after the Codex-only demonstration.
- Automatically creating implementation tickets or assigning a fixed PR count
  as part of this spec publication.

## Further Notes

The spec is published in the repository's local Markdown tracker with
`ready-for-agent` triage status. That status describes specification readiness,
not implementation, account-connection, deployment, or merge authorization.

The implementation guide, integration evidence, and deployment handoff
provide supporting detail. Provider interfaces and community readers remain
compatibility risks requiring verification against pinned versions.
This synthesis does not claim a running service or tested account integration.

- [Implementation guide](../../docs/implementation.md)
- [Deployment handoff](../../docs/deployment-electricpeak.md)
- [Integration evidence and limitations](../../docs/integration-evidence.md)
- [Domain glossary](../../GLOSSARY.md)
- [Observation boundary decision](../../docs/adr/0001-separate-observation-from-project-execution.md)
