"""Knowledge parsing dependencies stay installed at every sync boundary."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_make_install_targets_keep_the_required_workspace_extra() -> None:
    assert "cd backend && uv sync --all-packages --extra extraction-local" in _text("Makefile")
    assert "\ninstall:\n\tuv sync --all-packages --extra extraction-local\n" in _text("backend/Makefile")


def test_local_serve_sync_keeps_required_and_detected_optional_extras() -> None:
    script = _text("scripts/serve.sh")
    assert 'REQUIRED_UV_EXTRAS_FLAGS="--extra extraction-local"' in script
    assert "uv sync --quiet --all-packages $REQUIRED_UV_EXTRAS_FLAGS $UV_EXTRAS_FLAGS" in script
