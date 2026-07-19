"""Final M7 process-boundary release gate."""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_m6_gateway_reconnect_process import (
    _create_project_thread,
    _start_gateway,
    _stop_process,
    _wait_ready,
)
from test_m6_worker_crash_recovery_postgres import (
    _barrier_events,
    _start_worker,
    _wait_for_event,
)

from app.automations.ownership import AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY

_PROCESS_TIMEOUT = 60.0
BACKEND_ROOT = Path(__file__).resolve().parents[1]
_UNRESOLVED_DYNAMIC_IMPORT = "<unresolved-dynamic-import>"
_DYNAMIC_IMPORT_LITERAL_ALLOWLIST = frozenset(
    {
        ("deerflow.agents", "__getattr__", "deerflow.agents.factory"),
        ("deerflow.agents", "__getattr__", "deerflow.agents.lead_agent"),
        ("deerflow.agents", "__getattr__", "deerflow.agents.lead_agent.prompt"),
        ("deerflow.agents", "__getattr__", "deerflow.agents.thread_state"),
        ("deerflow.runtime", "__getattr__", "deerflow.runtime.runs.worker"),
        ("deerflow.runtime.runs", "__getattr__", "deerflow.runtime.runs.worker"),
    }
)
_DYNAMIC_IMPORT_VARIABLE_ALLOWLIST = frozenset(
    {
        ("deerflow.agents.memory.storage", "get_memory_storage", "module_path"),
        ("deerflow.reflection.resolvers", "resolve_variable", "module_path"),
    }
)


def test_process_probe_records_all_authority_boundaries() -> None:
    from support import m6_process_child

    source = inspect.getsource(m6_process_child)
    assert "run_worker(handlers=None)" in source
    assert "runner=_controlled_agent_runner" in source
    assert "_CoordinatedExecutor" not in source
    assert "_CoordinatedPrivateRunHandler" not in source
    for event in (
        '"role": "worker"',
        '"event": "claim"',
        '"event": "graph_execution"',
        '"event": "stream_append"',
        '"event": "terminal_append"',
    ):
        assert event in source


def test_worker_production_constructor_has_no_test_runner_seam() -> None:
    from app.worker.app import run_worker

    assert "agent_runner" not in inspect.signature(run_worker).parameters
    source = inspect.getsource(run_worker)
    assert "RunAgentPrivateExecutor(" in source
    assert "handlers is None" in source


def _local_module_file(module: str) -> Path | None:
    if module == "app" or module.startswith("app."):
        root = BACKEND_ROOT
    elif module == "deerflow" or module.startswith("deerflow."):
        root = BACKEND_ROOT / "packages" / "harness"
    else:
        return None
    relative = Path(*module.split("."))
    for candidate in (
        root / f"{relative}.py",
        root / relative / "__init__.py",
    ):
        if candidate.is_file():
            return candidate
    return None


