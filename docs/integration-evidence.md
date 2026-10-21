# Integration evidence and limits

The references below informed the design. The implementation has separate
[dependency verification](dependency-verification.md), synthetic slice evidence
([01](evidence-01.md), [02](evidence-02.md), [03](evidence-03.md),
[04](evidence-04.md), [05](evidence-05.md), [06](evidence-06.md), [07](evidence-07.md)), and
remaining live validation gates. Reference documentation alone is not evidence
that a reader or subscription-authenticated reviewer works end to end.

The first slice uses Codex threads only. OpenCode, Claude, ChatGPT web, and
Meta Muse are references for later source expansion, not dependencies of the
first demonstration.

| Component | Evidence | Implementation limit |
| --- | --- | --- |
| OpenClaw review | [Heartbeat](https://docs.openclaw.ai/gateway/heartbeat), [subscription OAuth](https://docs.openclaw.ai/concepts/oauth) | Validate bounded review and login renewal against a pinned release |
| OpenClaw containment | [Tool permissions](https://docs.openclaw.ai/gateway/security/tool-permissions), [sandbox capability matrix](https://docs.openclaw.ai/gateway/sandboxing/supported-capability-matrix) | Gateway/plugins/MCP need a whole-application boundary; a tool sandbox is insufficient |
| OpenCode | [Server interfaces](https://opencode.ai/docs/server/) | Match the actual running instance/version; validate history pagination and current activity |
| Codex | [App Server](https://learn.chatgpt.com/docs/app-server) | Validate source kinds, archives, pagination, and Mac session coverage; ordinary ChatGPT web history is separate |
| Claude Code | [Session browser cookbook](https://platform.claude.com/cookbook/claude-agent-sdk-05-building-a-session-browser) | Local history access depends on device availability and retained files |
| Claude web/Code/Cowork | [Claude Explorer](https://github.com/rpeck/claude-explorer) | Unofficial reader; login expiry, backend changes, and message truncation need handling |
| ChatGPT web | [gpt-history-mcp](https://github.com/YunosukeYoshino/gpt-history-mcp) | Unofficial desktop-catalog/backend bridge; enumeration and sync coverage need validation |
| Meta Muse | [Muse MCP](https://github.com/kleprevost/muse-mcp) | Unofficial browser-session bridge; read-only exposure, pagination, and session expiry need validation |

MCP is a tool interface, not a guarantee of account authorization, read-only
behavior, or complete history access. Only explicitly enabled source readers
may access private data.

This list does not require using every named community project. Prefer an
existing supported reader when it meets the source contract; report missing
capabilities instead of silently substituting partial coverage.

## Live validation still required

- On an explicitly authorized Mac installation, verify the pinned Codex reader
  against the owner's actual source kinds, history projection, archives and
  sleep/wake collection. Linux synthetic compatibility does not prove this.
- With a separately authorized dedicated subscription OAuth profile, verify
  inference, renewal, expiry/quota recovery and the representative synthetic
  reviewer judgment cases, including false positives. No model API key fallback
  is permitted. Controlled reviewer outputs do not prove model judgment.
- The deployment owner supplies private HTTPS ingress, authentication secrets,
  state/backup policy and a fixed mail recipient/transport, then explicitly
  activates them. Production Envoy and Electricpeak remain separate work.

These gates remain unverified; no private account or external notification was
used to satisfy the synthetic milestone checks.
