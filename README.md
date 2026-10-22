# Project Watchkeeper

A self-hosted monitor that tracks unfinished projects across AI conversations
and surfaces the next action that needs your attention.

The repository contains a synthetic local demonstration, an explicitly
invoked Codex collector, a restricted optional reviewer, an authenticated web
application, and configurable container artifacts. The default Compose stack
uses only generated content, demo credentials, and a local SMTP capture sink;
it does not connect to a source account, hosted model, or external mail server.

For a local container startup with Docker Compose 2.24.4 or newer:

```sh
docker compose config --quiet
docker compose up --build -d --wait
curl -fsS http://127.0.0.1:8765/readyz
```

Open `http://127.0.0.1:8765` and sign in with the public synthetic credentials
`demo` / `demo-password-not-for-deployment`. Run the controlled browser,
direct-upstream authentication, ingress, restart, deletion, and local-mail
acceptance with external Playwright tooling using
[`tests/compose-acceptance.sh`](tests/compose-acceptance.sh). For the standalone
Python flow, run `python3 -m watchkeeper.demo`; the test suite runs with
`python3 -m unittest discover -s tests -v`.

The app and reviewer images are versioned `0.1.0` and use pinned base image
digests. `compose.production.yaml` is an operator template that requires
private secret files, explicit HTTPS origins, an SMTP server, and externally
managed private networks. Read [container operations](docs/container-runtime.md)
before adapting it. Live Codex, OAuth, model judgment, outbound SMTP, ingress
firewall rules, and deployment-specific renewal behavior remain explicit
operator validation gates; they were not exercised with private accounts or
external services here.

See the [application contract](docs/application-contract.md), [Codex source
reader](docs/codex-reader.md), [restricted reviewer](docs/reviewer.md),
[recovery guidance](docs/recovery.md), and [Electricpeak handoff](docs/deployment-electricpeak.md).
See [continuous integration](docs/ci.md) for GitHub Actions checks and local reproduction.

- [Feature specification](.scratch/project-watchkeeper/spec.md)
- [Implementation guide and acceptance checks](docs/implementation.md)
- [Integration evidence and limitations](docs/integration-evidence.md)
- [Domain glossary](GLOSSARY.md)
- [Observation boundary ADR](docs/adr/0001-separate-observation-from-project-execution.md)
- [Agent conventions](AGENTS.md)
