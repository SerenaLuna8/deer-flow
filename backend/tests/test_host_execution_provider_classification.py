"""Fail-closed provider classification for host bash execution."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from deerflow.sandbox import tools as sandbox_tools
from deerflow.sandbox.local import LocalSandboxProvider
from deerflow.sandbox.sandbox_provider import SandboxProvider
from deerflow.sandbox.security import (
    HostBashExecutionMode,
    resolve_host_bash_execution_mode,
    uses_local_sandbox_provider_use,
)


def _config(
    use: str,
    *,
    approval_mode: str = "disabled",
    allow_host_bash: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        sandbox=SimpleNamespace(
            use=use,
            allow_host_bash=allow_host_bash,
            host_execution_approval=SimpleNamespace(mode=approval_mode),
        ),
    )


def _runtime(config: object, sandbox_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        context={"app_config": config},
        state={"sandbox": {"sandbox_id": sandbox_id}},
    )


def test_local_provider_reexports_and_subclasses_keep_local_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "test_custom_local_provider_classification"
    module = ModuleType(module_name)

    class CustomLocalProvider(LocalSandboxProvider):
        pass

    module.ReexportedLocalProvider = LocalSandboxProvider
    module.CustomLocalProvider = CustomLocalProvider
    monkeypatch.setitem(sys.modules, module_name, module)

    for class_name in ("ReexportedLocalProvider", "CustomLocalProvider"):
        provider_use = f"{module_name}:{class_name}"
        assert uses_local_sandbox_provider_use(provider_use)
        config = _config(
            provider_use,
            approval_mode="approval_required",
        )
        assert resolve_host_bash_execution_mode(config) is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED


def test_unknown_provider_fails_closed_instead_of_assuming_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "test_unknown_provider_classification"
    module = ModuleType(module_name)

    class UnknownProvider(SandboxProvider):
        pass

    module.UnknownProvider = UnknownProvider
    monkeypatch.setitem(sys.modules, module_name, module)

    assert (
        resolve_host_bash_execution_mode(
            _config(f"{module_name}:UnknownProvider"),
        )
        is HostBashExecutionMode.LOCAL_DISABLED
    )


def test_custom_isolated_provider_subclass_does_not_inherit_direct_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.aio_sandbox import AioSandboxProvider

    module_name = "test_custom_isolated_provider_classification"
    module = ModuleType(module_name)

    class CustomAioProvider(AioSandboxProvider):
        pass

    module.ReexportedAioProvider = AioSandboxProvider
    module.CustomAioProvider = CustomAioProvider
    monkeypatch.setitem(sys.modules, module_name, module)

    assert (
        resolve_host_bash_execution_mode(
            _config(f"{module_name}:ReexportedAioProvider"),
        )
        is HostBashExecutionMode.ISOLATED_DIRECT
    )
    assert (
        resolve_host_bash_execution_mode(
            _config(f"{module_name}:CustomAioProvider"),
        )
        is HostBashExecutionMode.LOCAL_DISABLED
    )
    assert (
        resolve_host_bash_execution_mode(
            _config("missing.provider:UnknownProvider"),
        )
        is HostBashExecutionMode.LOCAL_DISABLED
    )


@pytest.mark.parametrize(
    "provider_use",
    [
        "deerflow.community.aio_sandbox:AioSandboxProvider",
        "deerflow.community.boxlite:BoxliteProvider",
        "deerflow.community.e2b_sandbox:E2BSandboxProvider",
    ],
)
def test_builtin_isolated_providers_remain_direct(provider_use: str) -> None:
    assert resolve_host_bash_execution_mode(_config(provider_use)) is HostBashExecutionMode.ISOLATED_DIRECT


@pytest.mark.asyncio
async def test_actual_local_runtime_overrides_isolated_config_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(
        "deerflow.community.aio_sandbox:AioSandboxProvider",
        approval_mode="approval_required",
    )
    runtime = _runtime(config, "local-run:user:thread:run")
    calls: list[str] = []

    async def approval_required(*_args, **_kwargs):
        calls.append("approval")
        return "approval"

    async def direct(*_args, **_kwargs):
        calls.append("direct")
        return "direct"

    monkeypatch.setattr(
        sandbox_tools,
        "_approval_required_bash",
        approval_required,
    )
    monkeypatch.setattr(
        sandbox_tools,
        "_run_sync_tool_after_async_sandbox_init",
        direct,
    )

    result = await sandbox_tools._bash_tool_async(
        runtime,
        "verify provider classification",
        "python script.py",
    )

    assert result == "approval"
    assert calls == ["approval"]


@pytest.mark.asyncio
async def test_actual_local_runtime_with_disabled_policy_never_dispatches_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config("deerflow.community.aio_sandbox:AioSandboxProvider")
    runtime = _runtime(config, "local-run:user:thread:run")
    calls: list[str] = []

    async def approval_required(*_args, **_kwargs):
        calls.append("approval")
        return "approval"

    async def direct(*_args, **_kwargs):
        calls.append("direct")
        return "direct"

    monkeypatch.setattr(
        sandbox_tools,
        "_approval_required_bash",
        approval_required,
    )
    monkeypatch.setattr(
        sandbox_tools,
        "_run_sync_tool_after_async_sandbox_init",
        direct,
    )

    result = await sandbox_tools._bash_tool_async(
        runtime,
        "verify provider classification",
        "python script.py",
    )

    assert result.startswith("Error: Host bash execution is disabled")
    assert calls == []


@pytest.mark.asyncio
async def test_actual_isolated_runtime_keeps_direct_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config("deerflow.community.aio_sandbox:AioSandboxProvider")
    runtime = _runtime(config, "aio-sandbox-1")
    calls: list[str] = []

    async def approval_required(*_args, **_kwargs):
        calls.append("approval")
        return "approval"

    async def direct(*_args, **_kwargs):
        calls.append("direct")
        return "direct"

    monkeypatch.setattr(
        sandbox_tools,
        "_approval_required_bash",
        approval_required,
    )
    monkeypatch.setattr(
        sandbox_tools,
        "_run_sync_tool_after_async_sandbox_init",
        direct,
    )

    result = await sandbox_tools._bash_tool_async(
        runtime,
        "verify provider classification",
        "python script.py",
    )

    assert result == "direct"
    assert calls == ["direct"]
