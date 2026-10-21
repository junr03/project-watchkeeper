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

The slice includes collection, review, persistent project state, and
notification. Separate component assignments do not imply separate tickets
or pull requests.

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

- Initial history coverage and ongoing retention.
- The user's control surface for corrections, snoozing, and dismissals.
- Evidence and elapsed-time thresholds for an attention item.
- Ambiguous cross-thread project matches and completion judgments.
- Collection-health reporting, retry behavior, and notification delivery.
- Review limits, subscription exhaustion, and reconnection behavior.
- Detailed acceptance scenarios and implementation sequence.

Resolve these before treating this spec as ready for implementation.
