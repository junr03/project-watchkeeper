# Project Watchkeeper

A self-hosted monitor that tracks unfinished projects across AI conversations
and surfaces the next action that needs your attention.

The first synthetic slice accepts Codex-shaped observations, stores one
evidence-backed project, renders an authenticated control page, and captures a
deduplicated digest in a local `.eml` sink. It does not connect to live Codex
threads or send real email. Other sources and live reader compatibility are
later work.

Run the synthetic flow with Python 3.12 or newer:

```sh
python3 -m watchkeeper.demo
```

Run the application acceptance suite with:

```sh
python3 -m unittest discover -s tests -v
```

See the [application contract](docs/application-contract.md) for the HTTP
boundaries, credentials, request shapes, and production service settings.

- [Feature specification](.scratch/project-watchkeeper/spec.md)
- [Codex-only implementation tickets](.scratch/project-watchkeeper/README.md)
- [Implementation guide and acceptance checks](docs/implementation.md)
- [Electricpeak deployment handoff](docs/deployment-electricpeak.md)
- [Integration evidence and limitations](docs/integration-evidence.md)
- [Domain glossary](GLOSSARY.md)
- [Observation boundary](docs/adr/0001-separate-observation-from-project-execution.md)
- [Agent conventions](AGENTS.md)