def _local_imports(module: str, path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    symbols: set[str] = set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    importlib_aliases = {"importlib"}
    import_module_aliases: set[str] = set()
    builtins_aliases = {"builtins"}
    builtin_import_aliases = {"__import__"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
                elif alias.name == "builtins":
                    builtins_aliases.add(alias.asname or alias.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".") if package else []
            base_parts = package_parts[: len(package_parts) - (node.level - 1)]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if base:
            modules.add(base)
        for alias in node.names:
            symbols.add(alias.name)
            if base == "importlib" and alias.name == "import_module":
                import_module_aliases.add(alias.asname or alias.name)
            elif base == "builtins" and alias.name == "__import__":
                builtin_import_aliases.add(alias.asname or alias.name)
            candidate = f"{base}.{alias.name}" if base else alias.name
            if _local_module_file(candidate) is not None:
                modules.add(candidate)

    def enclosing_scope(node: ast.AST) -> str:
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return parent.name
            if isinstance(parent, ast.Lambda):
                return "<lambda>"
            parent = parents.get(parent)
        return "<module>"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        is_dynamic_import = False
        if isinstance(function, ast.Name):
            is_dynamic_import = function.id in import_module_aliases | builtin_import_aliases
        elif isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            is_dynamic_import = (function.attr == "import_module" and function.value.id in importlib_aliases) or (function.attr == "__import__" and function.value.id in builtins_aliases)
        if not is_dynamic_import:
            continue
        argument = node.args[0] if node.args else next((keyword.value for keyword in node.keywords if keyword.arg == "name"), None)
        scope = enclosing_scope(node)
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            target = argument.value
            if (module, scope, target) not in _DYNAMIC_IMPORT_LITERAL_ALLOWLIST:
                modules.add(target)
            continue
        if isinstance(argument, ast.Name) and (module, scope, argument.id) in _DYNAMIC_IMPORT_VARIABLE_ALLOWLIST:
            continue
        symbols.add(_UNRESOLVED_DYNAMIC_IMPORT)
    return modules, symbols


def _production_import_graph(*roots: str) -> tuple[set[str], set[str]]:
    pending = list(roots)
    visited: set[str] = set()
    symbols: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        path = _local_module_file(module)
        if path is None:
            continue
        visited.add(module)
        parts = module.split(".")
        pending.extend(".".join(parts[:index]) for index in range(1, len(parts)))
        imported, imported_symbols = _local_imports(module, path)
        symbols.update(imported_symbols)
        pending.extend(imported - visited)
    return visited, symbols


def test_import_graph_mutation_finds_function_scoped_worker_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend_root = tmp_path / "backend"
    gateway = backend_root / "app" / "gateway" / "app.py"
    gateway.parent.mkdir(parents=True)
    gateway.write_text(
        "def gateway_handler():\n    from app.reliability.execution import RunAgentPrivateExecutor\n    return RunAgentPrivateExecutor\n",
        encoding="utf-8",
    )
    execution = backend_root / "app" / "reliability" / "execution.py"
    execution.parent.mkdir(parents=True)
    execution.write_text("class RunAgentPrivateExecutor: pass\n", encoding="utf-8")
    monkeypatch.setattr("test_m7_process_boundary.BACKEND_ROOT", backend_root)

    modules, symbols = _production_import_graph("app.gateway.app")

    assert "app.reliability.execution" in modules
    assert "RunAgentPrivateExecutor" in symbols


def _install_dynamic_import_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    backend_root = tmp_path / "backend"
    gateway = backend_root / "app" / "gateway" / "app.py"
    gateway.parent.mkdir(parents=True)
    gateway.write_text(source, encoding="utf-8")
    execution = backend_root / "app" / "reliability" / "execution.py"
    execution.parent.mkdir(parents=True)
    execution.write_text("class RunAgentPrivateExecutor: pass\n", encoding="utf-8")
    monkeypatch.setattr("test_m7_process_boundary.BACKEND_ROOT", backend_root)


@pytest.mark.parametrize(
    "source",
    (
        "import importlib\ndef load():\n    return importlib.import_module('app.reliability.execution')\n",
        "import importlib as loader\nclass Loader:\n    target = loader.import_module('app.reliability.execution')\n",
        "from importlib import import_module\nload = lambda: import_module('app.reliability.execution')\n",
        "from importlib import import_module as load\ndef handler():\n    return load('app.reliability.execution')\n",
        "class Loader:\n    def load(self):\n        return __import__('app.reliability.execution')\n",
        "import builtins\nload = lambda: builtins.__import__('app.reliability.execution')\n",
    ),
    ids=(
        "importlib",
        "importlib-alias-class",
        "from-import-lambda",
        "from-import-alias",
        "dunder-import",
        "builtins-dunder-import",
    ),
)
def test_import_graph_mutation_finds_constant_dynamic_import_forms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    _install_dynamic_import_mutation(monkeypatch, tmp_path, source)

    modules, _ = _production_import_graph("app.gateway.app")

    assert "app.reliability.execution" in modules


def test_import_graph_agents_lazy_allowlist_is_exact_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend_root = tmp_path / "backend"
    agents = backend_root / "packages" / "harness" / "deerflow" / "agents" / "__init__.py"
    agents.parent.mkdir(parents=True)
    agents.write_text(
        "from importlib import import_module\ndef __getattr__(name):\n    import_module('deerflow.agents.factory')\n    import_module('app.reliability.execution')\n",
        encoding="utf-8",
    )
    for relative in (
        "packages/harness/deerflow/agents/factory.py",
        "app/reliability/execution.py",
    ):
        target = backend_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr("test_m7_process_boundary.BACKEND_ROOT", backend_root)

    modules, _ = _production_import_graph("deerflow.agents")

    assert "deerflow.agents.factory" not in modules
    assert "app.reliability.execution" in modules


def test_import_graph_unknown_dynamic_module_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_dynamic_import_mutation(
        monkeypatch,
        tmp_path,
        "from importlib import import_module\nTARGET = get_runtime_target()\nload = lambda: import_module(TARGET)\n",
    )

    _, symbols = _production_import_graph("app.gateway.app")

    assert _UNRESOLVED_DYNAMIC_IMPORT in symbols


def test_gateway_and_scheduler_cannot_import_worker_graph_execution() -> None:
    modules, symbols = _production_import_graph(
        "app.gateway.app",
        "app.scheduler.app",
    )
    banned_modules = {
        "app.reliability.execution",
        "app.worker",
        "deerflow.agents.lead_agent.agent",
        "deerflow.runtime.runs.worker",
    }
    assert not {module for module in modules if any(module == banned or module.startswith(f"{banned}.") for banned in banned_modules)}
    assert (
        not {
            "PrivateRunJobHandler",
            "RunAgentPrivateExecutor",
            "make_lead_agent",
            "run_agent",
        }
        & symbols
    )
    assert _UNRESOLVED_DYNAMIC_IMPORT not in symbols

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.gateway.app, app.scheduler.app; "
                "banned=('app.worker','app.reliability.execution',"
                "'deerflow.runtime.runs.worker','deerflow.agents.lead_agent.agent'); "
                "print('\\n'.join(name for name in sys.modules "
                "if any(name == item or name.startswith(item + '.') "
                "for item in banned)))"
            ),
        ],
        cwd=BACKEND_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(BACKEND_ROOT / "packages" / "harness"),
                    os.environ.get("PYTHONPATH", ""),
                )
            ),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert probe.stdout.strip() == ""


