# Separate observation from project execution

Watchkeeper observes work across accounts whose credentials may also permit
changing that work. Keep account collection separately permissioned and give
a dedicated reviewer only access to observations, its project register, and
a fixed notification destination. This sacrifices the convenience of a
general-purpose agent with direct account access so that identifying an
unfinished project cannot itself authorize resuming it or modifying its
source threads.

Credential isolation, narrow reader interfaces, and restrictions around the
entire application are implementation requirements. Prompt instructions and
a tool sandbox alone are insufficient enforcement.
