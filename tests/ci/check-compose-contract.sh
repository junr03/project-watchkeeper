#!/bin/sh
set -eu

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
for name in intake reviewer username password; do
  printf 'compose-contract-%s\n' "$name" >"$tmp/$name"
  chmod 600 "$tmp/$name"
done
export WATCHKEEPER_IMAGE=example.invalid/watchkeeper-app:test
export WATCHKEEPER_REVIEWER_IMAGE=example.invalid/watchkeeper-reviewer:test
export WATCHKEEPER_PUBLIC_BASE_URL=https://watchkeeper.compose.invalid
export WATCHKEEPER_ALLOWED_ORIGINS=https://watchkeeper.compose.invalid
export WATCHKEEPER_MAIL_HOST=mail.compose.invalid
export WATCHKEEPER_MAIL_PORT=587
export WATCHKEEPER_MAIL_SECURITY=starttls
export WATCHKEEPER_RECIPIENT=recipient@compose.invalid
export WATCHKEEPER_MAIL_SENDER=watchkeeper@compose.invalid
export WATCHKEEPER_INGRESS_NETWORK=watchkeeper-compose-ingress
export WATCHKEEPER_MAIL_NETWORK=watchkeeper-compose-mail
export WATCHKEEPER_INTAKE_TOKEN_SECRET="$tmp/intake"
export WATCHKEEPER_REVIEWER_TOKEN_SECRET="$tmp/reviewer"
export WATCHKEEPER_UI_USERNAME_SECRET="$tmp/username"
export WATCHKEEPER_UI_PASSWORD_SECRET="$tmp/password"

docker compose --profile '*' -f compose.yaml config --format json >"$tmp/base.json"
docker compose --profile '*' -f compose.yaml -f compose.production.yaml config --format json >"$tmp/production.json"
docker compose --profile '*' -f compose.yaml -f compose.build.yaml config --format json >"$tmp/build.json"
python3 - "$tmp/base.json" "$tmp/production.json" "$tmp/build.json" <<'PY'
import json
import sys

base, production, build = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
services = base["services"]
app_image = "example.invalid/watchkeeper-app:test"
reviewer_image = "example.invalid/watchkeeper-reviewer:test"
for config in (base, production):
    for name, service in config["services"].items():
        assert service["image"] == (reviewer_image if name == "reviewer" else app_image), (name, service.get("image"))
        assert "build" not in service, (name, service.get("build"))
        for mount in service.get("volumes", []):
            assert mount.get("type") != "bind", (name, mount)
reviewer = services["reviewer"]
assert reviewer["network_mode"] == "none"
assert reviewer["read_only"] is True
assert "live-reviewer" in reviewer["profiles"]
assert services["inference-proxy"]["profiles"] == ["live-reviewer"]
assert services["app"]["image"] == services["smtp-sink"]["image"] == services["private-ingress"]["image"] == services["inference-proxy"]["image"]
assert build["services"]["app"]["image"] == "project-watchkeeper:local"
assert build["services"]["reviewer"]["image"] == "project-watchkeeper-reviewer:local"
for name in ("app", "smtp-sink", "private-ingress", "inference-proxy"):
    assert "build" in build["services"][name], name
assert "build" in build["services"]["reviewer"]
print("base, production overlay, and explicit build overlay contracts passed")
PY