def _scheduler_config(database_url: str) -> str:
    return f"""\
log_level: warning
models: []
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
database:
  url: {database_url}
memory:
  token_counting: char
worker:
  enabled: true
  poll_interval_seconds: 0.05
  lease_seconds: 15
  heartbeat_seconds: 4
  max_concurrent_jobs: 1
scheduler:
  enabled: true
  poll_interval_seconds: 1
  max_concurrent_runs: 1
"""


def _scheduler_environment(tmp_path: Path, database_url: str) -> dict[str, str]:
    config = tmp_path / "scheduler-config.yaml"
    config.write_text(_scheduler_config(database_url), encoding="utf-8")
    backend_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.update(
        {
            "DEER_FLOW_CONFIG_PATH": str(config),
            "DEER_FLOW_HOME": str(tmp_path / "scheduler-home"),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(backend_root), environment.get("PYTHONPATH", "")))),
            "DEER_FLOW_AUDIT_ACTIVE_KEY_ID": "test-audit-v1",
            "DEER_FLOW_AUDIT_KEYRING_JSON": ('{"test-audit-v1":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}'),
        }
    )
    return environment


def _start_scheduler(
    tmp_path: Path,
    database_url: str,
) -> tuple[subprocess.Popen[bytes], object]:
    log = (tmp_path / "scheduler.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "app.scheduler.app"],
        cwd=Path(__file__).resolve().parents[1],
        env=_scheduler_environment(tmp_path, database_url),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return process, log


async def _wait_scheduler_owned(
    process: subprocess.Popen[bytes],
    database_url: str,
) -> int:
    engine = create_async_engine(database_url)
    deadline = time.monotonic() + _PROCESS_TIMEOUT
    high = (AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY >> 32) & 0xFFFF_FFFF
    low = AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY & 0xFFFF_FFFF
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Scheduler pid={process.pid} exited with {process.returncode}")
            async with engine.connect() as connection:
                backend_pid = await connection.scalar(
                    text(
                        """SELECT pid FROM pg_locks
                        WHERE locktype='advisory' AND granted
                          AND classid=:classid AND objid=:objid AND objsubid=1"""
                    ),
                    {"classid": high, "objid": low},
                )
            if backend_pid is not None:
                return int(backend_pid)
            await asyncio.sleep(0.05)
    finally:
        await engine.dispose()
    raise AssertionError(f"Scheduler pid={process.pid} did not acquire ownership")


async def _job_snapshot(database_url: str, run_id: str) -> tuple[object, ...]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT j.id,j.status,j.lease_owner_id,w.id,w.draining,r.status
                        FROM jobs j
                        JOIN runs r ON r.job_id=j.id
                        LEFT JOIN worker_nodes w ON w.id=j.lease_owner_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        return tuple(row)
    finally:
        await engine.dispose()


async def _stream_rows(
    database_url: str,
    *,
    project_id: str,
    owner_user_id: str,
    thread_id: str,
    run_id: str,
) -> tuple[tuple[int, str, str, str, str], ...]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    """SELECT seq,event_type,project_id::text,owner_user_id::text,thread_id
                    FROM run_events
                    WHERE project_id=:project_id AND owner_user_id=:owner_user_id
                      AND thread_id=:thread_id AND run_id=:run_id AND category='stream'
                    ORDER BY seq"""
                ),
                {
                    "project_id": project_id,
                    "owner_user_id": owner_user_id,
                    "thread_id": thread_id,
                    "run_id": run_id,
                },
            )
        return tuple((int(seq), str(event), str(project), str(owner), str(thread)) for seq, event, project, owner, thread in rows)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_process_roles_keep_graph_worker_only_and_reconnect_isolated(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    """Run Gateway, Scheduler and Worker against one disposable M7 database."""

    scheduler = gateway = replacement = worker = None
    scheduler_log = gateway_log = replacement_log = worker_log = None
    barrier = tmp_path / "m7-process-events.jsonl"
    release = tmp_path / "m7-process-release"
    roles: list[dict[str, object]] = []
    try:
        scheduler, scheduler_log = _start_scheduler(
            tmp_path,
            migrated_postgres_database_url,
        )
        scheduler_backend_pid = await _wait_scheduler_owned(
            scheduler,
            migrated_postgres_database_url,
        )
        roles.append(
            {
                "event": "ownership",
                "role": "scheduler",
                "pid": scheduler.pid,
                "backend_pid": scheduler_backend_pid,
            }
        )

        gateway, gateway_log, gateway_url = _start_gateway(
            tmp_path,
            migrated_postgres_database_url,
            "m7-gateway-first",
        )
        await _wait_ready(gateway, gateway_url)
        async with httpx.AsyncClient(
            base_url=gateway_url,
            timeout=15,
            trust_env=False,
        ) as owner:
            owner_user_id, project_id, thread_id = await _create_project_thread(owner)
            cookies = httpx.Cookies(owner.cookies)
            csrf = owner.cookies.get("csrf_token")
            assert csrf
            admitted = await owner.post(
                f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs",
                headers={"X-CSRF-Token": csrf},
                json={
                    "input": {"messages": [{"role": "user", "content": "M7 process boundary"}]},
                    "config": {"configurable": {"thread_id": thread_id}},
                },
            )
        assert admitted.status_code == 200, admitted.text
        run_id = admitted.json()["run_id"]
        roles.append({"event": "admission", "role": "gateway", "pid": gateway.pid})

        worker, worker_log = _start_worker(
            tmp_path,
            migrated_postgres_database_url,
            barrier,
            release,
            "m7-worker",
        )
        claim = await _wait_for_event(barrier, "claim", process=worker, pid=worker.pid)
        graph = await _wait_for_event(
            barrier,
            "graph_execution",
            process=worker,
            pid=worker.pid,
        )
        assert claim["role"] == graph["role"] == "worker"
        assert claim["job_id"] == graph["job_id"]

        job_id, job_status, lease_owner_id, worker_id, draining, run_status = await _job_snapshot(migrated_postgres_database_url, run_id)
        assert str(job_id) == claim["job_id"]
        assert (job_status, lease_owner_id, worker_id, draining, run_status) == (
            "running",
            worker_id,
            worker_id,
            False,
            "running",
        )

        release.touch()
        await _wait_for_event(
            barrier,
            "terminal_append",
            process=worker,
            pid=worker.pid,
        )
        await _wait_for_event(barrier, "settled", process=worker, pid=worker.pid)
        roles.extend(_barrier_events(barrier))

        rows = await _stream_rows(
            migrated_postgres_database_url,
            project_id=project_id,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            run_id=run_id,
        )
        assert [row[1] for row in rows] == ["updates", "stream.end"]
        assert all(row[2:] == (project_id, owner_user_id, thread_id) for row in rows)
        first_event_id, terminal_event_id = str(rows[0][0]), str(rows[1][0])

        path = f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/stream"
        async with httpx.AsyncClient(
            base_url=gateway_url,
            cookies=cookies,
            timeout=15,
            trust_env=False,
        ) as first_owner:
            initial = await first_owner.get(path)
        assert initial.status_code == 200, initial.text
        assert [line.removeprefix("id: ") for line in initial.text.splitlines() if line.startswith("id: ")] == [first_event_id, terminal_event_id]

        _stop_process(gateway, gateway_log)
        gateway = gateway_log = None
        replacement, replacement_log, replacement_url = _start_gateway(
            tmp_path,
            migrated_postgres_database_url,
            "m7-gateway-second",
        )
        await _wait_ready(replacement, replacement_url)
        async with httpx.AsyncClient(
            base_url=replacement_url,
            cookies=cookies,
            timeout=15,
            trust_env=False,
        ) as reconnected:
            replay = await reconnected.get(
                path,
                headers={"Last-Event-ID": first_event_id},
            )
        assert replay.status_code == 200, replay.text
        assert f"id: {first_event_id}\n" not in replay.text
        assert replay.text.count(f"id: {terminal_event_id}\n") == 1

        async with httpx.AsyncClient(
            base_url=replacement_url,
            timeout=15,
            trust_env=False,
        ) as foreign:
            registered = await foreign.post(
                "/api/v1/auth/register",
                json={
                    "email": f"m7-foreign-{os.getpid()}@example.com",
                    "password": "very-strong-password-123",
                },
            )
            assert registered.status_code == 201
            denied = await foreign.get(path)
        assert denied.status_code == 404

        graph_pids = {int(item["pid"]) for item in roles if item.get("event") == "graph_execution"}
        assert graph_pids == {worker.pid}
        assert scheduler.pid not in graph_pids
        assert replacement.pid not in graph_pids
        assert all(item.get("role") == "worker" for item in roles if item.get("event") in {"claim", "graph_execution", "stream_append", "terminal_append"})
    finally:
        for process, log in (
            (worker, worker_log),
            (gateway, gateway_log),
            (replacement, replacement_log),
            (scheduler, scheduler_log),
        ):
            if process is not None and log is not None:
                _stop_process(process, log)
