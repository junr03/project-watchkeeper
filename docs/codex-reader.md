# Codex reader

The first collector targets Codex app-server protocol 0.162.1. Its source
identity is an explicit installation ID configured by the operator. The
collector does not discover `~/.codex`, read the user's default Codex home, or
use an account login. It requires `--enable-codex`, an explicit source-state
directory, an intake URL, a token file, and a private cursor file.

## Supported interface

| Capability | Status | Behavior and limit |
| --- | --- | --- |
| Thread listing | Controlled Linux legacy and modern synthetic session measured | `thread/list`, ordered by `updated_at` descending, `useStateDbOnly: false`, and `modelProviders: []` (all providers) only inside a disposable selected-history mirror |
| Source kinds | Schema supported; live compatibility untested | Always supplies all inspected v2 kinds: `cli`, `vscode`, `exec`, `appServer`, `subAgent`, `subAgentReview`, `subAgentCompact`, `subAgentThreadSpawn`, `subAgentOther`, and `unknown` |
| Active history | Schema supported; live compatibility untested | `archived: false` scan |
| Archived history | Schema supported; live compatibility untested | Separate `archived: true` scan; coverage says complete only after it finishes |
| Thread pagination | Implemented; multi-page binary compatibility unverified | Continues from opaque `nextCursor`; cursor advances only after the corresponding intake response acknowledges persistence |
| Message pagination | Controlled legacy fixture and modern synthetic projection measured; modern multi-page compatibility unverified | Requests `thread/turns/list` with `itemsView: full`; turns and their available items continue by cursor. Large pages are split into intake batches below 900 KB; intermediate checkpoints stay at the page start until every chunk is acknowledged. A missing/non-full view or an empty turn result for a listed thread leaves coverage partial |
| Revisions and replay | Supported | Message ID is stable within source and thread; the revision combines the source timestamp and a content digest. Identical revisions preserve capture time |
| Current execution activity | Explicit thread status only | `active` and `idle` are recorded when Codex returns those exact status types. `notLoaded`, absent, or unrecognized status stays `unknown`; a completed turn does not imply idle |
| Deep links | Unsupported | The UI receives a plain Codex thread/message locator, never an invented URL |
| Attachments and non-text items | Partial | User and agent text items are retained. Other item types are not represented as messages; text beyond the intake limit marks thread coverage partial |
| Codex Mac compatibility | Untested | The protocol schema is pinned and fixture-tested on Linux. A controlled macOS Codex 0.162.1 session is still required before claiming live compatibility |

A controlled Linux run with Codex 0.162.1 successfully initialized, listed, read, and paginated a legacy synthetic transcript; its returned turn contained direct full-view `userMessage` and `agentMessage` items. A separate synthetic modern session created through a local fake Responses API showed that `thread/list` needs `modelProviders: []`, and that modern turn contents are projected into `thread_history_1.sqlite`. Copying only that database with SQLite's backup API into the disposable app-server home returned the full user and assistant items with timestamps. The modern path was also exercised through `CodexCollector`, actual HTTP intake, a controlled proposal, authenticated HTML state, and captured digest; two synthetic observations reached complete coverage. These checks used generated content and no account. Nonempty archived traversal, modern paginated rollout files, multi-page turn cursors, and Codex Mac compatibility remain unverified. The measurements establish a narrow Linux protocol path, not full compatibility.

The app-server request allowlist is `initialize`, `initialized`, `thread/list`,
`thread/read`, `thread/turns/list`, and `thread/items/list`. The current reader
uses the first three read methods plus initialization. It never calls
`thread/resume`, `turn/start`, source writes, file access, shell, or account
operations. Each RPC request has one 30-second monotonic deadline, including time spent
ignoring interleaved server notifications. Unexpected transport errors produce a sanitized failure category
(`authentication`, `quota`, `unsupported`, `unavailable`, or `protocol`) and
preserve the last acknowledged checkpoint.

## Isolated selected-history mirror

