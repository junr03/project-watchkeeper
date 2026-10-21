# Domain Docs

## Before exploring or designing

Read the root `GLOSSARY.md` and relevant decisions in `docs/adr/`.

If they do not exist, proceed silently. Domain modeling creates
them when terms or decisions are resolved.

## Layout

This repository uses one domain context:

- `GLOSSARY.md`: domain vocabulary, without implementation details.
- `docs/adr/NNNN-short-title.md`: significant architectural decisions.

## Consumer rules

Use the glossary's canonical terms in documentation, tickets,
code, and tests.

Surface conflicts with existing ADRs explicitly. Reference the
decision and explain why it may need revisiting.
