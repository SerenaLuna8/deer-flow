from importlib import import_module

from .features import Next, Prev, RuntimeFeatures

__all__ = [
    "create_deerflow_agent",
    "RuntimeFeatures",
    "Next",
    "Prev",
    "make_lead_agent",
    "SandboxState",
    "ThreadState",
]


def __getattr__(name: str):
    if name == "create_deerflow_agent":
        create_deerflow_agent = import_module("deerflow.agents.factory").create_deerflow_agent

        globals()[name] = create_deerflow_agent
        return create_deerflow_agent
    if name == "make_lead_agent":
        make_lead_agent = import_module("deerflow.agents.lead_agent").make_lead_agent
        prime_enabled_skills_cache = import_module("deerflow.agents.lead_agent.prompt").prime_enabled_skills_cache

        # LangGraph resolves deerflow.agents:make_lead_agent when registering
        # the graph. Prime at that explicit entrypoint instead of at package
        # import time so lightweight submodules can be imported without pulling
        # in the whole tool/subagent graph.
        prime_enabled_skills_cache()
        globals()[name] = make_lead_agent
        return make_lead_agent
    if name in {"SandboxState", "ThreadState"}:
        thread_state = import_module("deerflow.agents.thread_state")
        SandboxState = thread_state.SandboxState
        ThreadState = thread_state.ThreadState

        exports = {"SandboxState": SandboxState, "ThreadState": ThreadState}
        globals().update(exports)
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
