# Electricpeak deployment handoff

Status: Interface contract for a future deployment owner.

All implementation work covered by this plan stays in project-watchkeeper.
This repository supplies portable container artifacts, configurable Compose
examples, and operating documentation. It does not modify Electricpeak's
repository or running services. This boundary applies to the planned
implementation as well as this documentation PR.

## Deliverables owned by project-watchkeeper

The current portable artifacts are [`Dockerfile`](../Dockerfile),
[`Dockerfile.reviewer`](../Dockerfile.reviewer), [`compose.yaml`](../compose.yaml),
[`compose.production.yaml`](../compose.production.yaml), and the optional
[SMTP-auth overlay](../compose.production-smtp-auth.yaml). See the
[container runtime guide](container-runtime.md) for startup, configuration,
network, secret, backup, shutdown, and rollback procedures.

The deployment owner retrieves both Compose files from the Watchkeeper
revision being deployed, then supplies matching published app and reviewer
references. This uses the image-only platform surface without cloning the
Watchkeeper source repository. For example:

```sh
WATCHKEEPER_REVISION=REPLACE_WITH_PUBLISHED_COMMIT_SHA
curl -fsS "https://raw.githubusercontent.com/junr03/project-watchkeeper/${WATCHKEEPER_REVISION}/compose.yaml" -o compose.yaml
curl -fsS "https://raw.githubusercontent.com/junr03/project-watchkeeper/${WATCHKEEPER_REVISION}/compose.production.yaml" -o compose.production.yaml
export WATCHKEEPER_IMAGE="ghcr.io/junr03/project-watchkeeper:sha-${WATCHKEEPER_REVISION}"
export WATCHKEEPER_REVIEWER_IMAGE="ghcr.io/junr03/project-watchkeeper-reviewer:sha-${WATCHKEEPER_REVISION}"
docker compose -f compose.yaml -f compose.production.yaml pull
docker compose -f compose.yaml -f compose.production.yaml up --no-build -d
```

The required secret files and external networks are operator-created as
described in the runtime guide. This flow is available after the publication
change has merged to `main` and its first publication workflow succeeds. GHCR
packages may initially be private; the owner must set the intended package
visibility and verify anonymous pulling before relying on unauthenticated
host pulls. See [GitHub's container registry guidance](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

- Versioned container artifacts and public-safe Compose examples with
  independently constrained components where privileges differ.
- GHCR app and reviewer images published by Watchkeeper CI after all checks
  pass on `main`; deployment consumes published images and Compose and does
  not build Watchkeeper from source. Pin both images to the same commit or to
  the digest references reported by CI. The `main` tags move and update
  sequentially.
- Documented configuration, credential-file, and persistent-state interfaces.
- Configurable internal service port, private base URL and CSRF origin allowlist,
  mandatory application authentication, and network dependencies.
- A private-ingress contract suitable for the existing Electricpeak Envoy
  pattern. Application authentication remains effective if an upstream address
  is reached directly; forwarded identity headers are ignored.
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
| Envoy as the existing service entry point | Document upstream port, private base URL, authentication, and ignored forwarded-identity headers |
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

- Actual Envoy/private-network reachability, mandatory app authentication, and
  protection against direct-upstream bypass; forwarded identity headers do not
  authenticate users.
- Narrow secret/state mounts, permissions, image compatibility, and allowed
  network destinations.
- Account login and renewal, reader coverage, and delivery to the configured
  recipient.
- Restart recovery, retention/deletion, backups, and rollback compatibility.

Use generic examples and leave private values to the operator. These checks
describe readiness requirements; they do not authorize this repository's
agent to change Electricpeak or connect production accounts.
