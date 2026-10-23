# Continuous integration

The GitHub Actions workflow runs on every pull request base branch, pushes to
`main`, and manual dispatch. Pull request triggers let stacked branches run CI
without a branch filter. Workflows added at the top of an existing PR stack do
not retroactively appear on lower PR commits; those checks begin when each
lower PR receives a commit containing this workflow. Manual dispatch becomes
available after the workflow reaches the repository's default branch.

Every job uses an `ubuntu-24.04` hosted runner, has a time limit, and receives
only the repository's read-only `contents` permission. Checkout does not retain
credentials. The workflow has no secrets, linked accounts, source histories,
private fixtures, real model calls, or real notifications. Jobs may run in
parallel and duplicate runs on the same PR or ref are cancelled when a newer
run starts.

The workflow reports these checks:

- **Workflow lint** downloads actionlint 1.7.12 and verifies its published
  archive checksum before checking the Actions YAML. Actions are pinned to
  full commit SHAs verified against their upstream releases.
- **Python 3.12 tests** and **Python 3.14 tests** run the complete unittest
  suite with `ResourceWarning` treated as an error.
- **Container builds and reviewer checks** builds both images with build
  networking disabled, renders the production plus SMTP-auth Compose overlays
  using temporary synthetic secrets, validates the exact pinned OpenClaw
  configuration, invokes its tool factory and checks it returns no tools, and
  confirms the reviewer refuses to run when its OAuth profile is absent.
- **Synthetic Compose and browser acceptance** installs the locked Playwright
  1.64.1 Node dependency and runs `tests/compose-acceptance.sh`. It uses the
  repository's pinned browser image and a unique Compose project, exercises
  only generated data and local SMTP capture, and cleans its resources.
- **Synthetic Codex 0.162.1 compatibility** installs that exact Codex CLI
  version and runs `tests/codex-modern-compat.py` against its local fake
  Responses API. The harness creates synthetic sessions in a temporary home;
  it does not inspect a user's Codex home or call a hosted model.

Run the checks locally from the repository root:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
docker build --network=none -t project-watchkeeper:ci -f Dockerfile .
docker build --network=none -t project-watchkeeper-reviewer:ci -f Dockerfile.reviewer .
tests/ci/check-production-compose.sh
tests/ci/check-reviewer-runtime.sh
npm ci --prefix tests/ci --ignore-scripts
PLAYWRIGHT_NODE_MODULES="$PWD/tests/ci/node_modules" \
  WATCHKEEPER_COMPOSE_PROJECT="watchkeeper-ci-local-unique" \
  tests/compose-acceptance.sh
ci_tools_dir=$(mktemp -d)
npm install --prefix "$ci_tools_dir" @openai/codex@0.162.1
python3 tests/codex-modern-compat.py --codex-binary "$ci_tools_dir/node_modules/.bin/codex"
```

The checks do not establish OAuth login or renewal, provider endpoint
compatibility, real model availability or judgment, Mac or owner-history
compatibility, production host firewall rules, external SMTP delivery, or
Electricpeak deployment behavior. Those remain separate operator and live
validation gates described in [dependency verification](dependency-verification.md)
and [portable milestone evidence](evidence-07.md). Configuring required checks
in repository protection rules is a separate repository-owner decision.
