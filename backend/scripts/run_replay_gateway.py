"""Start a hermetic replay Gateway for the real-backend browser gate.

Derives one random disposable PostgreSQL database from the loopback development
``DATABASE_URL``, installs Schema V1, seeds the deterministic Replay model, and
owns the Gateway plus optional delayed Worker lifecycle. No model API key is
used. This is the ``playwright.real-backend.config.ts`` web server::

    DATABASE_URL=postgresql+asyncpg://.../deerflow \
      uv run python scripts/run_replay_gateway.py --port 8117

``tests/`` is put on the path so the test-only replay provider resolves;
``GATEWAY_CORS_ORIGINS`` is set for the task-local frontend. The derived
database is always named ``deerflow_test_replay_*`` and is dropped in ``finally``.

When the three ``ACT_WEAVE_KNOWLEDGE_MINIO_*`` variables are present, the
script also enables the Knowledge module for real: pgvector in the disposable
database, one disposable MinIO bucket, a deterministic mock SiliconFlow
provider on an ephemeral loopback port, a seeded model registry (one Provider
plus embedding/rerank models) pointing at that mock, and the
``/api/test-only/replay-knowledge`` control router (see
``tests/replay_knowledge.py``). Without those variables the Knowledge feature
stays off and the gate behaves exactly as before.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
import tempfile
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Literal

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "tests"))  # replay_provider + build_config_yaml live here


def _replay_worker_mode() -> Literal["immediate", "delayed"]:
    mode = os.environ.get("E2E_REPLAY_WORKER_MODE", "immediate")
    if mode not in {"immediate", "delayed"}:
        raise RuntimeError("E2E_REPLAY_WORKER_MODE must be immediate or delayed")
    return mode


def _write_readback(payload: dict[str, object]) -> None:
    raw_path = os.environ.get("E2E_REPLAY_READBACK_PATH", "").strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@contextmanager
def _uvicorn_shutdown_signal_guard():
    """Let uvicorn re-raise shutdown signals without skipping outer finally."""

    def consume_shutdown_signal(_signum, _frame) -> None:
        return None

    original = {signum: signal.signal(signum, consume_shutdown_signal) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for signum, handler in original.items():
            signal.signal(signum, handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8117)
    parser.add_argument("--fixture", default=str(_BACKEND / "tests" / "fixtures" / "replay" / "write_read_file.ultra.json"))
    parser.add_argument("--cors", default="http://localhost:3317")
    args = parser.parse_args()

    from _replay_fixture import (
        ReplayWorkerController,
        bootstrap_replay_test_database,
        build_config_yaml,
        install_replay_model_adapter,
        prepare_hermetic_skills,
        prepare_replay_runtime_catalog,
        replay_gateway_user,
        replay_test_database_from_development,
    )
    from replay_knowledge import knowledge_minio_environment_ready

    development_database_url = os.environ.get("DATABASE_URL")
    worker_mode = _replay_worker_mode()
    knowledge_enabled = knowledge_minio_environment_ready()
    readback: dict[str, object] = {
        "schema_version": 1,
        "worker_mode": worker_mode,
        "knowledge_enabled": knowledge_enabled,
        "database_created": False,
        "database_dropped": False,
        "gateway_outcome": "not_started",
    }
    database = None
    controller = None
    try:
        with replay_test_database_from_development(
            development_database_url,
        ) as database:
            readback["database_name"] = database.database_name
            readback["database_created"] = True
            os.environ["DATABASE_URL"] = database.database_url

            with tempfile.TemporaryDirectory(prefix="replay-gw-") as raw_home, contextlib.ExitStack() as knowledge_resources:
                home = Path(raw_home)
                cfg = home / "config.yaml"
                knowledge_block = ""
                knowledge_state = None
                knowledge_provider = None
                knowledge_objects = None
                if knowledge_enabled:
                    from replay_knowledge import (
                        KnowledgeReplayState,
                        ReplayKnowledgeProviderServer,
                        build_knowledge_config_block,
                        create_replay_knowledge_bucket,
                        drop_replay_knowledge_bucket,
                        list_replay_knowledge_objects,
                        prepare_pgvector_extension,
                        replay_minio_settings_from_environment,
                    )

                    prepare_pgvector_extension(database.database_url)
                    # The disposable database never outlives this process, so
                    # an ephemeral master key is enough when none is provided.
                    os.environ.setdefault(
                        "ACT_WEAVE_SECRET_KEY",
                        base64.b64encode(os.urandom(32)).decode("ascii"),
                    )
                    os.environ["ACT_WEAVE_REPLAY_KNOWLEDGE_FAST_RETRY"] = "1"
                    minio_settings = replay_minio_settings_from_environment()
                    knowledge_bucket = create_replay_knowledge_bucket(minio_settings)
                    knowledge_resources.callback(
                        drop_replay_knowledge_bucket,
                        minio_settings,
                        knowledge_bucket,
                    )
                    knowledge_state = KnowledgeReplayState()
                    knowledge_provider = ReplayKnowledgeProviderServer(knowledge_state)
                    knowledge_provider.start()
                    knowledge_resources.callback(knowledge_provider.stop)
                    knowledge_block = build_knowledge_config_block(bucket=knowledge_bucket)
                    knowledge_objects = partial(
                        list_replay_knowledge_objects,
                        minio_settings,
                        knowledge_bucket,
                    )
                    readback["knowledge"] = {
                        "bucket": knowledge_bucket,
                        "provider_port": knowledge_provider.port,
                    }
                cfg.write_text(
                    build_config_yaml(home=home, knowledge_block=knowledge_block),
                    encoding="utf-8",
                )

                # The replay process owns all prompt-affecting paths and never
                # inherits a developer's Skill tree.
                os.environ["ACT_WEAVE_HOME"] = str(home)
                os.environ["ACT_WEAVE_CONFIG_PATH"] = str(cfg)
                prepare_hermetic_skills(home)
                os.environ["ACT_WEAVE_REPLAY_FIXTURE"] = args.fixture
                os.environ.setdefault("AUTH_JWT_SECRET", "ci-replay-secret")
                os.environ["GATEWAY_CORS_ORIGINS"] = args.cors
                os.environ["PYTHONPATH"] = os.pathsep.join(
                    path
                    for path in (
                        str(_BACKEND),
                        str(_BACKEND / "tests"),
                        os.environ.get("PYTHONPATH", ""),
                    )
                    if path
                )
                install_replay_model_adapter()
                asyncio.run(bootstrap_replay_test_database(database.database_url))
                if knowledge_provider is not None:
                    from replay_knowledge import seed_replay_model_registry

                    asyncio.run(
                        seed_replay_model_registry(
                            database.database_url,
                            base_url=knowledge_provider.base_url,
                        )
                    )
                asyncio.run(prepare_replay_runtime_catalog(database.database_url))

                import uvicorn
                from replay_agent_router import (
                    build_replay_worker_router,
                )
                from replay_agent_router import (
                    router as replay_agent_router,
                )

                from app.gateway.app import app as gateway_app
                from app.gateway.deps import get_current_user_from_request

                controller = ReplayWorkerController(
                    database_url=database.database_url,
                    mode=worker_mode,
                )
                gateway_app.dependency_overrides[get_current_user_from_request] = replay_gateway_user
                gateway_app.include_router(replay_agent_router)
                if knowledge_state is not None and knowledge_objects is not None:
                    from replay_knowledge import build_replay_knowledge_router

                    gateway_app.include_router(
                        build_replay_knowledge_router(
                            knowledge_state,
                            list_objects=knowledge_objects,
                        )
                    )
                if worker_mode == "delayed":
                    gateway_app.include_router(build_replay_worker_router(controller))
                else:
                    controller.start()

                print(
                    f"[replay-gw] database=disposable worker_mode={worker_mode} knowledge={'on' if knowledge_enabled else 'off'} port={args.port}",
                    flush=True,
                )
                readback["gateway_outcome"] = "running"
                try:
                    with _uvicorn_shutdown_signal_guard():
                        uvicorn.run(
                            gateway_app,
                            host="127.0.0.1",
                            port=args.port,
                            log_level="warning",
                        )
                    readback["gateway_outcome"] = "stopped"
                finally:
                    controller.close()
                    readback["worker"] = controller.lifecycle_readback()
        readback["database_dropped"] = bool(database is not None and database.dropped)
        return 0
    except BaseException as error:
        readback["gateway_outcome"] = "failed"
        readback["failure_type"] = type(error).__name__
        if controller is not None:
            try:
                controller.close()
                readback["worker"] = controller.lifecycle_readback()
            except Exception:
                readback["worker_cleanup"] = "failed"
        readback["database_dropped"] = bool(database is not None and database.dropped)
        raise
    finally:
        _write_readback(readback)


if __name__ == "__main__":
    raise SystemExit(main())
