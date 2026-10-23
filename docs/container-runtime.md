# Container runtime and operator configuration

The public Compose files target Watchkeeper `0.1.0` and require Docker Compose
2.24.4 or newer for the production overlay's `!override` network handling.
`compose.yaml` is a synthetic local demonstration: its Basic-auth credentials and intake/reviewer
tokens are public test values, its recipient is `recipient@example.invalid`,
and SMTP is sent only to the bundled local capture sink. It is not a deployment
configuration.

## Synthetic startup and acceptance

Use Docker Compose v2 and Python is not required on the host. The standalone
Compose file consumes published GHCR images and contains no build instructions.
The app binds to
loopback only. The synthetic browser run uses a Linux Docker Engine with host
network mode for its temporary pinned Playwright browser runner. Install the
Playwright 1.64.1 Node package outside the repository; the browser comes from
the pinned runner image. These are test tools and are not shipped in the app
image.

```sh
docker compose config --quiet
docker compose pull
docker compose up --no-build -d --wait
curl -fsS http://127.0.0.1:8765/readyz
```

Sign in with `demo` / `demo-password-not-for-deployment`. The standalone
synthetic application exercise remains available with `python3 -m
watchkeeper.demo`. To run the container boundary and browser journey, install
Playwright in a temporary external tools directory and run:

```sh
PLAYWRIGHT_NODE_MODULES=/path/to/playwright/node_modules \
  WATCHKEEPER_PUBLISH_PORT=8775 WATCHKEEPER_INGRESS_PUBLISH_PORT=8776 \
  tests/compose-acceptance.sh
```

The script creates a unique Compose project, builds candidate images with the
explicit `compose.build.yaml` development overlay, then copies `compose.yaml`
to a temporary directory and starts that copied file with `--no-build --pull
never`. This checks the operator-facing image-only boundary without access to
the source checkout. It builds/starts the app, local SMTP
sink, and private-ingress fixture, exercises direct and ingress authentication,
forged forwarding headers, a controlled explicit request, digest deduplication,
project lifecycle, restart persistence, confirmed deletion, and re-import
exclusion, verifies exactly one captured `.eml` with the fixed synthetic
recipient, then removes only that project's containers and volumes. The test
uses synthetic content and the local sink. Choose unused loopback ports and
keep the project name unique when running in parallel. The script refuses an
already-existing project name and removes only the resources it created.

For local development or CI, add the explicit build overlay:

```sh
docker compose -f compose.yaml -f compose.build.yaml build
```

The pinned browser test image is `mcr.microsoft.com/playwright@sha256:7852dc5a7d72f5fe458a597d690f6c42fbc8e9e26ddfa95f8e6ef552558d9bbf`;
`PLAYWRIGHT_NODE_MODULES` points to an external installation of Playwright
1.64.1 mounted read-only into that disposable runner. Published images target
`linux/amd64`, the platform exercised by container CI; other architectures are
not claimed. GHCR publication runs only after all CI jobs pass on pushes to
this repository's `main`. It publishes both full-commit `sha-<40-character-sha>`
tags before advancing the moving `main` tags. Main-ref workflows do not cancel
an active run, and a head check prevents an older commit from moving aliases
backward. GitHub may replace a pending run with a newer pending run. The two
`main` tag updates are sequential, so infrastructure failure between them can
temporarily leave a mismatched pair. Production should pin both images to a
matching commit or their reported digests. The app image uses the
pinned Python 3.12 base in `Dockerfile`. The reviewer
image embeds its exact read-only runtime config at
`/etc/watchkeeper/openclaw.json5` and uses OpenClaw 2026.10.5 by digest. The app runs as UID 1000, socket group
2000, with a read-only root and a persistent state volume. Compose keeps the
reviewer at UID 1000, drops all capabilities, disables networking, and mounts
only the broker socket directory, inference-proxy socket directory, and its
dedicated OAuth state volume. The proxy is
the only reviewer-side process attached to external networking. It permits
only exact configured DNS names on HTTPS port 443 and rejects non-public DNS
answers. The reviewer itself cannot reach a source account, database, SMTP, or
container runtime.

## Live deployment overlay

`compose.production.yaml` is a template layered on the base file. It removes the
loopback ports and demo credentials/recipient, requires a private HTTPS origin,
uses configured SMTP, and attaches the app to operator-created private ingress
and outbound-mail networks. The mail network must restrict egress to the
configured SMTP server and port; the optional inference network must similarly
be governed by the host firewall. The Compose file cannot enforce network
firewall policy on every driver. Do not publish the application port directly.

