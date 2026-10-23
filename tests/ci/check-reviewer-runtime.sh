#!/bin/sh
set -eu

image=ghcr.io/openclaw/openclaw@sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5
reviewer_image=project-watchkeeper-reviewer:ci
docker pull "$image"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 1000:1000 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=0700,uid=1000,gid=1000 \
  --tmpfs /run/watchkeeper/openclaw-auth:rw,noexec,nosuid,size=8m,mode=0700,uid=1000,gid=1000 \
  -e OPENCLAW_CONFIG_PATH=/etc/watchkeeper/openclaw.json5 \
  --entrypoint node "$reviewer_image" /app/openclaw.mjs config validate

# Fail on compiled factory changes so a runtime upgrade gets an explicit review.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 1000:1000 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=0700,uid=1000,gid=1000 \
  --entrypoint node "$reviewer_image" --input-type=module -e '
import fs from "node:fs";
import { pathToFileURL } from "node:url";
const config = JSON.parse(fs.readFileSync("/etc/watchkeeper/openclaw.json5", "utf8"));
const modulePath = "/app/dist/agent-tools-7XKjvsM9.mjs";
if (!fs.existsSync(modulePath)) throw new Error("pinned tool factory module is missing; review before runtime upgrade");
const { n: createTools } = await import(pathToFileURL(modulePath));
const tools = await createTools({ config, agentId: "main", workspaceDir: "/tmp", sessionKey: "agent:main:ci", modelProvider: "openai", modelId: "gpt-6-astra" });
const names = tools.map((tool) => tool.name);
if (names.length !== 0) throw new Error(`expected no tools, got ${JSON.stringify(names)}`);
console.log(JSON.stringify({ status: "passed", tools: names }));
'

set +e
output=$(docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 1000:1000 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=0700,uid=1000,gid=1000 \
  --tmpfs /run/watchkeeper/openclaw-auth:rw,noexec,nosuid,size=8m,mode=0700,uid=1000,gid=1000 \
  -e WATCHKEEPER_REVIEWER_BROKER_TOKEN=synthetic-ci-token \
  -e WATCHKEEPER_REVIEW_ONCE=1 \
  --entrypoint python3 "$reviewer_image" -m watchkeeper.reviewer 2>&1)
status=$?
set -e
if [ "$status" -ne 4 ]; then
  printf '%s\n' "$output" >&2
  echo "reviewer without OAuth must refuse with authentication exit 4; got $status" >&2
  exit 1
fi
printf '%s\n' "$output" | grep -F '"failure": "authentication"' >/dev/null || {
  printf '%s\n' "$output" >&2
  echo "missing-OAuth refusal did not report authentication" >&2
  exit 1
}
echo "pinned reviewer config, empty tool set, and missing-OAuth refusal passed"
