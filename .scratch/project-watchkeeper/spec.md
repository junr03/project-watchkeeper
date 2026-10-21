# Project Watchkeeper

Document status: Draft; accepted decisions recorded, consolidated review pending.

A self-hosted monitor that tracks unfinished projects across AI
conversations and surfaces the next action that needs your attention.

## Accepted user-visible scope

- Discover candidate projects automatically across enabled sources.
- Identify unfinished outcomes that need the user to return, including when
  an agent ended its turn normally without asking an explicit question.
- Relate work across threads, providers, surfaces, and machines.
- Let the user exclude conversations and dismiss projects.
- Send a daily email digest only when there is new actionable information.
  Each attention item explains the unfinished outcome, proposes a next
  action, and links to supporting threads.
- Keep digest cadence and destination configurable.
- Preserve visibility of previously observed projects when a laptop sleeps;
  distinguish an unavailable source from evidence that a project has stalled.
- Support existing subscriptions/account login and MCP or local readers
  without requiring model API keys.
- Permit explicitly enabled unofficial consumer-account readers when a
  supported interface is unavailable. Isolate credentials and expose
  collection failures.

## First complete slice

Implement collectors for OpenCode on a server and Codex on a Mac, discover
unfinished work that needs the user, and produce a digest. Demonstrate the
flow using a synthetic project with source-shaped fixtures from both
surfaces, a simulated Mac sleep/wake cycle, and a local email sink. No
existing user project is assumed or required. Show retained visibility
during simulated sleep and silence on a repeat review with no new actionable
information.

Fixture-based checks do not establish real collector compatibility. Validate
the readers separately against pinned source versions and a controlled test
session before claiming live support. Connecting private accounts and
operating the service on Electricpeak are separate activation work.

ChatGPT web, Claude, and Meta Muse remain in the intended scope. Add their
collectors after this complete flow is demonstrated. Their inclusion is a
product requirement, not a claim that the available community readers are
already reliable or complete.

The slice includes collection, review, persistent project state, notification,
and a small authenticated control page linked from the digest. Separate
component assignments do not imply separate tickets or pull requests.

## History coverage and retention

Inspect all available history from enabled sources, starting with recently
updated threads and working backward within configurable limits. Report
actual coverage; source limits or missing history must not be presented as a
complete scan.

Captured transcripts expire after 30 days by default. Retain project
summaries and the specific evidence supporting unresolved attention items
until the user deletes them. Retention is configurable and deletion must be
explicitly available. This is a private runtime store, separate from the
public repository's local Markdown implementation tracker.

## User controls

Provide a small authenticated web page linked from the digest. It supports
snoozing, marking complete, abandoning, excluding, and correcting project
thread groupings. User decisions persist across reviews.

The page is single-user and accessible only through the deployment's private
network. On Electricpeak, route it through Envoy using the existing service
exposure pattern. Authentication remains required; private reachability and
reverse proxy routing alone do not establish the user's identity.

Opening an email link only displays the page. State changes require an
explicit authenticated action; link previews and email scanners must not
change project state.

Explicit user decisions take precedence over inferred state. Projects marked
complete or abandoned stay that way until the user reopens them. Reassess
snoozed projects when the snooze expires; a still-actionable result may then
be notified again.

Deleting a project removes its stored summaries and evidence and excludes
its known threads from future imports until explicitly re-enabled. Retain
only the minimal exclusion identifiers needed to prevent re-import. Source
conversations remain untouched.

### Proposed: reversible archive

Archive hides a project from the active view and pauses its attention reviews
and reminders without deleting its stored records or changing whether it is
complete or abandoned. Restore returns it to its prior disposition and
reassesses it when eligible. Ordinary transcript retention still applies;
archive is not a permanent transcript backup. Known archived projects must
not be rediscovered as new candidates merely because they are hidden.

This archive behavior awaits the final shared-understanding confirmation.

## Attention rules and collection health

An explicit unanswered request for user input is eligible for the next
digest. Inferred unfinished work requires three days without observed
progress and evidence of a concrete action the user could take. Thresholds
are configurable. An old timestamp alone is insufficient.

An actively working agent or a known external dependency must not be
presented as waiting on the user. If visibility is insufficient to establish
the current situation, represent that uncertainty rather than infer that the
user is holding up the project.

Automatically group threads only with strong supporting evidence. Leave
uncertain matches and completion judgments as proposals on the control page;
do not email them as established facts. An agent saying "done" does not prove
the intended outcome is complete.

Expired logins and persistent collection failures appear as separate,
deduplicated collection notices in the daily digest. Ordinary laptop sleep
marks observations as stale; it is not itself a collection failure or
evidence that a project has stalled.

## Review limits and recovery

Run reviews daily by default. Process history incrementally within
configurable work limits. Preserve checkpoints when subscription limits or
login failures interrupt progress, and resume after capacity or access is
restored. Persistent failures become collection notices. Never silently
switch to paid model API access.

## Operating boundary

Use a dedicated, restricted OpenClaw reviewer. Account collection is a
separately permissioned responsibility. The reviewer may read observations,
maintain its project register, and produce notifications to the configured
recipient. Source-thread replies, project execution, broad shell access,
delegation, and self-reconfiguration are outside its job.

Treat observed conversations as evidence, not authority to change permissions.
A full application boundary is required; a tool sandbox alone does not
contain Gateway-side plugins or MCP calls.

## Packaging and deployment

This public repository owns portable container packaging, configuration
interfaces, safe examples, and implementation documentation. Private account
details, credentials, transcripts, and host configuration stay outside Git.

Electricpeak's existing Compose-to-Nix container framework is the intended
consumer of this repository's deployment artifacts. All implementation work
in this plan stays in project-watchkeeper. Its deliverables are portable
containers, configurable examples, and a deployment handoff contract.

Changes to Electricpeak's repository, Envoy routes, secrets, firewall, Nix
imports, and running services are separate deployment work. Electricpeak
owns image selection, storage, secret provisioning, networking, and rollout.
This documentation PR does not deploy a service or authorize account
connections; future Watchkeeper implementation does not include those
Electricpeak changes either.

## Implementation guidance and final review

See [the implementation guide](../../docs/implementation.md) for component
boundaries, acceptance scenarios, delivery sequence, and activation gates.

The remaining product decision is the proposed archive behavior above. The
consolidated scope also awaits the user's final shared-understanding check.
This draft records design decisions; it does not authorize implementation,
merging, connecting accounts, or deploying services.
