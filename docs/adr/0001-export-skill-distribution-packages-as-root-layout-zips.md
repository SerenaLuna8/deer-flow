---
status: accepted
---

# Export Skill distribution packages as root-layout ZIPs

ActWeave exports one selected, persisted Skill Version as a deterministic `<slug>-v<version_number>.zip` with `SKILL.md` at the archive root. The export removes root `evals/`, every `node_modules/` and `__pycache__/` directory, every `.DS_Store` file, and every `*.pyc` file; a nested directory such as `examples/evals/` remains distributable. It also omits version history, governance state, Credential mappings, secrets, and other platform-private data. A regular ZIP lets users recognize and inspect the package with general-purpose archive tools, while the root layout exposes `SKILL.md` immediately without requiring knowledge of ActWeave's `.skill` extension or wrapper convention. Every persisted Project Skill Version, including Drafts and historical releases, is eligible; System Skill Versions must be published and not revoked.

## Considered Options

- Reuse the existing `.skill` extension and single wrapper directory emitted by `skill-creator`.
- Export an ActWeave-specific backup containing history and governed configuration.