Create separate private secret files for the four app credentials and use
`WATCHKEEPER_*_SECRET` variables to identify them. Because local Compose file
secrets preserve host file ownership and mode, provision the mounted files so
UID 1000 can read them while group/other permissions are absent (`0400` or
`0600`). The reviewer broker token file is mounted into both app and reviewer;
its exact bytes must match and remain readable by each UID 1000 process. The
SMTP password, when used, is a separate private file mounted only into the app.
Secret values are limited to 4096 bytes, must be nonempty, and may be supplied
through direct environment values only in synthetic/local setups. Config
rejects a direct value combined with its `_FILE` counterpart.

Example invocation (all referenced files/networks are operator-created):

```sh
export WATCHKEEPER_PUBLIC_BASE_URL=https://watchkeeper.internal.example
export WATCHKEEPER_ALLOWED_ORIGINS=https://watchkeeper.internal.example
export WATCHKEEPER_MAIL_HOST=mail.internal.example
export WATCHKEEPER_MAIL_PORT=587
export WATCHKEEPER_MAIL_SECURITY=starttls
export WATCHKEEPER_RECIPIENT=person@example.invalid
export WATCHKEEPER_MAIL_SENDER=watchkeeper@example.invalid
export WATCHKEEPER_INGRESS_NETWORK=watchkeeper-private-ingress
export WATCHKEEPER_MAIL_NETWORK=watchkeeper-mail-egress
export WATCHKEEPER_INTAKE_TOKEN_SECRET=/run/secrets/watchkeeper/intake-token
export WATCHKEEPER_REVIEWER_TOKEN_SECRET=/run/secrets/watchkeeper/reviewer-token
export WATCHKEEPER_UI_USERNAME_SECRET=/run/secrets/watchkeeper/ui-username
export WATCHKEEPER_UI_PASSWORD_SECRET=/run/secrets/watchkeeper/ui-password
docker compose -f compose.yaml -f compose.production.yaml config --quiet
docker compose -f compose.yaml -f compose.production.yaml up -d
```

For SMTP authentication, add `-f compose.production-smtp-auth.yaml` and set
`WATCHKEEPER_MAIL_USERNAME_SECRET` and `WATCHKEEPER_MAIL_PASSWORD_SECRET` to
private secret files. TLS certificate validation remains enabled. `plain` SMTP is
accepted only for loopback or the named synthetic SMTP sink and cannot carry
credentials. Sender and recipient are fixed configuration; a reviewer proposal
cannot choose them. SMTP disconnect after DATA is treated as ambiguous and is
not retried automatically. The app UI reports this distinction. External mail
already accepted by a server cannot be recalled.

### Shutdown, backup, restore, and rollback

Stop the app with `docker compose stop app` and wait for its container to exit
before copying persistent state. Set `WATCHKEEPER_STOP_GRACE_PERIOD` to a
duration longer than the configured SMTP and HTTP per-operation timeouts; the
default is three minutes. SMTP timeout applies per socket operation rather than
to the entire send, so a relay that makes slow progress can take longer. The
shutdown path marks readiness false, closes HTTP and broker listeners, stops
scheduled work, joins active request/broker threads, and releases the fixed
state lock only after those workers finish. A forced kill can leave delivery
status ambiguous; SMTP DATA is never retried
automatically when acceptance is uncertain.

Back up the complete `watchkeeper-state` volume as one unit, including the
SQLite database, `privacy.jsonl`, and any file-sink captures. For example, while
the app is stopped, use a trusted backup helper with the named volume mounted
read-only and the destination mounted separately. Verify the backup archive
and protect it as private state. If restoring an older database, keep the
current privacy journal (and its deletion events) so startup replays those
purges; restoring a database and journal from the same old snapshot can
resurrect deleted content. Do not roll the journal backward. SMTP mail accepted
by an external server cannot be recalled by restore or deletion.

Apply a new versioned image and let schema migrations run only after taking a
backup. `SCHEMA_VERSION` is 4; versions 0, 1, 2, and 3 migrate forward. A newer
schema fails closed. There is no supported downgrade to a pre-privacy binary;
rollback means restoring the application image only when that image supports
the current schema, otherwise restore a compatible backup while preserving the
current privacy journal and replaying its deletions. Confirm `/readyz`, state,
delivery status, and privacy-journal health after restart.

## Authentication and proxy behavior

App Basic authentication is always required at the direct upstream. The
application does not trust `Forwarded`, `X-Forwarded-User`,
`X-Forwarded-Proto`, or any proxy identity headers; `WATCHKEEPER_PROXY_MODE`
accepts only `ignore`. `WATCHKEEPER_PUBLIC_BASE_URL` and
`WATCHKEEPER_ALLOWED_ORIGINS` define canonical URL and CSRF origins and must
match the private ingress origin. HTTPS deployments set the session cookie's
Secure attribute. The local ingress fixture is only a forwarding test, not an
authentication layer or a production proxy.

