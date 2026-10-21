# Electricpeak deployment handoff

Status: Interface contract for a future deployment owner.

All implementation work covered by this plan stays in project-watchkeeper.
This repository supplies portable container artifacts, configurable Compose
examples, and operating documentation. It does not modify Electricpeak's
repository or running services. This boundary applies to the planned
implementation as well as this documentation PR.

## Deliverables owned by project-watchkeeper

- Versioned container artifacts and public-safe Compose examples with
  independently constrained components where privileges differ.
- Documented configuration, credential-file, and persistent-state interfaces.
- Configurable UID/GID, service ports, private base URL, trusted proxy/origin
  settings, authentication, and network dependencies.
- A private-ingress contract suitable for the existing Electricpeak Envoy
  pattern. Application authentication must remain effective if an upstream
  address can be reached directly.
- Health/readiness checks and documented startup, shutdown, retry, retention,
  deletion, backup, migration, and rollback behavior.
- A configurable email transport and fixed-recipient delivery interface.
- Synthetic fixtures, a local email sink, and tests of the application and
  container boundaries without production infrastructure.

Actual account identities, recipient addresses, credentials, host paths,
Envoy routes, and private network policy are deployment configuration, not
repository defaults.

## Electricpeak integration context

These are reference facts for the future deployment owner, not tasks assigned
to this repository's implementation agent:

| Existing host convention | Watchkeeper handoff requirement |
| --- | --- |
| Source Compose under `containers/source/<group>/docker-compose.yml`; generated Compose Nix imports in `configuration.nix` | Supply a portable Compose example the deployment owner can adapt |
| Home Manager file mirroring and `onChange` patterns | Document expected runtime paths and configuration reload/restart behavior |
| Runtime file secrets and consumer ordering/restarts | Support narrow credential-file mounts with documented permissions |
| Envoy as the existing service entry point | Document upstream port, private base URL, authentication, and proxy trust requirements |
| Production deployment from main after Nix checks | Supply versioned artifacts and rollback information; host validation stays with the deployment owner |

The Electricpeak owner separately chooses image versions, host storage,
secret provisioning, service ordering, Envoy routes, authentication, firewall
rules, and rollout. No Compose/Nix edits, host rebuilds, route changes, or
production deployment are part of the Watchkeeper implementation sequence.

## Facts still requiring deployment verification

The inspected public networking Compose mounts Envoy configuration and
certificates read-only, and runtime secret handling restarts its consumers.
Public documentation also describes a private Envoy configuration file.
The active private routes and synchronization between that file and the
runtime secret attachment were not available to verify.

No single-user authentication mechanism or outbound email transport was
verified in the public checkout. The deployment owner must select and
configure these interfaces. Do not assume Envoy routing authenticates users
or that an existing Gmail/mail-relay integration is available.

The existing shared Docker bridge does not by itself provide reviewer
isolation. The handoff must identify required component reachability and
denied paths so the deployment owner can enforce them.

## Operator acceptance checklist

For the separate deployment work, document how to verify:

- Actual Envoy/private-network reachability, authentication, and protection
  against direct-upstream bypass.
- Narrow secret/state mounts, permissions, image compatibility, and allowed
  network destinations.
- Account login and renewal, reader coverage, and delivery to the configured
  recipient.
- Restart recovery, retention/deletion, backups, and rollback compatibility.

Use generic examples and leave private values to the operator. These checks
describe readiness requirements; they do not authorize this repository's
agent to change Electricpeak or connect production accounts.
