# Project Watchkeeper

A self-hosted monitor that tracks unfinished projects across AI
conversations and surfaces the next action that needs your attention.

## Repository scope
Implement and document Project Watchkeeper in this repository. Provide
portable container artifacts and a deployment handoff contract for
Electricpeak. Changes to Electricpeak's repository or running services
belong to separate deployment work.

## Agent skills

### Issue tracker
Track issues and specs in local Markdown under `.scratch/`.
Before creating, reading, or updating tickets, read
`docs/agents/issue-tracker.md`.

### Triage labels
Use the five canonical triage roles as issue metadata.
Before triaging, read `docs/agents/triage-labels.md`.

### Domain docs
Use a single-context layout: root `GLOSSARY.md` and `docs/adr/`.
Before exploring or designing, read `docs/agents/domain.md`.

## Public repository
Keep committed documentation, examples, and fixtures public-safe.
Keep credentials, conversation history, and private deployment
configuration outside version control.
