# Dependency verification

These checks use public packages and synthetic state only. They do not establish
compatibility with an owner's Mac installation or semantic reviewer quality.

## Pinned dependencies

| Component | Version / immutable artifact | Verified here |
| --- | --- | --- |
| Codex app-server | CLI 0.162.1 | Generated the installed binary's JSON protocol schemas; inspected thread list/read/turn pagination contracts |
| OpenClaw | 2026.10.5; `ghcr.io/openclaw/openclaw@sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5` | Executed version, config validation, and actual tool construction in the pinned Linux image |
| Python | 3.12 slim; `python@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2` | First vertical slice's two HTTP acceptance tests passed as uid 65534, read-only root/source, no network, writable tmpfs |

The OpenClaw image reports `OpenClaw 2026.10.5 (1b58cb9)` and Node 24.21.0.
The image index contains Linux amd64 and arm64 manifests. Execution here was
Linux amd64; this is not an arm64 runtime claim.

## Reviewer configuration and callable tools

Mounted `runtime/openclaw/watchkeeper-review.json5` read-only into the pinned
image, with no credentials, `--network none`, `--read-only`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, uid/gid 1000, and writable `/tmp` tmpfs.
`node /app/openclaw.mjs config validate` returned `Config valid`.

Independently invoked the image's actual `createOpenClawCodingToolsAsync`
factory (export `n` from `/app/dist/agent-tools-7XKjvsM9.mjs`) with that parsed
configuration, an empty `/tmp` workspace, agent `main`, and a synthetic session
key. Its returned tool names were `[]`. This checks the runtime's assembled
tool surface, rather than trusting a configuration key alone. The compiled
module path is specific to this immutable image; repeat discovery and this
check when upgrading it.

The package's `agent exec` result envelope contains `ok`, `status`, and `final`.
Its `--config` option cannot be combined with `--isolated`; the application
therefore supplies a protected explicit configuration, a cleaned environment,
isolated disposable execution state, and narrowly scoped subscription OAuth
state. Only the required `openai` inference plugin is allowed, all tools are
denied, MCP servers are empty, and the model runtime is explicitly `openclaw`.
The current canonical provider is `openai`; the legacy `openai-codex` name
must not be used to trigger automatic native-harness installation.

Configuration and tool-factory checks do not prove containment of a deployed
container, successful OAuth renewal, provider endpoint compatibility, model
availability, or useful semantic judgments. Those are separate checks and
activation gates in the integration evidence and deployment handoff.

## Codex protocol findings

The installed binary's generated schemas confirm `thread/list` supports
`sortKey: updated_at`, descending order, pagination, explicit `sourceKinds`,
and separate archived/unarchived scans. Omitting source kinds excludes some
session origins. The collector uses `useStateDbOnly: false` solely in a
disposable credential-free transcript mirror, allowing metadata reconstruction
without writing to the source installation. It never copies source SQLite
databases or follows their absolute rollout paths.
`thread/read` does not resume a thread. `thread/turns/list` with `itemsView:
full` supplies turn items with continuation; a reader must not assume every
historical item supports a separate item-list call.

Thread `updatedAt` and turn `startedAt`/`completedAt` values are Unix seconds;
item-entry `startedAtMs`/`completedAtMs` values are milliseconds. A `notLoaded`
execution status means unknown activity, not idle. A controlled legacy binary run read both user and assistant items, reached
the authenticated page and captured a digest. Mac, modern rollout, nonempty
archived history and multipage binary compatibility remain unverified.

No source account was connected and no private source conversation was read
for these dependency checks.

The pinned image also verified `models auth login --help` supports provider,
method, profile-id and agent selection. No login command was executed.
