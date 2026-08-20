"""Use one Current Version lifecycle for Agent and Skill assets.

Revision ID: current_asset_version_lifecycle
Revises: skill_credential_source_field
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping

import sqlalchemy as sa
from alembic import op

revision = "current_asset_version_lifecycle"
down_revision = "skill_credential_source_field"
branch_labels = None
depends_on = None

_SYSTEM_ASSET_ID_NAMESPACE = uuid.UUID("6f6622dd-a1f5-5799-a2f7-d9f793ea8d2e")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    ).hexdigest()


def _agent_document(row: Mapping[str, object], skill_refs: list[dict[str, str]]) -> dict[str, object]:
    return {
        "description": row["description"],
        "mcp_version_ids": row["mcp_version_ids"],
        "model_ref": row["model_ref"],
        "skill_refs": skill_refs,
        "soul": row["soul"],
        "tool_groups": row["tool_groups"],
        "agents_instructions": row["agents_instructions"],
        "identity": row["identity"],
        "user_context": row["user_context"],
        "model_settings": row["model_settings"],
    }


def _agent_version_payload(connection, version_id: object) -> tuple[dict[str, object], str, list[str]]:
    row = (
        connection.execute(
            sa.text(
                """
            SELECT v.description, v.agents_instructions, v.soul, v.identity,
                   v.user_context, v.model_ref, v.model_settings, v.tool_groups,
                   a.slug, a.source_key,
                   COALESCE(
                     (SELECT jsonb_agg(r.mcp_server_version_id::text ORDER BY r.sort_order)
                      FROM agent_version_mcp_refs r WHERE r.agent_version_id = v.id),
                     '[]'::jsonb
                   ) AS mcp_version_ids
            FROM agent_versions v
            JOIN agents a ON a.id = v.agent_id
            WHERE v.id = :version_id
            """,
            ),
            {"version_id": version_id},
        )
        .mappings()
        .one()
    )
    skill_rows = (
        connection.execute(
            sa.text(
                """
            SELECT s.scope, s.id AS asset_id, r.skill_version_id
            FROM agent_version_skill_refs r
            JOIN skill_versions v ON v.id = r.skill_version_id
            JOIN skills s ON s.id = v.skill_id
            WHERE r.agent_version_id = :version_id
            ORDER BY r.sort_order
            """,
            ),
            {"version_id": version_id},
        )
        .mappings()
        .all()
    )
    refs = [{"asset_id": str(item["asset_id"]), "scope": str(item["scope"])} for item in skill_rows]
    document = _agent_document(row, refs)
    checksum = _canonical_digest(document)
    payload = {
        "slug": row["slug"],
        "source_key": row["source_key"],
        "description": row["description"],
        "agents_instructions": row["agents_instructions"],
        "soul": row["soul"],
        "identity": row["identity"],
        "user_context": row["user_context"],
        "model_ref": row["model_ref"],
        "model_settings": row["model_settings"],
        "tool_groups": row["tool_groups"],
        "skill_refs": [{"scope": item["scope"], "asset_id": item["asset_id"]} for item in refs],
        "mcp_version_ids": row["mcp_version_ids"],
        "payload_schema_version": 4,
        "resolved_skill_version_ids": [str(item["skill_version_id"]) for item in skill_rows],
    }
    dependencies = [
        *payload["resolved_skill_version_ids"],
        *row["mcp_version_ids"],
    ]
    return payload, checksum, dependencies


def _skill_payload(connection, version_id: object) -> tuple[dict[str, object], str]:
    version = (
        connection.execute(
            sa.text("SELECT secret_requirements FROM skill_versions WHERE id = :version_id"),
            {"version_id": version_id},
        )
        .mappings()
        .one()
    )
    requirements: list[dict[str, object]] = []
    for item in version["secret_requirements"]:
        if isinstance(item, str):
            requirements.append({"name": item, "optional": False})
        elif isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("optional", False), bool):
            requirements.append(
                {
                    "name": item["name"],
                    "optional": item.get("optional", False),
                }
            )
        else:
            raise RuntimeError("invalid Skill secret requirements during migration")
    files = (
        connection.execute(
            sa.text(
                """
            SELECT path, media_type, content
            FROM skill_version_files
            WHERE skill_version_id = :version_id
            ORDER BY path
            """,
            ),
            {"version_id": version_id},
        )
        .mappings()
        .all()
    )
    payload = {
        "files": [
            {
                "path": item["path"],
                "media_type": item["media_type"],
                "content_base64": base64.b64encode(bytes(item["content"])).decode("ascii"),
            }
            for item in files
        ],
        "secret_requirements": requirements,
    }
    checksum_document = [
        {
            "path": item["path"],
            "sha256": hashlib.sha256(bytes(item["content"])).hexdigest(),
            "size_bytes": len(bytes(item["content"])),
        }
        for item in files
    ]
    return payload, _canonical_digest(checksum_document)


def _mcp_payload(connection, run_row: Mapping[str, object]) -> dict[str, object]:
    version = (
        connection.execute(
            sa.text(
                """
            SELECT description, transport, command, args, url,
                   non_secret_env, non_secret_headers, oauth_metadata,
                   routing, tool_overrides, timeout_seconds
            FROM mcp_server_versions WHERE id = :version_id
            """,
            ),
            {"version_id": run_row["version_id"]},
        )
        .mappings()
        .one()
    )
    slots = (
        connection.execute(
            sa.text(
                """
            SELECT name, purpose, payload_schema, required
            FROM mcp_version_credential_slots
            WHERE mcp_server_version_id = :version_id
            ORDER BY name, id
            """,
            ),
            {"version_id": run_row["version_id"]},
        )
        .mappings()
        .all()
    )
    grants = (
        connection.execute(
            sa.text(
                """
            SELECT snapshot.credential_grant_id
            FROM run_mcp_grant_snapshots snapshot
            JOIN mcp_version_credential_slots slot
              ON slot.id = snapshot.credential_slot_id
            WHERE snapshot.project_id = :project_id
              AND snapshot.owner_user_id = :owner_user_id
              AND snapshot.run_id = :run_id
              AND snapshot.mcp_version_id = :version_id
            ORDER BY slot.name, slot.id
            """,
            ),
            run_row,
        )
        .scalars()
        .all()
    )
    return {
        "definition": {
            "description": version["description"],
            "transport": version["transport"],
            "command": version["command"],
            "args": version["args"],
            "url": version["url"],
            "env": version["non_secret_env"],
            "headers": version["non_secret_headers"],
            "oauth": version["oauth_metadata"],
            "routing": version["routing"],
            "tool_overrides": version["tool_overrides"],
            "timeout_seconds": version["timeout_seconds"],
            "credential_slots": [dict(item) for item in slots],
        },
        "credential_grant_ids": [str(value) for value in grants],
    }


def _backfill_run_snapshots(connection) -> None:
    rows = (
        connection.execute(
            sa.text(
                """
            SELECT project_id, owner_user_id, thread_id, run_id, asset_kind,
                   dependency_order, asset_scope, asset_id, version_id,
                   payload_checksum, catalog_generation
            FROM run_asset_versions
            ORDER BY project_id, owner_user_id, run_id, asset_kind, dependency_order
            """,
            ),
        )
        .mappings()
        .all()
    )
    for row in rows:
        checksum = row["payload_checksum"]
        dependencies: list[str] = []
        if row["asset_kind"] == "agent":
            payload, checksum, dependencies = _agent_version_payload(
                connection,
                row["version_id"],
            )
        elif row["asset_kind"] == "skill":
            payload, checksum = _skill_payload(connection, row["version_id"])
        elif row["asset_kind"] == "mcp":
            payload = _mcp_payload(connection, row)
        else:
            raise RuntimeError("invalid Run asset kind during migration")
        snapshot = {
            "schema_version": 1,
            "kind": row["asset_kind"],
            "scope": row["asset_scope"],
            "asset_id": str(row["asset_id"]),
            "version_id": str(row["version_id"]),
            "checksum": checksum,
            "catalog_generation": row["catalog_generation"],
            "dependency_version_ids": dependencies,
            row["asset_kind"]: payload,
        }
        connection.execute(
            sa.text(
                """
                UPDATE run_asset_versions
                SET snapshot_json = CAST(:snapshot AS jsonb),
                    payload_checksum = :checksum
                WHERE project_id = :project_id
                  AND owner_user_id = :owner_user_id
                  AND run_id = :run_id
                  AND asset_kind = :asset_kind
                  AND dependency_order = :dependency_order
                """,
            ),
            {
                **row,
                "snapshot": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                "checksum": checksum,
            },
        )


def _migrate_blueprints(connection) -> None:
    rows = (
        connection.execute(
            sa.text(
                """
            SELECT id, blueprint_json
            FROM agent_design_sessions
            WHERE blueprint_json IS NOT NULL
            """,
            ),
        )
        .mappings()
        .all()
    )
    for row in rows:
        blueprint = dict(row["blueprint_json"])
        version_ids = blueprint.pop("skill_version_ids", [])
        refs: list[dict[str, str]] = []
        for version_id in version_ids:
            skill = connection.execute(
                sa.text(
                    """
                    SELECT s.scope, s.id
                    FROM skill_versions v JOIN skills s ON s.id = v.skill_id
                    WHERE v.id = :version_id
                    """,
                ),
                {"version_id": version_id},
            ).one_or_none()
            if skill is None:
                raise RuntimeError("Agent Builder Skill reference is missing")
            refs.append({"scope": str(skill.scope), "asset_id": str(skill.id)})
        blueprint["skill_refs"] = refs
        connection.execute(
            sa.text(
                """
                UPDATE agent_design_sessions
                SET blueprint_json = CAST(:blueprint AS jsonb),
                    blueprint_checksum = :checksum
                WHERE id = :id
                """,
            ),
            {
                "id": row["id"],
                "blueprint": json.dumps(blueprint, ensure_ascii=False, separators=(",", ":")),
                "checksum": _canonical_digest(blueprint),
            },
        )


def _legacy_agent_checksum(connection, version_id: object) -> str:
    row = (
        connection.execute(
            sa.text(
                """
                SELECT description, agents_instructions, soul, identity,
                       user_context, model_ref, model_settings, tool_groups,
                       payload_schema_version,
                       COALESCE(
                         (SELECT jsonb_agg(r.skill_version_id::text ORDER BY r.sort_order)
                          FROM agent_version_skill_refs r
                          WHERE r.agent_version_id = v.id),
                         '[]'::jsonb
                       ) AS skill_version_ids,
                       COALESCE(
                         (SELECT jsonb_agg(r.mcp_server_version_id::text ORDER BY r.sort_order)
                          FROM agent_version_mcp_refs r
                          WHERE r.agent_version_id = v.id),
                         '[]'::jsonb
                       ) AS mcp_version_ids
                FROM agent_versions v
                WHERE v.id = :version_id
                """,
            ),
            {"version_id": version_id},
        )
        .mappings()
        .one()
    )
    schema_version = int(row["payload_schema_version"])
    if schema_version not in (1, 2, 3):
        raise RuntimeError("unsupported legacy Agent payload schema")
    document: dict[str, object] = {
        "description": row["description"],
        "mcp_version_ids": row["mcp_version_ids"],
        "model_ref": row["model_ref"],
        "skill_version_ids": row["skill_version_ids"],
        "soul": row["soul"],
        "tool_groups": row["tool_groups"],
    }
    if schema_version in (2, 3):
        document.update(
            {
                "agents_instructions": row["agents_instructions"],
                "identity": row["identity"],
                "user_context": row["user_context"],
            }
        )
    if schema_version == 3:
        document["model_settings"] = row["model_settings"]
    return _canonical_digest(document)


def _mcp_version_checksum(connection, version_id: object) -> str:
    payload = _mcp_payload(
        connection,
        {
            "project_id": uuid.UUID(int=0),
            "owner_user_id": "preflight",
            "run_id": "preflight",
            "version_id": version_id,
        },
    )
    definition = payload["definition"]
    if not isinstance(definition, dict):
        raise RuntimeError("invalid MCP definition during preflight")
    return _canonical_digest(definition)


def _preflight_project_lineage(connection) -> None:
    """Reject cross-asset, backward, cyclic, or ambiguous live lineages."""

    for asset_table, version_table, parent_column in (
        ("agents", "agent_versions", "agent_id"),
        ("skills", "skill_versions", "skill_id"),
    ):
        assets = connection.execute(
            sa.text(
                f"SELECT id,current_published_version_id FROM {asset_table} WHERE scope='project' ORDER BY id",
            )
        ).all()
        for asset_id, current_id in assets:
            rows = (
                connection.execute(
                    sa.text(
                        f"""
                        SELECT id,version_number,supersedes_version_id,workflow_status
                        FROM {version_table}
                        WHERE {parent_column}=:asset_id
                        ORDER BY version_number,id
                        """,
                    ),
                    {"asset_id": asset_id},
                )
                .mappings()
                .all()
            )
            by_id = {row["id"]: row for row in rows}
            if len(by_id) != len(rows) or len({row["version_number"] for row in rows}) != len(rows):
                raise RuntimeError("Project Agent/Skill lineage is ambiguous")
            if current_id is not None and current_id not in by_id:
                raise RuntimeError("Project Current Version has invalid ownership")
            for row in rows:
                parent_id = row["supersedes_version_id"]
                if parent_id is None:
                    continue
                parent = by_id.get(parent_id)
                if parent is None or int(parent["version_number"]) >= int(row["version_number"]):
                    raise RuntimeError("Project Agent/Skill lineage is invalid")

            eligible = {row["id"] for row in rows if row["workflow_status"] in ("draft", "published")}
            if current_id is not None:
                eligible.add(current_id)
                reachable = {current_id}
                changed = True
                while changed:
                    changed = False
                    for row in rows:
                        if row["id"] in eligible and row["supersedes_version_id"] in reachable and row["id"] not in reachable:
                            reachable.add(row["id"])
                            changed = True
            else:
                roots = {row["id"] for row in rows if row["id"] in eligible and row["supersedes_version_id"] is None}
                reachable = set(roots)
                changed = True
                while changed:
                    changed = False
                    for row in rows:
                        if row["id"] in eligible and row["supersedes_version_id"] in reachable and row["id"] not in reachable:
                            reachable.add(row["id"])
                            changed = True
            live_heads = [version_id for version_id in reachable if not any(row["id"] in reachable and row["supersedes_version_id"] == version_id for row in rows)]
            if len(live_heads) > 1:
                raise RuntimeError("Project Agent/Skill forward lineage has multiple heads")


def _run_key(row: Mapping[str, object]) -> tuple[object, object, object]:
    return (row["project_id"], row["owner_user_id"], row["run_id"])


def _preflight_run_closures(connection) -> None:
    """Prove every legacy Run has a complete, checksum-bound asset closure."""

    run_rows = (
        connection.execute(
            sa.text(
                """
                SELECT project_id,owner_user_id,thread_id,run_id
                FROM runs ORDER BY project_id,owner_user_id,run_id
                """,
            )
        )
        .mappings()
        .all()
    )
    asset_rows = (
        connection.execute(
            sa.text(
                """
                SELECT project_id,owner_user_id,thread_id,run_id,asset_kind,
                       dependency_order,asset_scope,asset_id,version_id,
                       payload_checksum,catalog_generation
                FROM run_asset_versions
                ORDER BY project_id,owner_user_id,run_id,dependency_order
                """,
            )
        )
        .mappings()
        .all()
    )
    assets_by_run: dict[tuple[object, object, object], list[Mapping[str, object]]] = {}
    for row in asset_rows:
        assets_by_run.setdefault(_run_key(row), []).append(row)
    if set(assets_by_run) != {_run_key(row) for row in run_rows}:
        raise RuntimeError("Run asset closure is missing or orphaned")

    for run in run_rows:
        rows = assets_by_run[_run_key(run)]
        if (
            not rows
            or rows[0]["asset_kind"] != "agent"
            or [row["dependency_order"] for row in rows] != list(range(len(rows)))
            or any(row["thread_id"] != run["thread_id"] for row in rows)
            or len({row["catalog_generation"] for row in rows}) != 1
        ):
            raise RuntimeError("Run asset closure order is invalid")
        kinds = [row["asset_kind"] for row in rows]
        if any(kind not in ("agent", "skill", "mcp") for kind in kinds) or kinds != sorted(kinds, key={"agent": 0, "skill": 1, "mcp": 2}.get):
            raise RuntimeError("Run asset closure kind order is invalid")

        included_skills: set[object] = set()
        included_mcps: set[object] = set()
        included_mcp_scopes: dict[object, object] = {}
        required_skills: set[object] = set()
        required_mcps: set[object] = set()
        lead_is_builtin_main = False
        for row in rows:
            if row["asset_kind"] == "agent":
                version = (
                    connection.execute(
                        sa.text(
                            """
                            SELECT v.agent_id,a.scope,a.project_id,a.source_key,
                                   v.payload_checksum
                            FROM agent_versions v JOIN agents a ON a.id=v.agent_id
                            WHERE v.id=:version_id
                            """,
                        ),
                        row,
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    version is None
                    or version["agent_id"] != row["asset_id"]
                    or version["scope"] != row["asset_scope"]
                    or (version["project_id"] != row["project_id"] if version["scope"] == "project" else version["project_id"] is not None)
                    or version["payload_checksum"] != row["payload_checksum"]
                    or _legacy_agent_checksum(connection, row["version_id"]) != row["payload_checksum"]
                ):
                    raise RuntimeError("Run Agent snapshot integrity is invalid")
                if row is rows[0]:
                    lead_is_builtin_main = version["scope"] == "system" and version["source_key"] == "builtin:agent:project-assistant"
                required_skills.update(
                    connection.execute(
                        sa.text(
                            "SELECT skill_version_id FROM agent_version_skill_refs WHERE agent_version_id=:version_id",
                        ),
                        row,
                    ).scalars()
                )
                required_mcps.update(
                    connection.execute(
                        sa.text(
                            "SELECT mcp_server_version_id FROM agent_version_mcp_refs WHERE agent_version_id=:version_id",
                        ),
                        row,
                    ).scalars()
                )
            elif row["asset_kind"] == "skill":
                version = (
                    connection.execute(
                        sa.text(
                            """
                            SELECT v.skill_id,s.scope,s.project_id,
                                   v.payload_checksum
                            FROM skill_versions v JOIN skills s ON s.id=v.skill_id
                            WHERE v.id=:version_id
                            """,
                        ),
                        row,
                    )
                    .mappings()
                    .one_or_none()
                )
                _payload, actual = _skill_payload(connection, row["version_id"])
                if (
                    version is None
                    or version["skill_id"] != row["asset_id"]
                    or version["scope"] != row["asset_scope"]
                    or (version["project_id"] != row["project_id"] if version["scope"] == "project" else version["project_id"] is not None)
                    or version["payload_checksum"] != row["payload_checksum"]
                    or actual != row["payload_checksum"]
                ):
                    raise RuntimeError("Run Skill snapshot integrity is invalid")
                included_skills.add(row["version_id"])
            else:
                version = (
                    connection.execute(
                        sa.text(
                            """
                            SELECT v.mcp_server_id,a.scope,a.project_id,
                                   v.payload_checksum
                            FROM mcp_server_versions v JOIN mcp_servers a ON a.id=v.mcp_server_id
                            WHERE v.id=:version_id
                            """,
                        ),
                        row,
                    )
                    .mappings()
                    .one_or_none()
                )
                actual = _mcp_version_checksum(connection, row["version_id"])
                if (
                    version is None
                    or version["mcp_server_id"] != row["asset_id"]
                    or version["scope"] != row["asset_scope"]
                    or (version["project_id"] != row["project_id"] if version["scope"] == "project" else version["project_id"] is not None)
                    or version["payload_checksum"] != row["payload_checksum"]
                    or actual != row["payload_checksum"]
                ):
                    raise RuntimeError("Run MCP snapshot integrity is invalid")
                included_mcps.add(row["version_id"])
                included_mcp_scopes[row["version_id"]] = row["asset_scope"]

        if (not lead_is_builtin_main and required_skills != included_skills) or not required_skills <= included_skills or required_mcps != included_mcps:
            raise RuntimeError("Run dependency closure is incomplete")

        credential_rows = (
            connection.execute(
                sa.text(
                    """
                    SELECT * FROM run_skill_credential_snapshots
                    WHERE project_id=:project_id AND owner_user_id=:owner_user_id
                      AND run_id=:run_id
                    ORDER BY skill_version_id,secret_name
                    """,
                ),
                run,
            )
            .mappings()
            .all()
        )
        credential_by_target = {(item["skill_version_id"], item["secret_name"]): item for item in credential_rows}
        if len(credential_by_target) != len(credential_rows) or any(item["skill_version_id"] not in included_skills for item in credential_rows):
            raise RuntimeError("Run Skill credential closure is invalid")
        for skill_version_id in included_skills:
            payload, _checksum = _skill_payload(connection, skill_version_id)
            requirements = payload["secret_requirements"]
            declared = {item["name"] for item in requirements}
            required = {item["name"] for item in requirements if not item["optional"]}
            present = {name for version_id, name in credential_by_target if version_id == skill_version_id}
            if not required <= present or not present <= declared:
                raise RuntimeError("Run required Skill credential closure is incomplete")
            for name in present:
                reference = credential_by_target[(skill_version_id, name)]
                binding = (
                    connection.execute(
                        sa.text(
                            """
                            SELECT * FROM project_skill_credential_bindings
                            WHERE id=:skill_credential_binding_id
                            """,
                        ),
                        reference,
                    )
                    .mappings()
                    .one_or_none()
                )
                if binding is None or any(
                    binding[column] != reference[snapshot_column]
                    for column, snapshot_column in (
                        ("project_id", "project_id"),
                        ("skill_id", "skill_id"),
                        ("skill_version_id", "skill_version_id"),
                        ("secret_name", "secret_name"),
                        ("config_revision", "binding_revision"),
                        ("credential_id", "credential_id"),
                        ("credential_version_id", "credential_version_id"),
                    )
                ):
                    raise RuntimeError("Run Skill credential authority is invalid")
                binding_source_field = binding.get("source_env_field_name", binding["secret_name"])
                snapshot_source_field = reference.get("source_env_field_name", reference["secret_name"])
                if binding_source_field != snapshot_source_field:
                    raise RuntimeError("Run Skill credential authority is invalid")
                payload_schema = connection.execute(
                    sa.text(
                        """
                        SELECT payload_schema
                        FROM credential_versions
                        WHERE id=:credential_version_id
                          AND credential_id=:credential_id
                        """,
                    ),
                    reference,
                ).scalar_one_or_none()
                env_fields = payload_schema.get("env") if isinstance(payload_schema, dict) else None
                if not isinstance(env_fields, list) or not all(isinstance(field, str) for field in env_fields) or snapshot_source_field not in env_fields:
                    raise RuntimeError("Run Skill credential source field is invalid")

        grant_rows = (
            connection.execute(
                sa.text(
                    """
                    SELECT snapshot.*,slot.mcp_server_version_id AS slot_version_id,
                           slot.required,
                           credential_grant.mcp_server_version_id AS grant_version_id,
                           credential_grant.credential_slot_id AS grant_slot_id,
                           credential_grant.credential_version_id AS grant_credential_version_id,
                           credential.scope AS credential_scope,
                           credential.project_id AS credential_project_id
                    FROM run_mcp_grant_snapshots snapshot
                    LEFT JOIN mcp_version_credential_slots slot ON slot.id=snapshot.credential_slot_id
                    LEFT JOIN credential_grants credential_grant
                      ON credential_grant.id=snapshot.credential_grant_id
                    LEFT JOIN credential_versions credential_version
                      ON credential_version.id=credential_grant.credential_version_id
                    LEFT JOIN credentials credential
                      ON credential.id=credential_version.credential_id
                    WHERE snapshot.project_id=:project_id
                      AND snapshot.owner_user_id=:owner_user_id
                      AND snapshot.run_id=:run_id
                    """,
                ),
                run,
            )
            .mappings()
            .all()
        )
        grants_by_slot = {(item["mcp_version_id"], item["credential_slot_id"]): item for item in grant_rows}
        if len(grants_by_slot) != len(grant_rows) or any(item["mcp_version_id"] not in included_mcps for item in grant_rows):
            raise RuntimeError("Run MCP grant closure is invalid")
        for item in grant_rows:
            expected_scope = included_mcp_scopes.get(item["mcp_version_id"])
            credential_scope_invalid = item["credential_scope"] != expected_scope or (item["credential_project_id"] != run["project_id"] if expected_scope == "project" else item["credential_project_id"] is not None)
            if (
                item["slot_version_id"] != item["mcp_version_id"]
                or item["grant_version_id"] != item["mcp_version_id"]
                or item["grant_slot_id"] != item["credential_slot_id"]
                or item["grant_credential_version_id"] != item["credential_version_id"]
                or credential_scope_invalid
            ):
                raise RuntimeError("Run MCP grant authority is invalid")
        required_slots = (
            {
                (item[0], item[1])
                for item in connection.execute(
                    sa.text(
                        "SELECT mcp_server_version_id,id FROM mcp_version_credential_slots WHERE required=true AND mcp_server_version_id = ANY(:version_ids)",
                    ),
                    {"version_ids": list(included_mcps)},
                ).all()
            }
            if included_mcps
            else set()
        )
        if not required_slots <= set(grants_by_slot):
            raise RuntimeError("Run required MCP grant closure is incomplete")


def _preflight_legacy_lifecycle(connection) -> None:
    """Fail before destructive DDL when legacy integrity is not provable."""

    _preflight_project_lineage(connection)

    invalid_system_ref = connection.execute(
        sa.text(
            """
            SELECT a.source_key
            FROM agents a
            JOIN agent_versions av ON av.agent_id = a.id
            JOIN agent_version_skill_refs ref ON ref.agent_version_id = av.id
            JOIN skill_versions sv ON sv.id = ref.skill_version_id
            JOIN skills s ON s.id = sv.skill_id
            WHERE a.scope = 'system' AND s.scope <> 'system'
            ORDER BY a.source_key
            LIMIT 1
            """,
        )
    ).scalar_one_or_none()
    if invalid_system_ref is not None:
        raise RuntimeError(
            f"System Agent references a Project Skill: {invalid_system_ref}",
        )
    missing_system_current = connection.execute(
        sa.text(
            """
            SELECT source_key
            FROM (
              SELECT source_key, current_published_version_id AS current_id
              FROM agents WHERE scope = 'system'
              UNION ALL
              SELECT source_key, current_published_version_id AS current_id
              FROM skills WHERE scope = 'system'
            ) assets
            WHERE current_id IS NULL
            ORDER BY source_key
            LIMIT 1
            """,
        )
    ).scalar_one_or_none()
    if missing_system_current is not None:
        raise RuntimeError(
            f"System Agent/Skill has no current version: {missing_system_current}",
        )
    for asset_table, version_table, parent_column in (
        ("agents", "agent_versions", "agent_id"),
        ("skills", "skill_versions", "skill_id"),
    ):
        system_rows = connection.execute(
            sa.text(
                f"SELECT id,source_key FROM {asset_table} WHERE scope='system' ORDER BY id",
            )
        ).all()
        for asset_id, source_key in system_rows:
            if not isinstance(source_key, str) or not source_key:
                raise RuntimeError("System Agent/Skill has no stable source key")
            canonical_id = _canonical_system_version_id(source_key)
            owner = connection.execute(
                sa.text(
                    f"SELECT {parent_column} FROM {version_table} WHERE id=:id",
                ),
                {"id": canonical_id},
            ).scalar_one_or_none()
            if owner is not None and owner != asset_id:
                raise RuntimeError("canonical System v1 identity is already owned")

    agent_rows = connection.execute(
        sa.text(
            """
            SELECT v.id, v.payload_checksum
            FROM agent_versions v
            JOIN agents a ON a.id = v.agent_id
            ORDER BY a.id, v.version_number
            """,
        )
    ).all()
    for version_id, expected in agent_rows:
        if _legacy_agent_checksum(connection, version_id) != expected:
            raise RuntimeError("Agent checksum mismatch during preflight")

    skill_rows = connection.execute(
        sa.text(
            """
            SELECT v.id, v.payload_checksum
            FROM skill_versions v
            JOIN skills s ON s.id = v.skill_id
            ORDER BY s.id, v.version_number
            """,
        )
    ).all()
    for version_id, expected in skill_rows:
        _payload, actual = _skill_payload(connection, version_id)
        if actual != expected:
            raise RuntimeError("Skill checksum mismatch during preflight")

    mcp_rows = connection.execute(
        sa.text(
            "SELECT id,payload_checksum FROM mcp_server_versions ORDER BY mcp_server_id,version_number",
        )
    ).all()
    for version_id, expected in mcp_rows:
        if _mcp_version_checksum(connection, version_id) != expected:
            raise RuntimeError("MCP checksum mismatch during preflight")

    _preflight_run_closures(connection)


def _drop_legacy_triggers(connection) -> None:
    trigger_rows = (
        connection.execute(
            sa.text(
                """
            SELECT trigger_table.relname AS table_name, trigger_row.tgname AS trigger_name
            FROM pg_trigger trigger_row
            JOIN pg_class trigger_table ON trigger_table.oid = trigger_row.tgrelid
            JOIN pg_namespace namespace ON namespace.oid = trigger_table.relnamespace
            JOIN pg_proc trigger_function ON trigger_function.oid = trigger_row.tgfoid
            WHERE namespace.nspname = current_schema()
              AND NOT trigger_row.tgisinternal
              AND trigger_function.proname = ANY(CAST(:function_names AS text[]))
            """,
            ),
            {
                "function_names": [
                    "prevent_shared_asset_version_payload_update",
                    "bump_asset_catalog_generation",
                    "ensure_system_binding_published_version",
                    "enforce_system_skill_version_revocation",
                    "prevent_bound_published_version_downgrade",
                    "prevent_published_version_child_mutation",
                    "enforce_shared_asset_version_state_transition",
                ],
            },
        )
        .mappings()
        .all()
    )
    for row in trigger_rows:
        connection.execute(
            sa.text(
                f'DROP TRIGGER IF EXISTS "{row["trigger_name"]}" ON "{row["table_name"]}"',
            ),
        )
    for table_name, trigger_name in (
        ("agent_versions", "trg_agent_versions_immutable"),
        ("agent_versions", "trg_agent_versions_bound_published"),
        ("agent_versions", "trg_agent_versions_state_transition"),
        ("agent_versions", "trg_agent_versions_generation"),
        ("skill_versions", "trg_skill_versions_immutable"),
        ("skill_versions", "trg_skill_versions_bound_published"),
        ("skill_versions", "trg_skill_versions_state_transition"),
        ("skill_versions", "trg_skill_versions_generation"),
        ("skill_versions", "trg_skill_version_revocations_generation"),
        ("skill_versions", "trg_skill_versions_revocation"),
        ("skill_version_files", "trg_skill_version_files_child_immutable"),
        ("agent_version_skill_refs", "trg_agent_version_skill_refs_child_immutable"),
        ("agent_version_mcp_refs", "trg_agent_version_mcp_refs_child_immutable"),
        ("project_system_agent_bindings", "trg_agent_bindings_published"),
        ("project_system_agent_bindings", "trg_agent_bindings_current"),
        ("project_system_skill_bindings", "trg_skill_bindings_published"),
        ("project_system_skill_bindings", "trg_skill_bindings_current"),
        ("mcp_server_versions", "trg_mcp_server_versions_immutable"),
        ("mcp_server_versions", "trg_mcp_server_versions_bound_published"),
        ("mcp_server_versions", "trg_mcp_server_versions_state_transition"),
        ("mcp_server_versions", "trg_mcp_server_versions_generation"),
        ("credential_versions", "trg_credential_versions_immutable"),
        ("credential_versions", "trg_credential_versions_state_transition"),
        ("credential_versions", "trg_credential_versions_generation"),
        ("skill_version_files", "trg_skill_version_files_immutable"),
        ("agent_version_skill_refs", "trg_agent_version_skill_refs_immutable"),
        ("agent_version_mcp_refs", "trg_agent_version_mcp_refs_immutable"),
        ("mcp_version_credential_slots", "trg_mcp_credential_slots_immutable"),
        ("mcp_version_credential_slots", "trg_mcp_credential_slots_child_immutable"),
        ("project_system_mcp_bindings", "trg_mcp_bindings_published"),
        ("project_system_mcp_bindings", "trg_mcp_bindings_generation"),
        ("mcp_servers", "trg_mcp_servers_generation"),
        ("credentials", "trg_credentials_generation"),
        ("agents", "trg_agents_generation"),
        ("skills", "trg_skills_generation"),
    ):
        connection.execute(
            sa.text(f'DROP TRIGGER IF EXISTS "{trigger_name}" ON "{table_name}"'),
        )
    for function_name in (
        "ensure_system_binding_published_version",
        "prevent_bound_published_version_downgrade",
        "prevent_published_version_child_mutation",
    ):
        connection.execute(sa.text(f'DROP FUNCTION IF EXISTS "{function_name}"()'))


def _canonical_system_version_id(source_key: str) -> uuid.UUID:
    return uuid.uuid5(
        _SYSTEM_ASSET_ID_NAMESPACE,
        f"{source_key}:version:1",
    )


def _normalize_system_assets(connection) -> None:
    connection.execute(
        sa.text(
            "ALTER TABLE project_skill_credential_bindings DROP CONSTRAINT fk_project_skill_credential_bindings_config, DROP CONSTRAINT fk_project_skill_credential_bindings_skill_version",
        )
    )
    connection.execute(
        sa.text(
            "ALTER TABLE project_skill_credential_configs DROP CONSTRAINT fk_project_skill_credential_configs_skill_version",
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE project_skill_credential_bindings binding
            SET admission_only = true
            FROM skills skill
            WHERE skill.id = binding.skill_id
              AND skill.scope = 'system'
            """,
        )
    )
    for asset_table, version_table, parent_column in (
        ("agents", "agent_versions", "agent_id"),
        ("skills", "skill_versions", "skill_id"),
    ):
        rows = (
            connection.execute(
                sa.text(
                    f"""
                    SELECT id, source_key, current_version_id
                    FROM {asset_table}
                    WHERE scope = 'system'
                    ORDER BY id
                    """,
                ),
            )
            .mappings()
            .all()
        )
        for row in rows:
            source_key = row["source_key"]
            current_id = row["current_version_id"]
            if not isinstance(source_key, str) or not source_key or current_id is None:
                raise RuntimeError("System Agent/Skill has no stable source or Current Version")
            canonical_id = _canonical_system_version_id(source_key)
            history = (
                connection.execute(
                    sa.text(
                        f"""
                        SELECT id, version_number, payload_checksum
                        FROM {version_table}
                        WHERE {parent_column} = :asset_id
                        ORDER BY version_number, id
                        """,
                    ),
                    {"asset_id": row["id"]},
                )
                .mappings()
                .all()
            )
            if not history or current_id not in {item["id"] for item in history}:
                raise RuntimeError("System Current Version does not belong to its asset")

            connection.execute(
                sa.text(
                    f"UPDATE {version_table} SET supersedes_version_id = NULL WHERE {parent_column} = :asset_id",
                ),
                {"asset_id": row["id"]},
            )
            if version_table == "agent_versions":
                connection.execute(
                    sa.text(
                        """
                        UPDATE agent_design_sessions
                        SET created_agent_version_id = :current_id
                        WHERE created_agent_id = :asset_id
                        """,
                    ),
                    {"asset_id": row["id"], "current_id": current_id},
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE project_system_agent_bindings
                        SET agent_version_id = :current_id
                        WHERE system_agent_id = :asset_id
                        """,
                    ),
                    {"asset_id": row["id"], "current_id": current_id},
                )
                for child_table in (
                    "agent_version_skill_refs",
                    "agent_version_mcp_refs",
                ):
                    connection.execute(
                        sa.text(
                            f"""
                            DELETE FROM {child_table}
                            WHERE agent_version_id IN (
                              SELECT id FROM agent_versions
                              WHERE agent_id = :asset_id AND id <> :current_id
                            )
                            """,
                        ),
                        {"asset_id": row["id"], "current_id": current_id},
                    )
            else:
                active_bindings = (
                    connection.execute(
                        sa.text(
                            """
                            SELECT * FROM project_skill_credential_bindings
                            WHERE skill_id=:asset_id
                              AND skill_version_id=:current_id
                              AND admission_only=true
                              AND status='active'
                            ORDER BY project_id,secret_name,id
                            """,
                        ),
                        {"asset_id": row["id"], "current_id": current_id},
                    )
                    .mappings()
                    .all()
                )
                for binding in active_bindings:
                    runtime_authority_binding_id = uuid.uuid4()
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO project_skill_credential_bindings
                              (id,project_id,skill_id,skill_version_id,secret_name,
                               credential_id,credential_version_id,config_revision,
                               status,created_by_user_id,created_at,revoked_at,
                               revoked_by_user_id,source_env_field_name,
                               admission_only,runtime_authority_binding_id)
                            VALUES
                              (:id,:project_id,:skill_id,:canonical_id,:secret_name,
                               :credential_id,:credential_version_id,:config_revision,
                               'active',:created_by_user_id,:created_at,NULL,NULL,
                               :source_env_field_name,false,NULL)
                            """,
                        ),
                        {
                            **binding,
                            "id": runtime_authority_binding_id,
                            "canonical_id": canonical_id,
                        },
                    )
                    connection.execute(
                        sa.text(
                            """
                            UPDATE project_skill_credential_bindings
                            SET runtime_authority_binding_id=:authority_id
                            WHERE id=:binding_id AND admission_only=true
                            """,
                        ),
                        {
                            "authority_id": runtime_authority_binding_id,
                            "binding_id": binding["id"],
                        },
                    )
                for skill_id_column, version_id_column in (
                    ("skill_creator_skill_id", "skill_creator_version_id"),
                    ("created_skill_id", "created_skill_version_id"),
                    ("target_skill_id", "base_version_id"),
                ):
                    connection.execute(
                        sa.text(
                            f"""
                            UPDATE skill_design_sessions
                            SET {version_id_column} = :current_id
                            WHERE {skill_id_column} = :asset_id
                              AND {version_id_column} IS NOT NULL
                            """,
                        ),
                        {"asset_id": row["id"], "current_id": current_id},
                    )
                connection.execute(
                    sa.text(
                        """
                        UPDATE project_system_skill_bindings
                        SET skill_version_id = :current_id
                        WHERE system_skill_id = :asset_id
                        """,
                    ),
                    {"asset_id": row["id"], "current_id": current_id},
                )
                connection.execute(
                    sa.text(
                        """
                        DELETE FROM project_skill_credential_configs
                        WHERE skill_id = :asset_id AND skill_version_id <> :current_id
                        """,
                    ),
                    {"asset_id": row["id"], "current_id": current_id},
                )
                connection.execute(
                    sa.text(
                        """
                        DELETE FROM skill_version_files
                        WHERE skill_version_id IN (
                          SELECT id FROM skill_versions
                          WHERE skill_id = :asset_id AND id <> :current_id
                        )
                        """,
                    ),
                    {"asset_id": row["id"], "current_id": current_id},
                )

            connection.execute(
                sa.text(
                    f"DELETE FROM {version_table} WHERE {parent_column} = :asset_id AND id <> :current_id",
                ),
                {"asset_id": row["id"], "current_id": current_id},
            )
            if current_id != canonical_id:
                connection.execute(
                    sa.text(
                        f"UPDATE {version_table} SET version_number = 2 WHERE id = :current_id",
                    ),
                    {"current_id": current_id},
                )
                if version_table == "agent_versions":
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO agent_versions
                              (id,agent_id,version_number,workflow_status,description,
                               soul,model_ref,model_settings,tool_groups,
                               supersedes_version_id,payload_checksum,submitted_at,
                               reviewed_at,reviewed_by_user_id,review_note,
                               created_by_user_id,created_at,agents_instructions,
                               identity,user_context,payload_schema_version)
                            SELECT :canonical_id,agent_id,1,workflow_status,description,
                                   soul,model_ref,model_settings,tool_groups,NULL,
                                   payload_checksum,submitted_at,reviewed_at,
                                   reviewed_by_user_id,review_note,created_by_user_id,
                                   created_at,agents_instructions,identity,user_context,
                                   payload_schema_version
                            FROM agent_versions WHERE id = :current_id
                            """,
                        ),
                        {"canonical_id": canonical_id, "current_id": current_id},
                    )
                    for child_table, columns in (
                        (
                            "agent_version_skill_refs",
                            "skill_asset_scope,skill_asset_id,sort_order",
                        ),
                        (
                            "agent_version_mcp_refs",
                            "mcp_server_version_id,sort_order",
                        ),
                    ):
                        connection.execute(
                            sa.text(
                                f"""
                                INSERT INTO {child_table} (agent_version_id,{columns})
                                SELECT :canonical_id,{columns}
                                FROM {child_table}
                                WHERE agent_version_id = :current_id
                                """,
                            ),
                            {"canonical_id": canonical_id, "current_id": current_id},
                        )
                    connection.execute(
                        sa.text(
                            "UPDATE agent_design_sessions SET created_agent_version_id=:canonical_id WHERE created_agent_id=:asset_id",
                        ),
                        {"canonical_id": canonical_id, "asset_id": row["id"]},
                    )
                    connection.execute(
                        sa.text(
                            "UPDATE project_system_agent_bindings SET agent_version_id=:canonical_id WHERE system_agent_id=:asset_id",
                        ),
                        {"canonical_id": canonical_id, "asset_id": row["id"]},
                    )
                    connection.execute(
                        sa.text(
                            "DELETE FROM agent_version_skill_refs WHERE agent_version_id=:current_id",
                        ),
                        {"current_id": current_id},
                    )
                    connection.execute(
                        sa.text(
                            "DELETE FROM agent_version_mcp_refs WHERE agent_version_id=:current_id",
                        ),
                        {"current_id": current_id},
                    )
                else:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO skill_versions
                              (id,skill_id,version_number,workflow_status,description,
                               frontmatter,compatibility,secret_requirements,
                               scan_decision,scan_summary,supersedes_version_id,
                               payload_checksum,submitted_at,reviewed_at,
                               reviewed_by_user_id,review_note,created_by_user_id,
                               created_at,revoked_at,revoked_by_user_id,
                               revocation_reason_code)
                            SELECT :canonical_id,skill_id,1,workflow_status,description,
                                   frontmatter,compatibility,secret_requirements,
                                   scan_decision,scan_summary,NULL,payload_checksum,
                                   submitted_at,reviewed_at,reviewed_by_user_id,
                                   review_note,created_by_user_id,created_at,revoked_at,
                                   revoked_by_user_id,revocation_reason_code
                            FROM skill_versions WHERE id = :current_id
                            """,
                        ),
                        {"canonical_id": canonical_id, "current_id": current_id},
                    )
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO skill_version_files
                              (skill_version_id,path,media_type,size_bytes,sha256,content)
                            SELECT :canonical_id,path,media_type,size_bytes,sha256,content
                            FROM skill_version_files
                            WHERE skill_version_id = :current_id
                            """,
                        ),
                        {"canonical_id": canonical_id, "current_id": current_id},
                    )
                    for skill_id_column, version_id_column in (
                        ("skill_creator_skill_id", "skill_creator_version_id"),
                        ("created_skill_id", "created_skill_version_id"),
                        ("target_skill_id", "base_version_id"),
                    ):
                        connection.execute(
                            sa.text(
                                f"""
                                UPDATE skill_design_sessions
                                SET {version_id_column} = :canonical_id
                                WHERE {skill_id_column} = :asset_id
                                  AND {version_id_column} = :current_id
                                """,
                            ),
                            {
                                "canonical_id": canonical_id,
                                "asset_id": row["id"],
                                "current_id": current_id,
                            },
                        )
                    connection.execute(
                        sa.text(
                            "UPDATE project_system_skill_bindings SET skill_version_id=:canonical_id WHERE system_skill_id=:asset_id",
                        ),
                        {"canonical_id": canonical_id, "asset_id": row["id"]},
                    )
                    connection.execute(
                        sa.text(
                            "UPDATE project_skill_credential_configs SET skill_version_id=:canonical_id WHERE skill_id=:asset_id AND skill_version_id=:current_id",
                        ),
                        {
                            "canonical_id": canonical_id,
                            "asset_id": row["id"],
                            "current_id": current_id,
                        },
                    )
                    connection.execute(
                        sa.text(
                            "DELETE FROM skill_version_files WHERE skill_version_id=:current_id",
                        ),
                        {"current_id": current_id},
                    )

                connection.execute(
                    sa.text(
                        f"UPDATE {asset_table} SET current_version_id=:canonical_id WHERE id=:asset_id",
                    ),
                    {"canonical_id": canonical_id, "asset_id": row["id"]},
                )
                connection.execute(
                    sa.text(
                        f"DELETE FROM {version_table} WHERE id=:current_id",
                    ),
                    {"current_id": current_id},
                )
            else:
                connection.execute(
                    sa.text(
                        f"UPDATE {version_table} SET version_number=1,supersedes_version_id=NULL WHERE id=:canonical_id",
                    ),
                    {"canonical_id": canonical_id},
                )

            if len(history) > 1 or current_id != canonical_id:
                current_checksum = next(item["payload_checksum"] for item in history if item["id"] == current_id)
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO system_asset_upgrade_audit
                          (id,asset_kind,asset_id,version_id,before_checksum,
                           after_checksum,package_digest,operator_identity)
                        VALUES
                          (:id,:kind,:asset_id,:version_id,:checksum,:checksum,
                           :package_digest,:operator_identity)
                        """,
                    ),
                    {
                        "id": uuid.uuid4(),
                        "kind": "agent" if version_table == "agent_versions" else "skill",
                        "asset_id": row["id"],
                        "version_id": canonical_id,
                        "checksum": current_checksum,
                        "package_digest": _canonical_digest(
                            [
                                {
                                    "id": str(item["id"]),
                                    "version_number": item["version_number"],
                                    "payload_checksum": item["payload_checksum"],
                                }
                                for item in history
                            ],
                        ),
                        "operator_identity": "schema-migration:current-asset-version-lifecycle",
                    },
                )

    connection.execute(
        sa.text(
            """
            ALTER TABLE project_skill_credential_configs
            ADD CONSTRAINT fk_project_skill_credential_configs_skill_version
            FOREIGN KEY(skill_id,skill_version_id)
            REFERENCES skill_versions (skill_id,id) ON DELETE CASCADE
            """,
        )
    )
    connection.execute(
        sa.text(
            """
            ALTER TABLE project_skill_credential_bindings
            ADD CONSTRAINT fk_project_skill_credential_bindings_skill
            FOREIGN KEY(skill_id) REFERENCES skills (id) ON DELETE CASCADE
            """,
        )
    )


