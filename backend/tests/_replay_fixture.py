"""Shared config + gateway-drive helpers for the record/replay e2e.

Record (``scripts/record_gateway.py`` + ``scripts/build_fixture_from_jsonl.py``)
and replay (``tests/test_replay_golden.py``)
MUST drive the gateway through an identical, prompt-affecting config — otherwise
the system prompt differs and the recorded input hashes never match on replay.
Centralising the config builder + drive loop here makes that identity hold by
construction; only the ``models[].use`` block differs (real model vs
``ReplayChatModel``).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

# mode -> (thinking_enabled, is_plan_mode, subagent_enabled). Mirrors the
# frontend mapping in core/threads/hooks.ts.
MODE_CONTEXT: dict[str, tuple[bool, bool, bool]] = {
    "flash": (False, False, False),
    "thinking": (True, False, False),
    "pro": (True, True, False),
    # thinking_enabled mirrors the frontend `context.mode !== "flash"` (hooks.ts),
    # so ultra is thinking-enabled too.
    "ultra": (True, True, True),
}

# The replay model block: same model NAME as recording (so nothing in the prompt
# shifts), only ``use`` swapped to the deterministic replay provider.
REPLAY_MODEL_BLOCK = """\
  - name: scenario-model
    display_name: Scenario Model
    use: replay_provider:ReplayChatModel
    model: replay
    supports_thinking: true"""


def real_model_block(model: str) -> str:
    return f"""\
  - name: scenario-model
    display_name: Scenario Model
    use: langchain_openai:ChatOpenAI
    model: {model}
    api_key: $OPENAI_API_KEY
    base_url: $OPENAI_API_BASE"""


def build_config_yaml(*, model_block: str, home: Path) -> str:
    """Full gateway config. Only ``model_block`` varies between record/replay.

    Everything that shapes the system prompt is pinned so record, replay, and CI
    produce byte-identical prompts regardless of the machine:
    - sandbox / tool_groups / tools — fixed here
    - skills — pointed at an empty ``<home>/skills`` so filesystem skills (incl.
      gitignored custom skills present only on a dev box) never leak into the
      prompt. Pair with an empty ``extensions_config.json`` (no MCP) via
      :func:`prepare_hermetic_extras`.
    - memory / summarization — disabled (background, non-deterministic timing)
    """
    return f"""\
log_level: warning
models:
{model_block}
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
skills:
  path: {home / "skills"}
  container_path: /mnt/skills
tool_groups:
  - name: file:read
  - name: file:write
tools:
  - name: ls
    group: file:read
    use: deerflow.sandbox.tools:ls_tool
  - name: read_file
    group: file:read
    use: deerflow.sandbox.tools:read_file_tool
  - name: write_file
    group: file:write
    use: deerflow.sandbox.tools:write_file_tool
# Memory + summarization make background / debounced model calls whose timing is
# non-deterministic; disable them so record and replay see the same model-call
# set. Title stays enabled, but the default title.model_name: null path is a
# local state update rather than a recorded model call.
memory:
  enabled: false
  injection_enabled: false
summarization:
  enabled: false
agents_api:
  enabled: true
database:
  url: $DATABASE_URL
"""


def prepare_hermetic_extras(home: Path) -> Path:
    """Create the empty skills tree + an empty extensions_config.json so the
    system prompt has no environment-dependent skills/MCP content.

    Returns the extensions-config path; the caller must point
    ``DEER_FLOW_EXTENSIONS_CONFIG_PATH`` at it. Call before starting the gateway.
    """
    (home / "skills" / "public").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "custom").mkdir(parents=True, exist_ok=True)
    extensions = home / "extensions_config.json"
    extensions.write_text(json.dumps({"mcpServers": {}, "skills": {}}), encoding="utf-8")
    return extensions


def sse_event_shapes(resp) -> list[dict]:
    """Reduce an SSE stream to (event name, sorted top-level data keys).

    Snapshots the *shape* of the stream, not volatile values, so the golden is
    stable across runs while still catching event-sequence / payload-shape drift.
    """
    events: list[dict] = []
    current: str | None = None
    for line in resp.iter_lines():
        if line.startswith("event:"):
            current = line[len("event:") :].strip()
        elif line.startswith("data:"):
            raw = line[len("data:") :].strip()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"_raw": raw[:200]}
            events.append({"event": current, "keys": sorted(data.keys()) if isinstance(data, dict) else None})
    return events


def drive_gateway(app, *, prompt: str, context: dict) -> list[dict]:
    """Register -> create project Agent/thread -> stream a private run.

    This is the final M4 project-private wire path, driven in-process via
    Starlette's TestClient with the real auth and asset-authoring flows.
    """
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        reg = client.post(
            "/api/v1/auth/register",
            json={"email": f"e2e-{uuid.uuid4().hex[:8]}@example.com", "password": "very-strong-password-123"},
        )
        assert reg.status_code == 201, reg.text
        csrf = client.cookies.get("csrf_token")
        assert csrf, "register must set csrf_token cookie"
        headers = {"X-CSRF-Token": csrf}

        suffix = uuid.uuid4().hex[:10]
        project = client.post(
            "/api/projects",
            json={"slug": f"replay-{suffix}", "display_name": "Replay project"},
            headers=headers,
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]

        asset = client.post(
            f"/api/projects/{project_id}/agents",
            json={"slug": f"replay-agent-{suffix}", "display_name": "Replay Agent"},
            headers=headers,
        )
        assert asset.status_code == 201, asset.text
        asset_id = asset.json()["item"]["id"]
        version = client.post(
            f"/api/projects/{project_id}/agents/{asset_id}/versions",
            json={
                "description": "Deterministic gateway replay",
                "soul": "Use the exact project tools to complete the request.",
                "model_ref": "scenario-model",
                "tool_groups": ["file:read", "file:write"],
                "skill_version_ids": [],
                "mcp_version_ids": [],
                "expected_asset_version": 1,
            },
            headers=headers,
        )
        assert version.status_code == 201, version.text
        version_id = version.json()["data"]["id"]
        published = client.post(
            f"/api/projects/{project_id}/agents/{asset_id}/versions/{version_id}/publish",
            json={"expected_asset_version": 2},
            headers=headers,
        )
        assert published.status_code == 200, published.text

        thread_id = str(uuid.uuid4())
        created = client.post(
            f"/api/projects/{project_id}/private-work/threads",
            json={
                "thread_id": thread_id,
                "agent_asset_id": asset_id,
                "agent_scope": "project",
                "metadata": {},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text

        body = {
            "assistant_id": "lead_agent",
            "input": {"messages": [{"role": "user", "content": prompt}]},
            "config": {"recursion_limit": 50},
            "context": context,
            "stream_mode": ["values"],
        }
        with client.stream(
            "POST",
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/stream",
            json=body,
            headers=headers,
        ) as resp:
            assert resp.status_code == 200, resp.read().decode()
            return sse_event_shapes(resp)
