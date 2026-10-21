# Project Watchkeeper

Project Watchkeeper identifies unfinished projects across AI conversations
and surfaces the next action that needs the user's attention.

## Language

**Project**:
An intended outcome whose work may span several threads, surfaces, providers,
and machines.
_Avoid_: Session, thread (when referring to the overall outcome)

**Thread**:
A conversation containing requests, responses, and evidence of work toward
one or more outcomes.
_Avoid_: Project (when referring only to the conversation)

**Surface**:
An application or interface where the user works with an agent, such as a
web chat, desktop application, or coding terminal.

**Source**:
A particular enabled account or installation from which Watchkeeper observes
threads.

**Candidate project**:
A possible project inferred from observed threads, before its identity or
relevance is established.

**Next action**:
A concrete step needed to advance a project's intended outcome.

**Attention item**:
A finding that an unfinished project needs a next action from the user,
supported by evidence from its threads.
_Avoid_: Idle session, process alert

**Observation**:
Evidence captured from a source at a known time; it may be incomplete or
older than the source's current state.

**Digest**:
A scheduled collection of new actionable information delivered to the user.

**Coverage**:
The portion of a source's available history that Watchkeeper has observed,
including known gaps and limits.

**Collection notice**:
A finding that Watchkeeper cannot reliably observe a source and may need
the user's help to restore visibility.
_Avoid_: Attention item (when no project action has been established)

**Snooze**:
A user decision to defer a project's attention items until a chosen time.

**Exclusion**:
A user decision to keep a conversation or source outside monitoring.

**Completion**:
The intended project outcome has been achieved; ending an agent turn does
not by itself establish completion.

**Abandonment**:
A user decision to stop pursuing a project's intended outcome.

**Deletion**:
A user decision to remove a project's stored content and exclude its known
threads from future imports until explicitly re-enabled.

**Project register**:
The user's monitored projects, their outcomes and supporting evidence,
and the decisions governing their continued monitoring.