def _already_has_current_lifecycle(connection) -> bool:
    """Recognize the consolidated install catalog after a marker-only drill.

    Fresh installs are stamped at the head and never execute this migration.
    Migration parity tests deliberately restore an older Alembic marker on top
    of the consolidated catalog so earlier migrations can be exercised.  Once
    those earlier migrations finish, this exact structural check prevents the
    head migration from trying to add the same columns a second time.  A
    partially converted production catalog does not satisfy all predicates and
    therefore still follows the fail-closed migration path below.
    """

    inspector = sa.inspect(connection)
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    skill_columns = {column["name"] for column in inspector.get_columns("skills")}
    agent_version_columns = {column["name"] for column in inspector.get_columns("agent_versions")}
    skill_version_columns = {column["name"] for column in inspector.get_columns("skill_versions")}
    ref_columns = {column["name"] for column in inspector.get_columns("agent_version_skill_refs")}
    run_asset_columns = {column["name"] for column in inspector.get_columns("run_asset_versions")}
    skill_binding_columns = {
        column["name"]
        for column in inspector.get_columns(
            "project_skill_credential_bindings",
        )
    }
    return (
        {"current_version_id", "revision"} <= agent_columns
        and "current_published_version_id" not in agent_columns
        and {"current_version_id", "revision"} <= skill_columns
        and "current_published_version_id" not in skill_columns
        and "workflow_status" not in agent_version_columns
        and "workflow_status" not in skill_version_columns
        and {"skill_asset_scope", "skill_asset_id"} <= ref_columns
        and "skill_version_id" not in ref_columns
        and "snapshot_json" in run_asset_columns
        and {
            "admission_only",
            "runtime_authority_binding_id",
        }
        <= skill_binding_columns
        and inspector.has_table("system_asset_upgrade_audit")
    )


