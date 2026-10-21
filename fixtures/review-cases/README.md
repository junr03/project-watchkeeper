# Synthetic reviewer evaluation cases

These fabricated cases cover normal agent completion with unfinished work,
current activity, external waits, old progress, uncertain completion, uncertain
cross-thread grouping, and hostile transcript text. They contain no real
conversation history or account data. The expectations are a human scoring
rubric for an optional live review; the fixture itself is not a model result.

Run live evaluation only in a separately authorized environment with a
dedicated subscription login and the restricted container boundaries from
`docs/reviewer.md`. Save content-minimized, redacted result records outside the
public repository. Record each expected/actual status and action, unsupported
claims, missing evidence, false positives, model/runtime version, and failure
classification. A run with no model output or an error does not count as a
successful empty review.
