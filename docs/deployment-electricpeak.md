# Electricpeak deployment contract

Status: Implementation guidance; no deployment is performed by this PR.

Project Watchkeeper provides portable container packaging and documented
configuration, credential, and persistent-state interfaces. Electricpeak owns
host-specific wiring and private configuration.

## Existing seams to reuse

- Author Compose under `containers/source/<group>/docker-compose.yml`.
  The repository's generator and PR workflow produce Compose Nix modules.
  Add a new generated group's import to `configuration.nix`; do not edit
  generated `docker-compose.nix` by hand.
- Follow the existing Home Manager configuration mirroring and `onChange`
  patterns for files under `containers/config`. Container-visible files must
  not depend on inaccessible Nix-store symlinks.
- Use runtime file secrets, narrow read-only mounts, ownership, and consumer
  ordering/restarts through `modules/onepassword-secrets/default.nix`.
  Never put resolved values or service-account tokens into source.
- Pin deployed images by reviewed version/digest and keep persistent state
  outside the container image. The deployment chooses UID/GID and storage.
- Run applicable `nix flake check --no-build` and
  `sudo nixos-rebuild build --flake .#electricpeak` checks for the later
  Electricpeak integration change. Production deployment occurs from main.
  These are not runtime tests for this documentation-only PR.

## Envoy and access

Expose the single-user control page through the existing Envoy service,
following the established Electricpeak service pattern. Configure upstream
address, private base URL, allowed origins, authentication, and private
reachability in deployment configuration. Do not publish an additional UI
host port. Ensure upstream access cannot bypass authentication.

The public networking Compose mounts Envoy configuration and certificates
read-only; the secret module connects its configuration to consumer restart
handling. Public documentation also describes private Envoy configuration.
The private production routes are unavailable in the inspected checkout, and
the synchronization between the documented private file and runtime secret
attachment is not established. Verify the production source of truth before
changing routes; do not invent a reload or synchronization mechanism.

Private networking and Envoy routing do not establish authentication by
themselves. No existing single-user authentication mechanism was verified in
the public checkout. Verify or supply that boundary before exposing project
data. Preserve existing firewall and overlay-network restrictions.

Joining the existing shared Docker bridge is not sufficient reviewer
isolation. Define reachability between Envoy, the control service, collectors,
store, reviewer, and sender explicitly; verify denied paths as well as
working routes.

## Email and account activation

No outbound email transport was verified in the public Electricpeak
configuration. Supply a configured mail relay or user-authorized account
transport and a fixed recipient at deployment time. Do not assume an existing
Gmail integration or embed a third-party API key requirement.

Provision source-account access only after reader and containment checks.
Provision the reviewer subscription login separately. Support explicit
reauthentication without exposing tokens in logs or public configuration.

## Rollout and rollback gates

Before activation, verify pinned-reader compatibility, private authentication,
restricted network paths, volume permissions, retention/deletion, backup
behavior, and delivery to the configured recipient. Start with synthetic
fixtures and a captured digest, then enable the agreed real sources and
delivery.

Keep prior image/config versions and compatible state backups available.
State migrations must document rollback compatibility. Disable scheduling
and delivery independently while investigating failures; preserve the
project register and user decisions.

## Inspected Electricpeak paths

- `AGENTS.md`, `configuration.nix`
- `containers/generate-docker-modules.sh`
- `containers/source/networking/docker-compose.yml`
- `containers/source/utilities/docker-compose.yml`
- `modules/onepassword-secrets/default.nix`
- `users/junr03/files.nix`, `users/junr03/scripts/mirror-file.sh`
- `docs/secrets.md`
- `.github/actions/generate_docker_modules/action.yml`
- `.github/workflows/nixos.yml`

These establish integration patterns, not the private deployment's current
hostnames, credentials, authentication policy, or firewall state.