def _create_system_asset_upgrade_audit() -> None:
    op.create_table(
        "system_asset_upgrade_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_kind", sa.String(length=16), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("before_checksum", sa.String(length=64), nullable=False),
        sa.Column("after_checksum", sa.String(length=64), nullable=False),
        sa.Column("package_digest", sa.String(length=64), nullable=False),
        sa.Column("operator_identity", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("asset_kind IN ('agent', 'skill')", name="ck_system_asset_upgrade_audit_kind"),
        sa.CheckConstraint("before_checksum ~ '^[0-9a-f]{64}$'", name="ck_system_asset_upgrade_audit_before_checksum"),
        sa.CheckConstraint("after_checksum ~ '^[0-9a-f]{64}$'", name="ck_system_asset_upgrade_audit_after_checksum"),
        sa.CheckConstraint("package_digest ~ '^[0-9a-f]{64}$'", name="ck_system_asset_upgrade_audit_package_digest"),
    )


def upgrade() -> None:
    connection = op.get_bind()
    if _already_has_current_lifecycle(connection):
        return
    _preflight_legacy_lifecycle(connection)
    _drop_legacy_triggers(connection)

    op.add_column("run_asset_versions", sa.Column("snapshot_json", sa.dialects.postgresql.JSONB(), nullable=True))
    _backfill_run_snapshots(connection)
    op.alter_column("run_asset_versions", "snapshot_json", nullable=False)

    _migrate_blueprints(connection)
    op.add_column(
        "project_skill_credential_bindings",
        sa.Column(
            "admission_only",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "project_skill_credential_bindings",
        sa.Column(
            "runtime_authority_binding_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_project_skill_credential_bindings_runtime_authority",
        "project_skill_credential_bindings",
        "runtime_authority_binding_id IS NULL OR admission_only = true",
    )
    op.create_foreign_key(
        "fk_project_skill_credential_bindings_runtime_authority",
        "project_skill_credential_bindings",
        "project_skill_credential_bindings",
        ["runtime_authority_binding_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_index(
        "uq_project_skill_credential_bindings_active_name",
        table_name="project_skill_credential_bindings",
    )
    op.drop_constraint("ck_agent_versions_payload_schema_version", "agent_versions", type_="check")
    op.drop_constraint("ck_agent_versions_model_settings", "agent_versions", type_="check")

    op.add_column("agent_version_skill_refs", sa.Column("skill_asset_scope", sa.String(length=16), nullable=True))
    op.add_column("agent_version_skill_refs", sa.Column("skill_asset_id", sa.Uuid(), nullable=True))
    op.execute(
        """
        UPDATE agent_version_skill_refs ref
        SET skill_asset_scope = skill.scope,
            skill_asset_id = skill.id
        FROM skill_versions version
        JOIN skills skill ON skill.id = version.skill_id
        WHERE version.id = ref.skill_version_id
        """,
    )
    op.alter_column("agent_version_skill_refs", "skill_asset_scope", nullable=False)
    op.alter_column("agent_version_skill_refs", "skill_asset_id", nullable=False)
    op.drop_constraint("agent_version_skill_refs_skill_version_id_fkey", "agent_version_skill_refs", type_="foreignkey")
    op.drop_constraint("agent_version_skill_refs_pkey", "agent_version_skill_refs", type_="primary")
    op.drop_column("agent_version_skill_refs", "skill_version_id")
    op.create_primary_key(
        "agent_version_skill_refs_pkey",
        "agent_version_skill_refs",
        ["agent_version_id", "skill_asset_scope", "skill_asset_id"],
    )
    op.create_check_constraint(
        "ck_agent_version_skill_refs_scope",
        "agent_version_skill_refs",
        "skill_asset_scope IN ('system', 'project')",
    )
    op.create_foreign_key(
        "fk_agent_version_skill_refs_skill_asset",
        "agent_version_skill_refs",
        "skills",
        ["skill_asset_id", "skill_asset_scope"],
        ["id", "scope"],
        ondelete="RESTRICT",
    )

    for table_name, old_pointer, new_pointer, old_revision, new_revision, old_fk, new_fk, version_table, parent_column in (
        ("agents", "current_published_version_id", "current_version_id", "version", "revision", "fk_agents_current_published_version", "fk_agents_current_version", "agent_versions", "agent_id"),
        ("skills", "current_published_version_id", "current_version_id", "version", "revision", "fk_skills_current_published_version", "fk_skills_current_version", "skill_versions", "skill_id"),
    ):
        op.drop_constraint(old_fk, table_name, type_="foreignkey")
        op.alter_column(table_name, old_pointer, new_column_name=new_pointer)
        op.alter_column(table_name, old_revision, new_column_name=new_revision)
        op.drop_constraint(f"ck_{table_name}_version", table_name, type_="check")
        op.create_check_constraint(f"ck_{table_name}_revision", table_name, f"{new_revision} >= 1")
        op.create_foreign_key(
            new_fk,
            table_name,
            version_table,
            ["id", new_pointer],
            [parent_column, "id"],
        )

    _create_system_asset_upgrade_audit()
    _normalize_system_assets(connection)
    op.create_index(
        "uq_project_skill_credential_bindings_active_name",
        "project_skill_credential_bindings",
        ["project_id", "skill_id", "skill_version_id", "secret_name"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND admission_only = false",
        ),
    )

    op.execute(
        "UPDATE agent_versions SET supersedes_version_id = NULL WHERE workflow_status IN ('pending_approval', 'rejected')",
    )
    op.execute(
        "UPDATE skill_versions SET supersedes_version_id = NULL WHERE workflow_status IN ('pending_approval', 'rejected')",
    )
    for table_name in ("agent_versions", "skill_versions"):
        op.drop_constraint(f"ck_{table_name}_workflow_status", table_name, type_="check")
        op.drop_constraint(f"{table_name}_reviewed_by_user_id_fkey", table_name, type_="foreignkey")
        for column_name in (
            "workflow_status",
            "submitted_at",
            "reviewed_at",
            "reviewed_by_user_id",
            "review_note",
        ):
            op.drop_column(table_name, column_name)

    op.create_check_constraint(
        "ck_agent_versions_payload_schema_version",
        "agent_versions",
        "payload_schema_version IN (1, 2, 3, 4)",
    )
    op.create_check_constraint(
        "ck_agent_versions_model_settings",
        "agent_versions",
        """
        jsonb_typeof(model_settings) = 'object'
        AND (payload_schema_version IN (3, 4) OR model_settings = '{}'::jsonb)
        AND model_settings - 'temperature' - 'max_tokens' - 'thinking_enabled' - 'reasoning_effort' = '{}'::jsonb
        AND (NOT (model_settings ? 'temperature') OR (jsonb_typeof(model_settings->'temperature') = 'number' AND (model_settings->>'temperature')::numeric BETWEEN 0 AND 2))
        AND (
            NOT (model_settings ? 'max_tokens')
            OR (
                jsonb_typeof(model_settings->'max_tokens') = 'number'
                AND (model_settings->>'max_tokens')::numeric = trunc((model_settings->>'max_tokens')::numeric)
                AND (model_settings->>'max_tokens')::numeric BETWEEN 1 AND 200000
            )
        )
        AND (NOT (model_settings ? 'thinking_enabled') OR jsonb_typeof(model_settings->'thinking_enabled') = 'boolean')
        AND (NOT (model_settings ? 'reasoning_effort') OR (jsonb_typeof(model_settings->'reasoning_effort') = 'string' AND model_settings->>'reasoning_effort' IN ('low', 'medium', 'high')))
        """,
    )

    for table_name, fk_name, column_name in (
        ("project_system_agent_bindings", "fk_project_system_agent_bindings_version", "agent_version_id"),
        ("project_system_skill_bindings", "fk_project_system_skill_bindings_version", "skill_version_id"),
    ):
        op.drop_constraint(fk_name, table_name, type_="foreignkey")
        op.drop_column(table_name, column_name)

    _install_current_comments(connection)
    _install_current_triggers(connection)


def _install_current_triggers(connection) -> None:
    # Keep this release DDL explicit: migrations must never import mutable ORM
    # metadata. The function bodies match the chain-head consolidated schema.
    for statement in _CURRENT_TRIGGER_DDL:
        connection.execute(sa.text(statement))


def _install_current_comments(connection) -> None:
    for statement in (
        "COMMENT ON TABLE agents IS '保存智能体的逻辑身份和 Current Version 指针。'",
        "COMMENT ON COLUMN agents.current_version_id IS '项目智能体：当前版本标识。'",
        "COMMENT ON COLUMN agents.revision IS '项目智能体：配置修订号。'",
        "COMMENT ON TABLE skills IS '保存技能的逻辑身份和 Current Version 指针。'",
        "COMMENT ON COLUMN skills.current_version_id IS '项目技能：当前版本标识。'",
        "COMMENT ON COLUMN skills.revision IS '项目技能：配置修订号。'",
        "COMMENT ON TABLE agent_version_skill_refs IS '保存智能体版本到技能资产的有序依赖；运行时解析其 Current Version。'",
        "COMMENT ON COLUMN agent_version_skill_refs.skill_asset_scope IS '智能体技能引用：技能资产范围。'",
        "COMMENT ON COLUMN agent_version_skill_refs.skill_asset_id IS '智能体技能引用：技能资产标识。'",
        "COMMENT ON TABLE run_asset_versions IS '冻结一次运行准入时解析出的智能体、技能或 MCP 完整版本内容。'",
        "COMMENT ON COLUMN run_asset_versions.snapshot_json IS '运行资产快照：准入时冻结的完整且不含明文凭据的资产内容。'",
        "COMMENT ON COLUMN project_skill_credential_bindings.admission_only IS '技能凭据绑定：仅供已准入运行继续验证的退役权限标记。'",
        "COMMENT ON COLUMN project_skill_credential_bindings.runtime_authority_binding_id IS '技能凭据绑定：退役绑定关联的当前运行权限绑定标识。'",
        "COMMENT ON TABLE system_asset_upgrade_audit IS '记录软件包升级原子替换 System Agent 或 Skill Current v1 的校验和证据。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.id IS '系统资产升级审计：主键标识。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.asset_kind IS '系统资产升级审计：资产类型。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.asset_id IS '系统资产升级审计：资产标识。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.version_id IS '系统资产升级审计：版本标识。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.before_checksum IS '系统资产升级审计：升级前载荷校验和。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.after_checksum IS '系统资产升级审计：升级后载荷校验和。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.package_digest IS '系统资产升级审计：升级软件包目录摘要。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.operator_identity IS '系统资产升级审计：执行数据库升级的操作主体身份。'",
        "COMMENT ON COLUMN system_asset_upgrade_audit.occurred_at IS '系统资产升级审计：发生时间。'",
    ):
        connection.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )


_CURRENT_TRIGGER_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION enforce_live_skill_credential_binding_target()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.admission_only = false AND NOT EXISTS (
            SELECT 1
            FROM project_skill_credential_configs config
            JOIN skill_versions version
              ON version.skill_id = config.skill_id
             AND version.id = config.skill_version_id
            WHERE config.project_id = NEW.project_id
              AND config.skill_id = NEW.skill_id
              AND config.skill_version_id = NEW.skill_version_id
        ) THEN
            RAISE EXCEPTION 'live Skill credential binding target unavailable'
                USING ERRCODE = '23503';
        END IF;
        IF NEW.runtime_authority_binding_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM project_skill_credential_bindings authority
            WHERE authority.id = NEW.runtime_authority_binding_id
              AND authority.project_id = NEW.project_id
              AND authority.skill_id = NEW.skill_id
              AND authority.secret_name = NEW.secret_name
              AND authority.admission_only = false
        ) THEN
            RAISE EXCEPTION 'retired Skill credential runtime authority unavailable'
                USING ERRCODE = '23503';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION protect_live_skill_credential_binding_target()
    RETURNS trigger AS $$
    BEGIN
        IF TG_TABLE_NAME = 'project_skill_credential_configs' THEN
            IF EXISTS (
                SELECT 1 FROM project_skill_credential_bindings binding
                WHERE binding.project_id = OLD.project_id
                  AND binding.skill_id = OLD.skill_id
                  AND binding.skill_version_id = OLD.skill_version_id
                  AND binding.admission_only = false
            ) THEN
                RAISE EXCEPTION 'live Skill credential config is referenced'
                    USING ERRCODE = '23503';
            END IF;
        ELSIF EXISTS (
            SELECT 1 FROM project_skill_credential_bindings binding
            WHERE binding.skill_id = OLD.skill_id
              AND binding.skill_version_id = OLD.id
              AND binding.admission_only = false
        ) THEN
            RAISE EXCEPTION 'live Skill credential version is referenced'
                USING ERRCODE = '23503';
        END IF;
        RETURN OLD;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER trg_project_skill_credential_bindings_live_target
    BEFORE INSERT OR UPDATE OF project_id,skill_id,skill_version_id,secret_name,admission_only,runtime_authority_binding_id
    ON project_skill_credential_bindings
    FOR EACH ROW EXECUTE FUNCTION enforce_live_skill_credential_binding_target()
    """,
    """
    CREATE TRIGGER trg_project_skill_credential_configs_live_binding
    BEFORE DELETE OR UPDATE OF project_id,skill_id,skill_version_id
    ON project_skill_credential_configs
    FOR EACH ROW EXECUTE FUNCTION protect_live_skill_credential_binding_target()
    """,
    """
    CREATE TRIGGER trg_skill_versions_live_credential_binding
    BEFORE DELETE OR UPDATE OF id,skill_id
    ON skill_versions
    FOR EACH ROW EXECUTE FUNCTION protect_live_skill_credential_binding_target()
    """,
    """
    CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
    RETURNS trigger AS $$
    DECLARE
        asset_scope text;
    BEGIN
        IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
           AND TG_TABLE_NAME IN ('agent_versions', 'skill_versions') THEN
            IF TG_TABLE_NAME = 'agent_versions' THEN
                SELECT scope INTO asset_scope FROM agents WHERE id = NEW.agent_id;
            ELSE
                SELECT scope INTO asset_scope FROM skills WHERE id = NEW.skill_id;
            END IF;
            IF asset_scope = 'system' THEN
                RETURN NEW;
            END IF;
        END IF;
        IF (to_jsonb(NEW) - ARRAY[
            'workflow_status', 'status', 'submitted_at', 'reviewed_at',
            'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
            'revoked_by_user_id', 'revocation_reason_code'
        ]::text[]) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY[
            'workflow_status', 'status', 'submitted_at', 'reviewed_at',
            'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
            'revoked_by_user_id', 'revocation_reason_code'
        ]::text[]) THEN
            RAISE EXCEPTION 'shared asset version payload is immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION bump_asset_catalog_generation()
    RETURNS trigger AS $$
    BEGIN
        INSERT INTO asset_catalog_state (id, generation, updated_at)
        VALUES (1, 1, now())
        ON CONFLICT (id) DO UPDATE
          SET generation = asset_catalog_state.generation + 1,
              updated_at = now();
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION ensure_system_binding_eligible_version()
    RETURNS trigger AS $$
    DECLARE
        version_revoked_at timestamp with time zone;
        current_id uuid;
        asset_status text;
        version_status text;
    BEGIN
        CASE TG_TABLE_NAME
            WHEN 'project_system_agent_bindings' THEN
                SELECT current_version_id, status INTO current_id, asset_status
                FROM agents
                WHERE id = NEW.system_agent_id AND scope = 'system'
                FOR UPDATE;
            WHEN 'project_system_skill_bindings' THEN
                SELECT current_version_id, status INTO current_id, asset_status
                FROM skills
                WHERE id = NEW.system_skill_id AND scope = 'system'
                FOR UPDATE;
                SELECT revoked_at INTO version_revoked_at
                FROM skill_versions
                WHERE id = current_id AND skill_id = NEW.system_skill_id
                FOR UPDATE;
                IF TG_OP = 'UPDATE'
                   AND OLD.enabled IS TRUE
                   AND NEW.enabled IS FALSE
                   AND OLD.system_skill_id = NEW.system_skill_id THEN
                    RETURN NEW;
                END IF;
            WHEN 'project_system_mcp_bindings' THEN
                SELECT workflow_status INTO version_status
                FROM mcp_server_versions
                WHERE id = NEW.mcp_server_version_id
                  AND mcp_server_id = NEW.system_mcp_server_id
                FOR UPDATE;
            ELSE
                RAISE EXCEPTION 'unsupported system binding table';
        END CASE;
        IF TG_TABLE_NAME = 'project_system_mcp_bindings'
           AND version_status IS DISTINCT FROM 'published' THEN
            RAISE EXCEPTION 'system MCP binding requires a published version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_TABLE_NAME IN ('project_system_agent_bindings', 'project_system_skill_bindings')
           AND (current_id IS NULL OR asset_status IS DISTINCT FROM 'active'
           OR (TG_TABLE_NAME = 'project_system_skill_bindings'
               AND version_revoked_at IS NOT NULL)) THEN
            RAISE EXCEPTION 'system binding requires an eligible Current Version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION enforce_system_skill_version_revocation()
    RETURNS trigger AS $$
    DECLARE
        asset_scope text;
        asset_project_id uuid;
        current_id uuid;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.revoked_at IS NOT NULL
               OR NEW.revoked_by_user_id IS NOT NULL
               OR NEW.revocation_reason_code IS NOT NULL THEN
                RAISE EXCEPTION 'skill version must be created unrevoked'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END IF;

        IF NEW.revoked_at IS NOT DISTINCT FROM OLD.revoked_at
           AND NEW.revoked_by_user_id IS NOT DISTINCT FROM OLD.revoked_by_user_id
           AND NEW.revocation_reason_code IS NOT DISTINCT FROM OLD.revocation_reason_code THEN
            RETURN NEW;
        END IF;

        IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
           AND OLD.revoked_at IS NOT NULL
           AND NEW.revoked_at IS NULL
           AND NEW.revoked_by_user_id IS NULL
           AND NEW.revocation_reason_code IS NULL THEN
            SELECT scope, project_id, current_version_id
            INTO asset_scope, asset_project_id, current_id
            FROM skills
            WHERE id = NEW.skill_id
            FOR UPDATE;
            IF asset_scope = 'system'
               AND asset_project_id IS NULL
               AND current_id = NEW.id
               AND NEW.version_number = 1 THEN
                RETURN NEW;
            END IF;
        END IF;
        IF OLD.revoked_at IS NOT NULL
           OR OLD.revoked_by_user_id IS NOT NULL
           OR OLD.revocation_reason_code IS NOT NULL
           OR NEW.revoked_at IS NULL
           OR NEW.revoked_by_user_id IS NULL
           OR NEW.revocation_reason_code IS NULL
           OR NEW.revocation_reason_code NOT IN ('security', 'policy', 'integrity') THEN
            RAISE EXCEPTION 'system skill version revocation is irreversible'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        SELECT scope, project_id, current_version_id
        INTO asset_scope, asset_project_id, current_id
        FROM skills
        WHERE id = NEW.skill_id
        FOR UPDATE;
        IF asset_scope IS DISTINCT FROM 'system'
           OR asset_project_id IS NOT NULL
           OR current_id IS DISTINCT FROM NEW.id
           OR NEW.version_number != 1 THEN
            RAISE EXCEPTION 'only a System Skill Current v1 can be revoked'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION prevent_bound_mcp_published_version_downgrade()
    RETURNS trigger AS $$
    DECLARE
        is_bound boolean;
    BEGIN
        IF OLD.workflow_status = 'published'
           AND NEW.workflow_status IS DISTINCT FROM 'published' THEN
            SELECT EXISTS (
                SELECT 1 FROM project_system_mcp_bindings
                WHERE mcp_server_version_id = OLD.id
            ) INTO is_bound;
            IF is_bound THEN
                RAISE EXCEPTION 'bound published version cannot change workflow status'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION prevent_asset_version_child_mutation()
    RETURNS trigger AS $$
    DECLARE
        parent_version_id uuid;
        parent_status text;
        parent_scope text;
        parent_project_id uuid;
        parent_asset_id uuid;
        purge_allowed boolean := false;
    BEGIN
        CASE TG_TABLE_NAME
            WHEN 'skill_version_files' THEN
                parent_version_id := CASE WHEN TG_OP = 'DELETE'
                    THEN OLD.skill_version_id ELSE NEW.skill_version_id END;
                SELECT asset.scope, asset.project_id, asset.id
                INTO parent_scope, parent_project_id, parent_asset_id
                FROM skill_versions version
                JOIN skills asset ON asset.id = version.skill_id
                WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
                IF TG_OP = 'DELETE' THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM skill_versions version
                        JOIN skills asset ON asset.id = version.skill_id
                        JOIN projects project ON project.id = asset.project_id
                        WHERE version.id = OLD.skill_version_id
                          AND asset.scope = 'project'
                          AND (
                              (
                                  project.status = 'pending_deletion'
                                  AND project.deletion_effective_at IS NOT NULL
                                  AND project.deletion_effective_at <= now()
                              )
                              OR (
                                  project.status = 'active'
                                  AND project.is_suspended IS FALSE
                                  AND asset.status = 'archived'
                                  AND asset.current_version_id IS NULL
                                  AND current_setting(
                                      'deerflow.skill_hard_delete_asset_id',
                                      true
                                  ) = asset.id::text
                              )
                          )
                    ) INTO purge_allowed;
                END IF;
            WHEN 'agent_version_skill_refs' THEN
                parent_version_id := CASE WHEN TG_OP = 'DELETE'
                    THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
                SELECT asset.scope, asset.project_id, asset.id
                INTO parent_scope, parent_project_id, parent_asset_id
                FROM agent_versions version
                JOIN agents asset ON asset.id = version.agent_id
                WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
                IF TG_OP = 'DELETE' THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM agent_versions version
                        JOIN agents asset ON asset.id = version.agent_id
                        JOIN projects project ON project.id = asset.project_id
                        WHERE version.id = OLD.agent_version_id
                          AND asset.scope = 'project'
                          AND (
                              (
                                  project.status = 'pending_deletion'
                                  AND project.deletion_effective_at IS NOT NULL
                                  AND project.deletion_effective_at <= now()
                              )
                              OR (
                                  project.status = 'active'
                                  AND project.is_suspended IS FALSE
                                  AND asset.status = 'archived'
                                  AND asset.current_version_id IS NULL
                                  AND current_setting(
                                      'deerflow.agent_hard_delete_asset_id',
                                      true
                                  ) = asset.id::text
                              )
                          )
                    ) INTO purge_allowed;
                END IF;
            WHEN 'agent_version_mcp_refs' THEN
                parent_version_id := CASE WHEN TG_OP = 'DELETE'
                    THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
                SELECT asset.scope, asset.project_id, asset.id
                INTO parent_scope, parent_project_id, parent_asset_id
                FROM agent_versions version
                JOIN agents asset ON asset.id = version.agent_id
                WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
                IF TG_OP = 'DELETE' THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM agent_versions version
                        JOIN agents asset ON asset.id = version.agent_id
                        JOIN projects project ON project.id = asset.project_id
                        WHERE version.id = OLD.agent_version_id
                          AND asset.scope = 'project'
                          AND (
                              (
                                  project.status = 'pending_deletion'
                                  AND project.deletion_effective_at IS NOT NULL
                                  AND project.deletion_effective_at <= now()
                              )
                              OR (
                                  project.status = 'active'
                                  AND project.is_suspended IS FALSE
                                  AND asset.status = 'archived'
                                  AND asset.current_version_id IS NULL
                                  AND current_setting(
                                      'deerflow.agent_hard_delete_asset_id',
                                      true
                                  ) = asset.id::text
                              )
                          )
                    ) OR EXISTS (
                        SELECT 1
                        FROM mcp_server_versions version
                        JOIN mcp_servers asset ON asset.id = version.mcp_server_id
                        JOIN projects project ON project.id = asset.project_id
                        WHERE version.id = OLD.mcp_server_version_id
                          AND asset.scope = 'project'
                          AND project.status = 'pending_deletion'
                          AND project.deletion_effective_at IS NOT NULL
                          AND project.deletion_effective_at <= now()
                    ) INTO purge_allowed;
                END IF;
            WHEN 'mcp_version_credential_slots' THEN
                parent_version_id := CASE WHEN TG_OP = 'DELETE'
                    THEN OLD.mcp_server_version_id ELSE NEW.mcp_server_version_id END;
                SELECT workflow_status INTO parent_status
                FROM mcp_server_versions WHERE id = parent_version_id FOR UPDATE;
                IF TG_OP = 'DELETE' THEN
                    SELECT EXISTS (
                        SELECT 1
                        FROM mcp_server_versions version
                        JOIN mcp_servers asset ON asset.id = version.mcp_server_id
                        JOIN projects project ON project.id = asset.project_id
                        WHERE version.id = OLD.mcp_server_version_id
                          AND asset.scope = 'project'
                          AND (
                              (
                                  project.status = 'pending_deletion'
                                  AND project.deletion_effective_at IS NOT NULL
                                  AND project.deletion_effective_at <= now()
                              )
                              OR (
                                  project.status = 'active'
                                  AND project.is_suspended IS FALSE
                                  AND asset.status = 'archived'
                                  AND asset.current_published_version_id IS NULL
                                  AND current_setting(
                                      'deerflow.mcp_hard_delete_asset_id',
                                      true
                                  ) = asset.id::text
                              )
                          )
                    ) INTO purge_allowed;
                END IF;
            ELSE
                RAISE EXCEPTION 'unsupported version child table';
        END CASE;
        IF TG_TABLE_NAME IN (
            'skill_version_files',
            'agent_version_skill_refs',
            'agent_version_mcp_refs'
        ) THEN
            IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
               AND parent_scope = 'system' THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF current_setting('deerflow.asset_version_assembly', true) = parent_version_id::text THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' AND purge_allowed THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'Agent and Skill version child rows are immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'DELETE' AND purge_allowed THEN
            RETURN OLD;
        END IF;
        IF parent_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'published version child rows are immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION enforce_shared_asset_version_state_transition()
    RETURNS trigger AS $$
    BEGIN
        IF TG_TABLE_NAME = 'credential_versions' THEN
            IF NEW.status = OLD.status
               OR (OLD.status = 'active' AND NEW.status IN ('retired', 'revoked'))
               OR (OLD.status = 'retired' AND NEW.status = 'revoked') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid credential version status transition'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.workflow_status = OLD.workflow_status
           OR (OLD.workflow_status = 'draft' AND NEW.workflow_status IN ('pending_approval', 'published'))
           OR (OLD.workflow_status = 'pending_approval' AND NEW.workflow_status IN ('published', 'rejected')) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'invalid shared asset version workflow transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "CREATE TRIGGER trg_agent_versions_immutable BEFORE UPDATE ON agent_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_versions_immutable BEFORE UPDATE ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_versions_revocation BEFORE INSERT OR UPDATE OF revoked_at, revoked_by_user_id, revocation_reason_code ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_system_skill_version_revocation()",
    "CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_credential_versions_immutable BEFORE UPDATE ON credential_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_skill_refs_immutable BEFORE UPDATE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_mcp_refs_immutable BEFORE UPDATE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_credential_slots_immutable BEFORE UPDATE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_bindings_current BEFORE INSERT OR UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version()",
    "CREATE TRIGGER trg_skill_bindings_current BEFORE INSERT OR UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version()",
    "CREATE TRIGGER trg_mcp_bindings_published BEFORE INSERT OR UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version()",
    "CREATE TRIGGER trg_mcp_server_versions_bound_published BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_mcp_published_version_downgrade()",
    "CREATE TRIGGER trg_skill_version_files_child_immutable BEFORE INSERT OR DELETE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation()",
    "CREATE TRIGGER trg_agent_version_skill_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation()",
    "CREATE TRIGGER trg_agent_version_mcp_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation()",
    "CREATE TRIGGER trg_mcp_credential_slots_child_immutable BEFORE INSERT OR DELETE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation()",
    "CREATE TRIGGER trg_mcp_server_versions_state_transition BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition()",
    "CREATE TRIGGER trg_credential_versions_state_transition BEFORE UPDATE OF status ON credential_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition()",
    "CREATE TRIGGER trg_agents_generation AFTER UPDATE OF status, current_version_id, revision ON agents FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skills_generation AFTER UPDATE OF status, current_version_id, revision ON skills FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_servers_generation AFTER UPDATE OF status, current_published_version_id ON mcp_servers FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_version_revocations_generation AFTER UPDATE OF revoked_at ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_server_versions_generation AFTER UPDATE OF workflow_status ON mcp_server_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credentials_generation AFTER UPDATE OF status, current_version_id, is_delete ON credentials FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credential_versions_generation AFTER UPDATE OF status ON credential_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_agent_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_agent_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_skill_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_mcp_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credential_grants_generation AFTER INSERT OR UPDATE OR DELETE ON credential_grants FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
)
