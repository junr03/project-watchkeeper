# Issue tracker: Local Markdown

Issues and specs live as Markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- Spec: `.scratch/<feature-slug>/spec.md`.
- Implementation tickets:
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`.
- Number tickets from `01`, with one file per ticket.
- Record triage as a `Status:` line near the top, using the
  values in `triage-labels.md`.
- Append discussion under `## Comments`.
- Reference tickets by repository-relative path; numbers alone
  are scoped to their feature directory.

## Publishing and reading

When a skill says "publish to the issue tracker", create the
appropriate local Markdown file.

When a skill says "fetch the relevant ticket", read its file.

GitHub pull requests remain the code and documentation review
surface. Creating a ticket does not create a GitHub issue.

Commit only public-safe tracker content. Keep private source
material and deployment details outside the repository.
