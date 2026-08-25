from __future__ import annotations

from pathlib import Path

from app.gateway.routers.project_assets import project_router
from deerflow.persistence import shared_assets as persistence_shared_assets
from deerflow.persistence.base import Base
from deerflow.persistence.shared_assets import AgentRow
from deerflow.persistence.shared_assets.binding_model import _TRIGGER_DDL

FULL_SCHEMA = (Path(__file__).resolve().parents[1] / "packages/harness/deerflow/persistence/full_schema.sql").read_text(encoding="utf-8")


def test_project_agent_owns_one_mutable_definition() -> None:
    columns = set(AgentRow.__table__.columns.keys())

    assert {
        "description",
        "agents_instructions",
        "soul",
        "identity",
        "user_context",
        "model_ref",
        "model_settings",
        "tool_groups",
        "payload_schema_version",
        "payload_checksum",
    } <= columns
    assert "current_version_id" not in columns
    assert "agent_versions" not in Base.metadata.tables
    assert not hasattr(persistence_shared_assets, "AgentVersionRow")
    assert not hasattr(persistence_shared_assets, "AgentVersionSkillRefRow")
    assert not hasattr(persistence_shared_assets, "AgentVersionMcpRefRow")

    skill_refs = Base.metadata.tables["agent_skill_refs"]
    assert set(skill_refs.columns.keys()) == {
        "agent_id",
        "sort_order",
        "skill_asset_scope",
        "skill_asset_id",
    }

    mcp_refs = Base.metadata.tables["agent_mcp_refs"]
    assert set(mcp_refs.columns.keys()) == {
        "agent_id",
        "sort_order",
        "mcp_server_version_id",
    }


def test_project_agent_routes_do_not_expose_a_version_lifecycle() -> None:
    route_paths = {route.path for route in project_router.routes}

    assert "/api/projects/{project_id}/agents/{asset_id}/versions" not in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/versions/{version_id}/activate" not in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/instructions" in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/capability-bindings" in route_paths


def test_schema_v1_contains_only_the_agent_definition_aggregate() -> None:
    assert "CREATE TABLE agent_versions" not in FULL_SCHEMA
    assert "CREATE TABLE agent_version_skill_refs" not in FULL_SCHEMA
    assert "CREATE TABLE agent_version_mcp_refs" not in FULL_SCHEMA
    assert "created_agent_version_id" not in FULL_SCHEMA

    agents = FULL_SCHEMA.split("CREATE TABLE agents (", 1)[1].split(");", 1)[0].lower()
    for column in (
        "definition_id uuid not null",
        "description text default '' not null",
        "agents_instructions text default '' not null",
        "soul text default '' not null",
        "identity text default '' not null",
        "user_context text default '' not null",
        "model_ref varchar(255) default 'default' not null",
        "model_settings jsonb default '{}'::jsonb not null",
        "tool_groups jsonb default '[]'::jsonb not null",
        "payload_schema_version integer default 4 not null",
        "payload_checksum char(64) not null",
    ):
        assert column in agents

    assert "CREATE TABLE agent_skill_refs (" in FULL_SCHEMA
    assert "CREATE TABLE agent_mcp_refs (" in FULL_SCHEMA


def test_agent_definition_mutations_are_database_fenced() -> None:
    ddl = "\n".join(_TRIGGER_DDL)

    assert "deerflow.agent_definition_mutation_id" in ddl
    assert "trg_agents_definition_mutation" in ddl
    assert "trg_agent_skill_refs_definition_mutation" in ddl
    assert "trg_agent_mcp_refs_definition_mutation" in ddl
    assert "NEW.definition_id IS NOT DISTINCT FROM OLD.definition_id" in ddl
    assert "NEW.revision != OLD.revision + 1" in ddl
    assert ddl.count("IS NOT DISTINCT FROM target_agent_id::text") == 2
    assert "agent_versions" not in ddl
