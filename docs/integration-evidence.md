# Integration evidence and limits

These references informed the design. Documented capabilities and inspected
community code are candidate integration seams, not evidence that an
end-to-end Watchkeeper installation has been tested.

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
