# 03: Assess unfinished outcomes through restricted OpenClaw review

**What to build:** Use a dedicated subscription-authenticated OpenClaw reviewer to assess Codex observations. Evidence-supported attention items reach the page and digest; uncertain project matches and completion claims remain proposals. Enforce the monitoring-only boundary around the whole reviewer application.

**Blocked by:** [01: Demonstrate a Codex project becoming an attention item](01-codex-attention-demo.md).

**Business decisions and activation gates:** No unresolved business decisions. A configured test subscription login gates actual model evaluation; use synthetic evidence and controlled transport failures for deterministic checks. Do not require model API keys or connect owner source accounts. Containment must be verified before enabling the real reviewer.

**Demo:** Review synthetic cases where an agent finished normally but the outcome remains unfinished, alongside active-work, external-dependency, and ambiguous-completion cases. See only supported eligible attention items in the captured digest, uncertainty on the page, and rejected attempts to execute source instructions or change the notification recipient.

**Status:** ready-for-agent

**Work breakdown:**

| Work ID | Deliverable | Depends on | Owner | State | Evidence |
| --- | --- | --- | --- | --- | --- |
| W1 | Connect bounded OpenClaw assessment to the existing proposal and project interfaces within a constrained runtime | None | Unassigned | Planned | Synthetic observations produce validated proposals; denied tools, source access, and network paths are demonstrated |
| W2 | Apply attention eligibility and uncertainty rules to page and digest outcomes | W1 | Unassigned | Planned | Controlled cases distinguish explicit requests, three-day inferred inactivity, active work, dependencies, and unsupported completion |
| W3 | Evaluate actual reviewer judgment and hostile-input behavior independently of deterministic policy tests | W1, W2 | Unassigned | Planned | Recorded representative model runs and containment checks include false positives and unsupported claims; failures remain visible |

## Acceptance criteria

- [ ] Reviewer inference uses a supported account/subscription login; source-account credentials are not supplied to it.
- [ ] The reviewer can only read permitted observations and submit proposals; shell/browser execution, source replies, delegation, self-reconfiguration, and arbitrary recipient selection are unavailable.
- [ ] Restrictions cover Gateway-side plugins/MCP as well as tool execution; no container-runtime socket or host-wide mount is exposed.
- [ ] An explicit unanswered input request is eligible for the next digest; inferred attention requires the configurable three-day default and a concrete evidence-supported user action.
- [ ] Active agent work, known external dependencies, and source outages do not become unsupported claims that the project is waiting on the user.
- [ ] Grouping requires strong evidence; uncertain grouping/completion is visible as a proposal rather than emailed as fact.
- [ ] Invalid or unsupported proposals do not overwrite authoritative user decisions.
- [ ] Hostile instructions in evidence cannot expand permissions, expose credentials, or alter recipients.
- [ ] Actual model evaluation is reported separately from controlled-response tests; unexecuted account-dependent checks are not marked passing.

## References

- [Feature specification](../spec.md)
- [Approved milestone and execution rules](../README.md)
