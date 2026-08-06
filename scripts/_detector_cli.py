"""Shared bootstrap for the detector CLI shims in this directory."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from pathlib import Path

SCRIPTS_PATH = Path(__file__).resolve().parent
DETECTOR_PACKAGE_PATH = SCRIPTS_PATH / "detectors"


def run_detector(module_name: str, argv: Sequence[str] | None = None) -> int:
    """Import a `detectors.*` module and run its `main(argv)`."""
    if not DETECTOR_PACKAGE_PATH.is_dir():
        raise RuntimeError(f"detector package not found: {DETECTOR_PACKAGE_PATH}")
    if str(SCRIPTS_PATH) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_PATH))
    module = importlib.import_module(module_name)
    return module.main(argv)