## Source collection and reviewer gates

Codex collection is a separate, explicitly invoked host command. It requires
`--enable-codex` and either `--prepare-from-codex-dir` with an explicit source
path or `--source-dir` for a prepared mirror. See [Codex reader compatibility
and source safety](codex-reader.md). The app container never discovers or
mounts a host Codex home.

The live reviewer Compose profile is off by default. `--profile live-reviewer`
starts the network-isolated worker and the narrowly allowlisted inference
proxy. The reviewer image contains the fixed read-only OpenClaw config at
`/etc/watchkeeper/openclaw.json5`; it does not mount configuration from the
host. Before enabling the profile, provision the exact single OpenAI subscription
OAuth profile in the dedicated `reviewer-auth` volume using the supported
OpenClaw authentication flow described in [reviewer operations](reviewer.md).
No API-key fallback is enabled. No login, OAuth refresh, real inference, or
external email was performed for this milestone. Use only an independently
authorized profile and synthetic cases for any later live evaluation. Review
quality, account renewal, and operator network rules remain live-validation
gates.

## Configuration reference

The complete examples are in `compose.yaml` and `compose.production.yaml`.
Set `WATCHKEEPER_IMAGE` and `WATCHKEEPER_REVIEWER_IMAGE` to matching full-SHA
tags or digest references to select the deployment pair. Defaults use moving
`:main` tags. A GHCR package may initially be private; the repository owner
must set the desired visibility and verify anonymous pulls before operators
rely on the pull flow. See [GitHub's container registry guidance](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
The application validates settings at startup and supports `python -m
watchkeeper --check-config` to report a content-free success or error.

| Setting | Purpose |
| --- | --- |
| `WATCHKEEPER_DATABASE`, `WATCHKEEPER_EMAIL_DIR`, `WATCHKEEPER_PRIVACY_JOURNAL` | Persistent app state, local file-sink captures, and privacy journal paths |
| `WATCHKEEPER_INTAKE_TOKEN[_FILE]`, `WATCHKEEPER_REVIEWER_TOKEN[_FILE]`, `WATCHKEEPER_UI_USERNAME[_FILE]`, `WATCHKEEPER_UI_PASSWORD[_FILE]` | Required intake, worker, and UI credentials |
| `WATCHKEEPER_PUBLIC_BASE_URL`, `WATCHKEEPER_ALLOWED_ORIGINS`, `WATCHKEEPER_PROXY_MODE=ignore` | Canonical origin and CSRF allowlist; forwarded identity headers are ignored |
| `WATCHKEEPER_MAIL_TRANSPORT`, `WATCHKEEPER_MAIL_HOST`, `WATCHKEEPER_MAIL_PORT`, `WATCHKEEPER_MAIL_SECURITY` | `file` or SMTP with explicit `plain`, `starttls`, or `ssl` transport |
| `WATCHKEEPER_RECIPIENT`, `WATCHKEEPER_MAIL_SENDER`, `WATCHKEEPER_MAIL_USERNAME`, `WATCHKEEPER_MAIL_PASSWORD_FILE` | Fixed envelope and optional SMTP AUTH (TLS required) |
| `WATCHKEEPER_MAIL_TIMEOUT_SECONDS` | Bounded SMTP connection/operation timeout |
| `WATCHKEEPER_STALE_AFTER_SECONDS`, `WATCHKEEPER_TRANSCRIPT_RETENTION_DAYS` | Source staleness and evidence retention |
| `WATCHKEEPER_INFERRED_AFTER_DAYS` | Minimum complete-coverage inactivity period for inferred items |
| `WATCHKEEPER_SCHEDULE_TIMEZONE`, `WATCHKEEPER_REVIEW_TIME`, `WATCHKEEPER_DIGEST_TIME` | Local schedule settings |
| `WATCHKEEPER_REVIEW_WINDOW_LIMIT`, `WATCHKEEPER_REVIEW_WINDOW_BYTES`, `WATCHKEEPER_REVIEW_WINDOWS_PER_DAY`, `WATCHKEEPER_SCHEDULER_POLL_SECONDS` | Bounded review scheduling |
| `WATCHKEEPER_REVIEWER_BROKER_SOCKET`, `WATCHKEEPER_REVIEWER_BROKER_TOKEN[_FILE]` | App side of the narrow reviewer broker |
| `WATCHKEEPER_INFERENCE_HOSTS` | Exact DNS allowlist consumed by the separate proxy process; literal IPs/wildcards rejected |
| `WATCHKEEPER_BIND_HOST`, `WATCHKEEPER_PORT` | App listener inside its network namespace |

`WATCHKEEPER_TRUSTED_PROXIES` and trusted forwarded identity are unsupported.
Collector-specific options are CLI flags, not unused application environment
settings.
