# Portable milestone verification

The coordinator independently verified the combined Codex-only implementation
on Linux amd64 using synthetic observations, controlled reviewer responses,
local SMTP capture, simulated time and isolated test resources. No private
source account, subscription login, external recipient or Electricpeak
configuration was activated or modified.

## Application and container acceptance

- The final integrated suite passed **93 tests** under the pinned Python 3.12
  container with a read-only root/source, non-root UID, networking disabled
  and ResourceWarning errors enabled. It covers reader pagination/revisions,
  policy, persistent decisions, retention/deletion, bounded review recovery,
  notification deduplication, configuration and SMTP acceptance classification.
- Both application and reviewer Dockerfiles built from their pinned base
  digests with build networking disabled. Build context is restricted to the
  Dockerfiles and Python source. The application has no third-party Python
  runtime dependencies.
- A fresh isolated Compose project started the app, local SMTP capture sink
  and forwarding ingress. Both documented loopback URLs became ready. The
  SMTP sink stays on the internal application network; the demo publishing
  bridge is removed by the production overlay.
- Actual Chromium controls submitted a synthetic project, sent exactly one
  fixed-recipient SMTP digest, suppressed repeated delivery, and exercised
  complete/reopen, abandon/reopen, archive/restore and snooze. Graceful app
  shutdown exited zero. After restart, snooze and accepted-delivery history
  persisted. Cancelling deletion preserved the project; confirming it removed
  the project and excluded its thread. Re-import stored no excluded content.
- Direct upstream and forwarding-ingress requests both rejected forged
  forwarding identity headers, unauthenticated state access, wrong-role tokens
  and actions without CSRF protection. Authenticated access worked through both
  routes. The HTTPS production configuration sets a Secure CSRF cookie.
- The production overlay started with synthetic private secret files readable
  by UID 1000. Its rendered app networks contain only application, mail egress
  and private ingress; it publishes no app port and retains no demo credential
  value. A second serving process sharing its state was rejected by the fixed
  process lock.

The repository provides `tests/compose-acceptance.sh` and
`tests/compose-browser.cjs` for repeating the local browser journey. See
[container operations](container-runtime.md) for test prerequisites and
configuration. The coordinator independently ran the repository runner with
`WATCHKEEPER_COMPOSE_PROJECT=watchkeeper-07-final-review`, app port 8785,
ingress port 8786 and an external Playwright 1.64.1 dependency directory. Both
browser phases and the captured-email assertion passed; the runner removed
its synthetic resources. The implementation agent separately reproduced the
same checks in a different project. The local ingress is a forwarding fixture, not production Envoy.

## Transport, shutdown and restore

The coordinator separately exercised actual STARTTLS and implicit TLS against
an ephemeral local certificate. Both paths accepted a fixed-recipient message,
classified an explicit recipient rejection as retryable, and classified a
connection loss after DATA as ambiguous. Message bytes used CRLF framing. The
certificate/key existed only in a temporary directory. Deterministic transport
checks cover the same acceptance distinctions without requiring an account.

A real application process received SIGTERM during an active synthetic SMTP
DATA exchange. It stayed alive and retained its state lock while the request
finished; a second process could not acquire the state. After transport
acceptance, shutdown exited zero and restart retained the accepted batch.
Forced termination during external SMTP acceptance remains ambiguous and is
not automatically retried; SMTP does not provide exactly-once delivery.

The container restore check retained a synthetic database backup from before
an authenticated deletion, stopped the app, restored only that old database
under the state lock and preserved the current privacy journal. Startup replay
removed the restored project and retained its thread exclusion before readiness.
Earlier tests also cover current raw/retained evidence precedence, shared-thread
one-hop deletion, aggregate capture cleanup, interrupted journal replay,
forward migrations and rejection of newer unsupported schemas. External mail
and independently retained backup copies cannot be recalled by deletion.

## Whole-reviewer boundary

The built OpenClaw container ran as UID 1000 with no network, a read-only root,
dropped capabilities, no-new-privileges, separate narrow socket-directory
mounts and dedicated subscription state. Independent checks confirmed:

- Protected configuration and code could not be opened for writing.
- App database, intake/UI secrets, source homes and container-runtime sockets
  were absent; direct public TCP connectivity failed.
- The broker accepted authenticated reviewer health requests and rejected
  project state, intake and delivery/user-action paths.
- The actual pinned OpenClaw tool factory returned `[]` under the protected
  config. Only the required OpenAI provider plugin is allowed; agent tools are
  denied and MCP servers are empty. This restriction covers the application
  process, including provider/plugin execution.
- The actual worker accepted its private broker-token file and exited with an
  authentication failure before claiming evidence when subscription state was
  empty. No fallback to model API keys occurred.
- The packaged inference proxy rejected literal-IP/wildcard/port-bearing
  allowlist entries, private DNS results, unlisted hosts, non-443 destinations
  and methods other than CONNECT. No external service was contacted by these
  denial checks.

## Remaining gates

Tickets 02 and 03 remain partially verified. Actual Mac/owner-history reader
compatibility, authorized subscription inference and renewal, provider endpoint
compatibility and representative real model judgment/hostile-input evaluation
remain unverified. Controlled reviewer output and an empty tool set do not
prove semantic quality. See [reader evidence](evidence-02.md),
[reviewer evidence](evidence-03.md) and the
[integration gates](integration-evidence.md).

The deployment owner must separately provision private ingress, secrets,
network policy, state/backup storage, a fixed mail recipient/transport and any
explicitly authorized account activation. This implementation provides the
[Electricpeak handoff](deployment-electricpeak.md) without changing that
repository, Envoy, firewall, Nix configuration or existing services.
