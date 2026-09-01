#!/usr/bin/env python3
"""Hash installed, build-provisioned parsing resources. Never downloads anything.

After installing the pinned extraction-local extra and platform libmagic:
  .venv/bin/python scripts/build_extraction_resources.py --output PATH
Review and package the generated platform entry; use the same lock in Gateway
and Worker. Other platform entries are preserved when updating an existing file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from actweave_knowledge.extraction.runtime_resources import build_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fresh = build_manifest()
    if args.output.exists():
        existing = json.loads(args.output.read_text())
        if existing.get("format_version") != 1:
            raise ValueError("unsupported resource lock format")
        existing.setdefault("platforms", {}).update(fresh["platforms"])
        existing.setdefault("native_build_probes", {}).update(fresh["native_build_probes"])
        fresh = existing
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fresh, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
