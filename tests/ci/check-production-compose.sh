#!/bin/sh
set -eu

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
for name in intake reviewer username password mail-username mail-password; do
  printf 'synthetic-%s-value\n' "$name" >"$tmp/$name"
  chmod 600 "$tmp/$name"
done

export WATCHKEEPER_PUBLIC_BASE_URL=https://watchkeeper.ci.invalid
export WATCHKEEPER_ALLOWED_ORIGINS=https://watchkeeper.ci.invalid
export WATCHKEEPER_MAIL_HOST=mail.ci.invalid
export WATCHKEEPER_MAIL_PORT=587
export WATCHKEEPER_MAIL_SECURITY=starttls
export WATCHKEEPER_RECIPIENT=recipient@ci.invalid
export WATCHKEEPER_MAIL_SENDER=watchkeeper@ci.invalid
export WATCHKEEPER_INGRESS_NETWORK=watchkeeper-ci-private-ingress
export WATCHKEEPER_MAIL_NETWORK=watchkeeper-ci-mail-egress
export WATCHKEEPER_INTAKE_TOKEN_SECRET="$tmp/intake"
export WATCHKEEPER_REVIEWER_TOKEN_SECRET="$tmp/reviewer"
export WATCHKEEPER_UI_USERNAME_SECRET="$tmp/username"
export WATCHKEEPER_UI_PASSWORD_SECRET="$tmp/password"
export WATCHKEEPER_MAIL_USERNAME_SECRET="$tmp/mail-username"
export WATCHKEEPER_MAIL_PASSWORD_SECRET="$tmp/mail-password"

docker compose -f compose.yaml -f compose.production.yaml -f compose.production-smtp-auth.yaml config --quiet
docker compose -f compose.yaml -f compose.production.yaml -f compose.production-smtp-auth.yaml config --format json >"$tmp/compose.json"
python3 - "$tmp/compose.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    config = json.load(stream)
app = config["services"]["app"]
assert set(app["networks"]) == {"application", "mail-egress", "private-ingress"}, app["networks"]
assert not app.get("ports"), app.get("ports")
assert not app.get("depends_on"), app.get("depends_on")
for secret in ("intake_token", "reviewer_token", "ui_username", "ui_password",
               "mail_username", "mail_password"):
    assert secret in config["secrets"], secret
environment = app["environment"]
for key in ("WATCHKEEPER_INTAKE_TOKEN", "WATCHKEEPER_REVIEWER_TOKEN",
            "WATCHKEEPER_REVIEWER_BROKER_TOKEN", "WATCHKEEPER_UI_USERNAME",
            "WATCHKEEPER_UI_PASSWORD"):
    assert environment.get(key) == "", (key, environment.get(key))
assert environment["WATCHKEEPER_RECIPIENT"] == "recipient@ci.invalid"
assert environment["WATCHKEEPER_MAIL_USERNAME_FILE"] == "/run/secrets/mail_username"
assert environment["WATCHKEEPER_MAIL_PASSWORD_FILE"] == "/run/secrets/mail_password"
print("production and SMTP-auth Compose overlays rendered with private synthetic files")
PY
