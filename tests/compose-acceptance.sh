#!/bin/sh
# Build and run the synthetic container/browser journey in isolated Compose resources.
set -eu

project=${WATCHKEEPER_COMPOSE_PROJECT:-watchkeeper-07-acceptance-$$}
case "$project" in *[!a-zA-Z0-9_-]*|'') echo "invalid Compose project name" >&2; exit 2;; esac
app_port=${WATCHKEEPER_PUBLISH_PORT:-8775}
ingress_port=${WATCHKEEPER_INGRESS_PUBLISH_PORT:-8776}
playwright_image=${WATCHKEEPER_PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright@sha256:7852dc5a7d72f5fe458a597d690f6c42fbc8e9e26ddfa95f8e6ef552558d9bbf}
export WATCHKEEPER_PUBLISH_PORT="$app_port"
export WATCHKEEPER_INGRESS_PUBLISH_PORT="$ingress_port"
export WATCHKEEPER_PUBLIC_BASE_URL="http://127.0.0.1:$app_port"
export WATCHKEEPER_ALLOWED_ORIGINS="$WATCHKEEPER_PUBLIC_BASE_URL"
export WATCHKEEPER_ACCEPTANCE_URL="$WATCHKEEPER_PUBLIC_BASE_URL"
export WATCHKEEPER_ACCEPTANCE_INGRESS_URL="http://127.0.0.1:$ingress_port"
compose_file=$(mktemp -d)
created=0
cleanup() {
  if [ "$created" -eq 1 ]; then
    docker compose --project-name "$project" -f "$compose_file/compose.yaml" --profile acceptance down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$compose_file"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
cp compose.yaml "$compose_file/compose.yaml"
export WATCHKEEPER_IMAGE=project-watchkeeper:local
export WATCHKEEPER_REVIEWER_IMAGE=project-watchkeeper-reviewer:local
compose() { docker compose --project-name "$project" -f "$compose_file/compose.yaml" --profile acceptance "$@"; }
build_compose() { docker compose --project-name "$project" -f compose.yaml -f compose.build.yaml --profile acceptance "$@"; }
if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ] || \
   [ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$project")" ] || \
   [ -n "$(docker network ls -q --filter "label=com.docker.compose.project=$project")" ]; then
  echo "Compose project name already has resources: $project" >&2
  exit 2
fi
command -v docker >/dev/null || { echo "Docker is required" >&2; exit 2; }
playwright_modules=${PLAYWRIGHT_NODE_MODULES:-}
[ -n "$playwright_modules" ] && [ -d "$playwright_modules" ] || {
  echo "Set PLAYWRIGHT_NODE_MODULES to an external directory containing Playwright 1.64.1" >&2; exit 2;
}
created=1
build_compose build
compose up --no-build --pull never -d --wait
curl -fsS "http://127.0.0.1:$app_port/readyz" >/dev/null || {
  echo "published loopback app port is not reachable from the host" >&2; exit 1;
}
run_browser() {
  phase=$1
  docker run --rm --network host --read-only --cap-drop ALL \
    --security-opt no-new-privileges --shm-size 512m --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    -v "$PWD/tests/compose-browser.cjs:/acceptance.cjs:ro" \
    -v "$playwright_modules:/runner/node_modules:ro" \
    -e NODE_PATH=/runner/node_modules \
    -e WATCHKEEPER_ACCEPTANCE_URL="$WATCHKEEPER_ACCEPTANCE_URL" \
    -e WATCHKEEPER_ACCEPTANCE_INGRESS_URL="$WATCHKEEPER_ACCEPTANCE_INGRESS_URL" \
    "$playwright_image" node /acceptance.cjs "$phase"
}
run_browser exercise
compose exec -T smtp-sink python -c 'from pathlib import Path; files=list(Path("/var/lib/watchkeeper/smtp").glob("*.eml")); assert len(files)==1, f"expected exactly one captured digest, got {len(files)}"; message=files[0].read_bytes(); assert b"To: recipient@example.invalid" in message and b"X-Watchkeeper-Delivery-ID:" in message'
compose restart app
# Wait for readiness after restart before rechecking persistent user state.
i=0
until compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=2)" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 30 ] || { echo "app did not become ready after restart" >&2; exit 1; }
  sleep 1
done
run_browser after-restart
