---
status: accepted
---

# Keep System Skill v1 immutable

A System Skill is installed once under a stable asset and version identity, and its v1 definition is immutable. Reinstalling the exact checksum is idempotent, while any changed checksum is rejected rather than replacing code that existing Projects may already trust with their Configuration Secrets.

Changing a System Skill requires a new System Skill identity and explicit Project adoption. Project bindings and secret values never transfer automatically to that new identity. This sacrifices in-place System Skill maintenance in exchange for a simple authorization boundary: Project approval of one System Skill definition never silently authorizes different platform code to receive the same secrets.

This decision supersedes the System Skill portion of System Asset Upgrade in ADR-0002; System Agent lifecycle remains unchanged.
