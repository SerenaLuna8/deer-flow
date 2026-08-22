"""Start a hermetic replay gateway for the core full-stack E2E.

Builds an ephemeral process config, seeds the disposable PostgreSQL model and
runtime-policy catalogs for ``ReplayChatModel``, then runs uvicorn — no model
API key, deterministic. Used as a
Playwright ``webServer`` (see ``frontend/playwright.real-backend.config.ts``) and
runnable standalone for debugging::

    DATABASE_URL=postgresql+asyncpg://.../deerflow_test_replay_local \
      uv run python scripts/run_replay_gateway.py --port 8011

``tests/`` is put on the path so the test-only replay provider resolves;
``GATEWAY_CORS_ORIGINS`` is set so the frontend on :3000 can talk to it.
Every catalog or schema write is rejected unless the target database name has
the disposable ``deerflow_test_`` prefix.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "tests"))  # replay_provider + build_config_yaml live here


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--fixture", default=str(_BACKEND / "tests" / "fixtures" / "replay" / "write_read_file.ultra.json"))
    parser.add_argument("--cors", default="http://localhost:3000")
    args = parser.parse_args()

    from _replay_fixture import (
        bootstrap_replay_test_database,
        build_config_yaml,
        install_replay_model_adapter,
        prepare_hermetic_skills,
        prepare_replay_runtime_catalog,
        replay_gateway_user,
        replay_worker,
    )

    home = Path(tempfile.mkdtemp(prefix="replay-gw-"))
    cfg = home / "config.yaml"
    cfg.write_text(build_config_yaml(home=home), encoding="utf-8")

    # Override (not setdefault): the replay gateway must be hermetic, so an outer
    # ACT_WEAVE_HOME can't leak in and shift prompt-affecting paths/skills.
    os.environ["ACT_WEAVE_HOME"] = str(home)
    os.environ["ACT_WEAVE_CONFIG_PATH"] = str(cfg)
    prepare_hermetic_skills(home)
    os.environ["ACT_WEAVE_REPLAY_FIXTURE"] = args.fixture
    os.environ.setdefault("AUTH_JWT_SECRET", "ci-replay-secret")
    os.environ["GATEWAY_CORS_ORIGINS"] = args.cors
    # Child / dynamic imports (resolve_class) search PYTHONPATH too.
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (str(_BACKEND), str(_BACKEND / "tests"), os.environ.get("PYTHONPATH", "")) if p)
    install_replay_model_adapter()
    if os.environ.get("ACT_WEAVE_REPLAY_BOOTSTRAP_SCHEMA") == "1":
        asyncio.run(bootstrap_replay_test_database())
    asyncio.run(prepare_replay_runtime_catalog())

    import uvicorn
    from replay_agent_router import router as replay_agent_router

    from app.gateway.app import app as gateway_app
    from app.gateway.deps import get_current_user_from_request

    gateway_app.dependency_overrides[get_current_user_from_request] = replay_gateway_user
    gateway_app.include_router(replay_agent_router)

    print(f"[replay-gw] config={cfg} fixture={args.fixture} cors={args.cors} port={args.port}", flush=True)
    with replay_worker():
        uvicorn.run(gateway_app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
