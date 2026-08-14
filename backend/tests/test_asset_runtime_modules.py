from __future__ import annotations


def test_private_agent_runtime_compatibility_identity() -> None:
    from app.private_work import private_agent_runtime
    from app.private_work.asset_runtime import (
        PrivateAgentRuntime as CompatibilityPrivateAgentRuntime,
    )
    from app.private_work.private_agent_runtime import (
        PrivateAgentRuntime as DirectPrivateAgentRuntime,
    )

    assert CompatibilityPrivateAgentRuntime is DirectPrivateAgentRuntime
    assert DirectPrivateAgentRuntime.__module__ == "app.private_work.private_agent_runtime"
    assert private_agent_runtime.logger.name == "app.private_work.asset_runtime"
