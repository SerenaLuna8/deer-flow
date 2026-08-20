# Domain Docs

This repository uses a single-context domain-documentation layout.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If either location doesn't exist, **proceed silently**. Don't flag its absence
or suggest creating it upfront. The `/domain-modeling` skill, reached via
`/grill-with-docs` and `/improve-codebase-architecture`, creates domain docs
lazily when terms or decisions are actually resolved.

## File structure

```text
/
├── CONTEXT.md
├── docs/adr/
├── backend/
└── frontend/
```

## Use the glossary's vocabulary

When your output names a domain concept—in an issue title, refactor proposal,
hypothesis, or test name—use the term defined in `CONTEXT.md`. Don't drift to
synonyms the glossary explicitly avoids.

If the concept isn't in the glossary yet, reconsider whether you're inventing
language the project doesn't use or note the genuine gap for `/domain-modeling`.

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than
silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
