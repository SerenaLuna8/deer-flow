from __future__ import annotations

import asyncio
from types import SimpleNamespace

from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.mcp.definition import NetworkMcpEndpointPolicy


def test_worker_executor_injects_operator_mcp_security_policy(
    monkeypatch,
) -> None:
    from app.reliability import execution as execution_module

    captured: dict[str, object] = {}

    class CapturingAssetRuntime:
        def __init__(self, _session_factory, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        execution_module,
        "PrivateAssetRuntime",
        CapturingAssetRuntime,
    )
    endpoint = "https://10.8.0.42/tools"
    app_config = SimpleNamespace(
        mcp_security=McpSecurityConfig(
            project_remote_allowed_networks=("10.0.0.0/8",),
            require_egress_proxy=True,
            egress_proxy_url="http://egress-proxy.internal:3128",
            discovery_timeout_seconds=47,
            tool_call_timeout_seconds=23,
        )
    )

    execution_module.RunAgentPrivateExecutor(
        object(),  # type: ignore[arg-type]
        app_config=app_config,
        bridge=object(),
        project_checkpointer=object(),  # type: ignore[arg-type]
        store=object(),
        event_store=object(),
        agent_factory=object(),
    )

    endpoint_policy = captured["endpoint_policy"]
    assert endpoint_policy.allows(endpoint) is True  # type: ignore[union-attr]
    assert endpoint_policy.allows("https://192.168.1.42/tools") is False  # type: ignore[union-attr]
    assert captured["http_client_factory"] is not None
    assert captured["discovery_timeout_seconds"] == 47
    assert captured["tool_call_timeout_seconds"] == 23
    client = captured["http_client_factory"](None, None, None)  # type: ignore[operator]
    try:
        assert client.timeout.read == 47
    finally:
        asyncio.run(client.aclose())


def test_run_admission_and_scheduler_share_operator_endpoint_policy() -> None:
    from app.automations.dispatcher import AutomationDispatcher
    from app.private_work.run_admission import PrivateRunAdmissionService
    from app.reliability.execution import PrivateRunJobHandler

    policy = NetworkMcpEndpointPolicy(("10.0.0.0/8",))

    admission = PrivateRunAdmissionService(  # type: ignore[arg-type]
        object(),
        endpoint_policy=policy,
    )
    dispatcher = AutomationDispatcher(  # type: ignore[arg-type]
        object(),
        endpoint_policy=policy,
    )
    worker_handler = PrivateRunJobHandler(  # type: ignore[arg-type]
        object(),
        executor=object(),  # type: ignore[arg-type]
        endpoint_policy=policy,
    )

    assert admission._snapshots._endpoint_policy is policy
    assert dispatcher._snapshots._endpoint_policy is policy
    assert worker_handler._snapshots._endpoint_policy is policy


def test_agent_chat_runtime_and_controls_share_operator_endpoint_policy() -> None:
    from app.gateway.routers.project_input_polish import ProjectInputPolishService
    from app.private_work.asset_runtime import PrivateAssetRuntime
    from app.private_work.chat_controls import ProjectChatControlService

    policy = NetworkMcpEndpointPolicy(("10.0.0.0/8",))

    asset_runtime = PrivateAssetRuntime(  # type: ignore[arg-type]
        object(),
        endpoint_policy=policy,
    )
    chat_controls = ProjectChatControlService(  # type: ignore[arg-type]
        object(),
        object(),
        object(),
        object(),
        endpoint_policy=policy,
    )
    input_polish = ProjectInputPolishService(  # type: ignore[arg-type]
        object(),
        endpoint_policy=policy,
    )

    assert asset_runtime._snapshots._endpoint_policy is policy
    assert chat_controls._snapshots._endpoint_policy is policy
    assert input_polish._snapshots._endpoint_policy is policy


def test_gateway_mcp_authoring_reuses_startup_endpoint_policy(
    monkeypatch,
) -> None:
    from app.gateway.routers import project_assets

    policy = NetworkMcpEndpointPolicy(("10.0.0.0/8",))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                mcp_endpoint_policy=policy,
                shared_asset_audit_sink=object(),
            )
        )
    )
    monkeypatch.setattr(project_assets, "_factory", lambda: object())

    service = project_assets.get_mcp_service(request)

    assert service._endpoint_policy is policy
