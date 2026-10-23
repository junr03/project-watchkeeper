# Restricted OpenClaw reviewer

The reviewer is a scheduled, subscription-authenticated worker. It claims one
bounded job at a time from the reviewer-only application broker, returns
structured proposals, and has no source-account login or user-action endpoint. Captured
conversation text is untrusted evidence. The pinned OpenClaw configuration
denies every agent tool, permits only the bundled OpenAI provider plugin, has an
empty MCP server list, and explicitly selects `agentRuntime.id: openclaw`.

## Runtime and credential boundary

The runtime image is pinned to
`ghcr.io/openclaw/openclaw@sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5`
(OpenClaw 2026.10.5, `1b58cb9`). The committed configuration is
[`runtime/openclaw/watchkeeper-review.json5`](../runtime/openclaw/watchkeeper-review.json5).
The pinned image accepted it with `openclaw config validate` while running
without network, credentials, or a writable root filesystem. This establishes
schema validity only; it does not establish OAuth validity or reviewer quality.

The runtime boundary checks were performed with this image digest, a non-root
UID, `--network none`, a read-only root, all Linux capabilities dropped, and
`no-new-privileges`. Reproduce the configuration check from the repository
root with:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp --user 1000:1000 \
  -e HOME=/tmp -e OPENCLAW_CONFIG_PATH=/review.json5 \
  -v "$PWD/runtime/openclaw/watchkeeper-review.json5:/review.json5:ro" \
  ghcr.io/openclaw/openclaw@sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5 \
  node /app/openclaw.mjs config validate
```

The pinned image's bundled coding-tool factory was also imported with the
configuration mounted read-only under the same isolation flags. It returned
`{"tools":[]}`. This exact tool-set check is reproducible with:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp --user 1000:1000 \
  -e HOME=/tmp -e OPENCLAW_STATE_DIR=/tmp/openclaw \
  -v "$PWD/runtime/openclaw/watchkeeper-review.json5:/review.json5:ro" \
  ghcr.io/openclaw/openclaw@sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5 \
  node --input-type=module -e 'import fs from "node:fs"; const m=await import("/app/dist/agent-tools-7XKjvsM9.mjs"); const config=JSON.parse(fs.readFileSync("/review.json5","utf8")); const tools=await m.n({config,agentId:"main",workspaceDir:"/tmp",sessionKey:"agent:main:watchkeeper-test"}); console.log(JSON.stringify({tools:tools.map(t=>t.name)})); process.exit(tools.length===0?0:1);'
```

To verify that an empty auth mount fails before any evidence job is claimed,
run the repository worker in a deployment with the authenticated application
broker socket mounted at `/run/watchkeeper/app.sock`. Set
`WATCHKEEPER_REVIEW_ONCE=1` to perform exactly one preflight and exit (status 4
for authentication failure); the default worker polls continuously.

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp --tmpfs /run/watchkeeper/openclaw-auth:uid=1000,gid=1000,mode=0700 \
  --user 1000:1000 -e HOME=/tmp -e OPENCLAW_STATE_DIR=/run/watchkeeper/openclaw-auth \
  -e PYTHONPATH=/opt/watchkeeper -e PYTHONDONTWRITEBYTECODE=1 \
  -e WATCHKEEPER_REVIEWER_BROKER_TOKEN=synthetic-test-token \
  -e WATCHKEEPER_REVIEW_ONCE=1 \
  -v "$PWD:/opt/watchkeeper:ro" \
  -v "$WATCHKEEPER_APP_SOCKET:/run/watchkeeper/app.sock" \
  -v "$PWD/runtime/openclaw/watchkeeper-review.json5:/etc/watchkeeper/openclaw.json5:ro" \
  ghcr.io/openclaw/openclaw@sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5 \
  python3 -m watchkeeper.reviewer
```

These checks validate only the pinned configuration, empty tool set, and
fail-closed authentication preflight. No login, subscription refresh, proxy
connectivity, or live model inference was attempted.

Provision one OpenAI subscription OAuth profile with the exact id
`openai:watchkeeper-subscription` in a separate operator-controlled auth
directory. Use OpenClaw's supported auth command in a separately authorized
setup environment, targeting the dedicated state directory and agent:

```sh
openclaw models auth login --provider openai --method device-code \
  --profile-id openai:watchkeeper-subscription --agent main
