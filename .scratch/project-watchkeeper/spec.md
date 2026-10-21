# Project Watchkeeper

Document status: Draft; design interview in progress.

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

Observe OpenCode on a server and Codex on a Mac, discover unfinished work
that needs the user, and deliver a digest. Demonstrate collection from both
sources, retained visibility while the Mac sleeps, and silence on a repeat
review with no new actionable information.

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

Opening an email link only displays the page. State changes require an
explicit authenticated action; link previews and email scanners must not
change project state.

## Attention rules and collection health

An explicit unanswered request for user input is eligible for the next
digest. Inferred unfinished work requires three days without observed
progress and evidence of a concrete action the user could take. Thresholds
are configurable. An old timestamp alone is insufficient.

An actively working agent or a known external dependency must not be
presented as waiting on the user. If visibility is insufficient to establish
the current situation, represent that uncertainty rather than infer that the
user is holding up the project.

Expired logins and persistent collection failures appear as separate,
deduplicated collection notices in the daily digest. Ordinary laptop sleep
marks observations as stale; it is not itself a collection failure or
evidence that a project has stalled.

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

The deployment target is Electricpeak's existing Compose-to-Nix container
framework. Electricpeak owns its deployment-specific image selection,
storage, secret provisioning, networking, and rollout. This documentation PR
does not deploy a service or authorize account connections.

## Decisions still to resolve

- Control-page access boundary.
- Ambiguous cross-thread project matches and completion judgments.
- Deletion and future re-import behavior.
- Review limits, subscription exhaustion, and reconnection behavior.
- Detailed acceptance scenarios and implementation sequence.

Resolve these before treating this spec as ready for implementation.
