"""Test-only Worker entry point with the deterministic replay adapter."""

from __future__ import annotations

from _replay_fixture import install_replay_model_adapter


def main() -> None:
    install_replay_model_adapter()

    from app.worker.app import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()