```

Mount only that OpenClaw state directory at `/run/watchkeeper/openclaw-auth`
with the narrow write access needed for OAuth refresh. Keep it separate from
the read-only config mount. The worker preflights `models auth list` and refuses
to run unless this exact profile is the only OpenAI profile and its credential
type is `oauth`; no API-key fallback is selected. Do not mount a source
account, general home directory, Codex history, or container-runtime socket.
The worker starts
`openclaw agent exec` with the committed config, one fixed model, one fixed
profile order, and a cleaned environment. It passes no API-key variable, model
fallback, arbitrary config path, or user-controlled command. The normal
`agent exec` temporary session mode is overridden with a worker-owned temporary
state directory so startup recovery and a `finally` cleanup remove each run's
transcript and prompt artifacts.

Run the whole reviewer container with no network interface, a read-only root,
no added capabilities, no-new-privileges, and only two narrow Unix sockets:

- The application broker exposes authenticated reviewer health and
  lease-bound job claim, completion, and failure routes. The legacy bounded
  `GET /api/review-input` and `POST /api/reviews` routes remain available, but
  the scheduled worker uses the job routes. User actions are rejected by this
  broker.
- The inference proxy exposes HTTPS `CONNECT` only to exact configured DNS
  names on port 443. It resolves the destination, rejects any private or
  reserved address, and connects to the selected public IP. No redirects or
  model-selected hosts are added to the allowlist. The reviewer uses a
  loopback-to-Unix bridge because OpenClaw's Node fetch honors the environment
  proxy when `NODE_USE_ENV_PROXY=1`.

The separately networked proxy process accepts `WATCHKEEPER_INFERENCE_HOSTS`
as a comma-separated list of exact public inference hostnames, for example
`api.openai.com,auth.openai.com,chatgpt.com`. Operators must verify the
destinations required by their pinned subscription route before activation;
wildcards, literal IPs, and non-443 CONNECT requests are rejected. The proxy
does not expose the application broker or user actions. Its Unix socket is the
only network path available to the isolated reviewer.

The broker token is passed to the worker process, not to OpenClaw. The child
environment contains only the runtime path, an empty `HOME`, the dedicated
OAuth state path, the temporary loopback proxy, and basic locale settings.
Source text, prompt, and model output are not written by Watchkeeper to durable
storage or logs. OpenClaw's per-run state directory is removed after success or
failure; stale directories are removed under a process lock at the next run.

## Proposal contract and deterministic policy

Every proposal has exactly these input fields:

```json
{
  "project_id": "garden-guide",
  "outcome": "Publish a tested garden guide",
  "next_action": "Choose three planting zones",
  "reasoning": "The captured source asks the user to make this choice.",
  "evidence_refs": ["opaque-observation-id"],
  "evidence_revisions": {"opaque-observation-id": "3"},
  "status": "explicit_request",
  "grouping_confidence": "strong"
}
```

Supported statuses are `explicit_request`, `inferred`, `active_work`,
`external_dependency`, `uncertain_completion`, `uncertain_grouping`, and
`no_action`. Grouping confidence is `strong` or `uncertain`. A successful
review with no proposals is the exact output `{"proposals":[]}`.

The service rejects malformed, unknown, stale, or unsupported evidence
references and compares every proposed revision with the currently stored
revision. It derives elapsed progress from the newest stored source timestamp
across every referenced source/thread pair, so an old selected message cannot
hide newer observed work. Missing, unavailable, malformed, or stale source
context is fail-closed. Inferred eligibility additionally requires a concrete
next action, the configurable `WATCHKEEPER_INFERRED_AFTER_DAYS` inactivity
threshold (default three days), and explicit
per-thread `completeness: "complete"` with no reported gaps. A trusted active
thread signal, external dependency, uncertain completion, uncertain grouping,
or incomplete coverage cannot become an emailed attention item. Aggregate
source coverage must also be `coverage.state: "complete"` with an empty gaps
list; a new partial scan cannot inherit an older completed thread record as
proof of current inactivity. The inferred-action check requires a recognized
action verb followed by an object (for example, “Choose three planting zones”);
valid phrasings outside that conservative list may be held as proposals until
reviewed. Service policy remains the authority.

The reviewer HTTP contract is separate from a review run's status. The worker
emits content-free JSON status and distinguishes `quota`, `authentication`,
`unavailable`, and `invalid_output` failures from `success` with zero
proposals. A rejected proposal or changed revision is not counted as a
successful empty review. Failure payloads contain no transcript or credential
material.

## Synthetic and live evaluation

`fixtures/review-cases/cases.json` contains fabricated cases and a human scoring
rubric. Run deterministic boundary checks with:

```sh
python3 -m unittest discover -s tests
```

For an actual model evaluation, an operator must separately authorize and
provision the dedicated subscription OAuth profile, run the pinned container
with the boundaries above, load only the public synthetic cases into a
disposable local Watchkeeper instance, and invoke one bounded review per case.
Record expected versus observed status/action, unsupported claims, missed
evidence, false positives, grouping mistakes, runtime/model identity, and
failure classification. Keep detailed output private; publish only redacted,
content-minimized results. Quota, login, proxy, and invalid-output failures must
remain visible. A successful empty response and a failed review are different
results.

No subscription login or live model judgment was performed for this change.
The image config validation and local policy/broker/proxy tests do not prove
OAuth renewal, inference-host compatibility, semantic accuracy, false-positive
rates, or hostile-input resistance in a real model run. The application
scheduler gives each local day a bounded newest-evidence review window followed
by resumable historical backfill windows. Every worker job has a byte-bounded
snapshot and durable lease; an incomplete or stale result is discarded.