The app-server can update local metadata while enumerating history, so it runs
against a temporary copy, never the supplied directory itself. Provision a
dedicated mirror from only the transcript JSONL files you explicitly select.
Do not use the regular Codex home as `--source-dir`. Do not place a Codex
configuration, `state_5.sqlite`, login/session files, plugins, tokens, symlinks,
or other private material in the mirror. For modern thread history, you may
explicitly provision exactly one `thread_history_1.sqlite` snapshot beside the
marker. The reader accepts only the pinned 0.162.1 projection schema, rejects
unknown tables, views, indexes, triggers, migration versions, sidecars, and
thread IDs not present in selected JSONL transcripts, then copies that single
database into a fresh temporary `CODEX_HOME`. Never copy the source database
file and its WAL files directly; create a consistent standalone snapshot with
SQLite's backup API. The reader also removes account and proxy variables and
uses an otherwise clean temporary home. It does not select providers by
default; `modelProviders: []` explicitly means all providers in this app-server
version.

Create `.watchkeeper-reader-state` containing this exact public-safe marker:

```json
{"format":"watchkeeper-codex-reader-state-v1","credentials_excluded":true,"transcripts_only":true}
```

The marker records the operator's assertion about that directory; the
collector still verifies the directory allowlist and rejects symlinks. Keep
active transcript JSONL under `sessions/` and archived transcript JSONL under
`archived_sessions/` so the app-server can see each collection scope. If you
need modern turns, first choose those same transcript files, then back up only
the projection database into the mirror. For example, after creating the
mirror directories and copying the explicitly selected transcripts:

```sh
python3 - <<'PY'
import sqlite3
from pathlib import Path

source = Path("/private/codex/thread_history_1.sqlite")
destination = Path("/private/watchkeeper/codex-mirror/thread_history_1.sqlite")
read_only = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
try:
    with sqlite3.connect(destination) as snapshot:
        read_only.backup(snapshot)
finally:
    read_only.close()
PY
```

The source database is opened read-only. Keep the snapshot outside the
repository with restricted permissions. The reader refuses a sidecar or a
projection thread whose transcript was not selected; missing or empty modern
history results remain partial rather than being treated as complete. An
empty history projection is optional for older mirrors, but it may not support
modern turn reads.

The optional actual-binary check uses a temporary home and a local fake
Responses API; it does not read account files or contact a hosted model:

```sh
python tests/codex-modern-compat.py --codex-binary /path/to/codex
```

It emits only pass/fail and item counts, then deletes its temporary files.

A mirror can contain private transcripts, so keep it outside the repository and
restrict its permissions. The intake token is kept separately in a file with
no group or other access. The cursor file is written atomically with mode
0600 only after an HTTP intake acknowledgement. App-server output and source
content are not written to logs.

A typical invocation is:

```sh
python -m watchkeeper.collector \
  --enable-codex \
  --source-id codex-mac-primary \
  --source-dir /private/watchkeeper/codex-mirror \
  --intake-url https://watchkeeper.internal/api/observations \
  --intake-token-file /private/watchkeeper/intake-token \
  --cursor-file /private/watchkeeper/codex-cursor \
  --codex-binary /opt/codex/0.162.1/bin/codex \
  --max-threads 500 \
  --max-pages 100
```

The CLI checks the binary version before launching and sends requests through
the HTTP collector bearer token. Do not pass the token on the command line.
The local cursor file belongs to this installation and must be backed up with
its permissions intact. If the source becomes unavailable, observations stay
available in Watchkeeper and the cursor does not advance.

## Coverage meaning

Coverage separates listing, transcript completeness, archived-history access,
source freshness, and live activity. Source-level activity reports running if
any scanned thread explicitly reports active, otherwise unknown; only the
per-thread status distinguishes an explicit idle value from unknown. `partial` means the limit was reached,
a page failed, a timestamp was missing, or some available item detail was not
represented. The collector stores per-thread completeness so policy can reject
inferences that depend on incomplete history. An unknown activity signal is
not an idle signal. Current account subscription, Mac installation behavior,
real session import, and semantic review quality have not been tested by the
public fixtures.
