"""Pure Run Snapshot admission rules: encoding budget, key rejection, recursion clamp, closure validators."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkConflict, PrivateWorkTooLarge
from app.private_work.legacy_run_skill_snapshot_writer import PreparedLegacyRunSkillSnapshots
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRecord
from app.private_work.snapshot_contracts import RunSnapshotAssetStale
from app.shared_assets.errors import SharedAssetError
from app.shared_assets.mcp_secret_closure import McpSecretClosure
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedAssetSnapshot,
    ResolvedMcpSnapshot,
    ResolvedRunAssetClosure,
    ResolvedSkillVersionSnapshot,
)
from app.shared_assets.run_snapshot_codec import (
    MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES,
    MAX_RUN_SKILL_REFERENCE_MANIFEST_JSON_BYTES,
    RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
    RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
    RunAssetSnapshotTooLarge,
    encode_run_asset_snapshot,
    encode_run_skill_version_manifest,
    encoded_run_asset_snapshot_json_size,
)
from app.shared_assets.skill_secret_policy import parse_skill_secret_declarations
from app.system_runtime_settings.models import LockedAgentRuntimePolicy
from deerflow.mcp_definition_policy import McpDefinitionPolicyError, McpEndpointPolicy, validate_project_mcp_definition
from deerflow.persistence.private_work.model import RunAssetVersionRow, RunSkillVersionRefRow
from deerflow.persistence.shared_assets.mcp_model import McpServerRow, McpServerVersionRow
from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow

_FORBIDDEN_PERSISTED_KEY_PARTS = (
    "secret",
    "envelope",
    "key_id",
    "nonce",
    "ciphertext",
    "storage_locator",
)


@dataclass(slots=True)
class _RunAssetSnapshotAdmissionEncoder:
    request_id: str
    encoded_json_bytes: int = 0

    def encode(self, snapshot: ResolvedAssetSnapshot) -> dict[str, object]:
        try:
            encoded = encode_run_asset_snapshot(snapshot)
        except RunAssetSnapshotTooLarge:
            raise PrivateWorkTooLarge(self.request_id) from None
        self.encoded_json_bytes += encoded_run_asset_snapshot_json_size(encoded)
        if self.encoded_json_bytes > MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES:
            raise PrivateWorkTooLarge(self.request_id)
        return encoded

    def encode_skill_version(
        self,
        snapshot: ResolvedSkillVersionSnapshot,
    ) -> dict[str, object]:
        try:
            encoded = encode_run_skill_version_manifest(snapshot)
        except RunAssetSnapshotTooLarge:
            raise PrivateWorkTooLarge(self.request_id) from None
        encoded_size = encoded_run_asset_snapshot_json_size(encoded)
        if encoded_size > MAX_RUN_SKILL_REFERENCE_MANIFEST_JSON_BYTES:
            raise PrivateWorkTooLarge(self.request_id)
        self.encoded_json_bytes += encoded_size
        if self.encoded_json_bytes > MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES:
            raise PrivateWorkTooLarge(self.request_id)
        return encoded

    def include_legacy_skill(
        self,
        encoded: Mapping[str, object],
    ) -> dict[str, object]:
        if encoded.get("schema_version") != RUN_ASSET_SNAPSHOT_SCHEMA_VERSION or encoded.get("kind") != AssetKind.SKILL.value:
            raise PrivateWorkTooLarge(self.request_id)
        copied = dict(encoded)
        self.encoded_json_bytes += encoded_run_asset_snapshot_json_size(copied)
        if self.encoded_json_bytes > MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES:
            raise PrivateWorkTooLarge(self.request_id)
        return copied


def _r1_snapshot_schema_version(
    snapshot_json: Mapping[str, object],
) -> int:
    schema_version = snapshot_json.get("schema_version")
    if type(schema_version) is not int or schema_version != RUN_ASSET_SNAPSHOT_SCHEMA_VERSION:
        raise RunSnapshotAssetStale
    return schema_version


def _apply_runtime_recursion_limit(
    request: PrivateRunCreate,
    policy: LockedAgentRuntimePolicy,
) -> PrivateRunCreate:
    kwargs = dict(request.kwargs)
    raw_config = kwargs.get("config")
    config = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    requested = config.get("recursion_limit", 100)
    if type(requested) is not int or requested <= 0:
        requested = 100
    config["recursion_limit"] = min(
        requested,
        policy.value.max_recursion_limit,
    )
    kwargs["config"] = config
    return replace(request, kwargs=kwargs)


def _reject_secret_bearing_keys(value: object, request_id: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_PERSISTED_KEY_PARTS):
                raise PrivateWorkConflict(request_id)
            _reject_secret_bearing_keys(item, request_id)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_bearing_keys(item, request_id)


def asset_allowed(
    *,
    asset_scope: str,
    asset_project_id: uuid.UUID | None,
    project_id: uuid.UUID,
) -> bool:
    return (asset_scope == AssetScope.SYSTEM.value and asset_project_id is None) or (asset_scope == AssetScope.PROJECT.value and asset_project_id == project_id)


def validate_project_mcp_secret_slots(
    mcps: list[tuple[McpServerRow, McpServerVersionRow]],
    closures: Mapping[uuid.UUID, McpSecretClosure],
    *,
    endpoint_policy: McpEndpointPolicy | None,
) -> None:
    """Validate locked secret-slot schemas before admitting work."""

    for asset, version in mcps:
        if asset.scope != AssetScope.PROJECT.value:
            continue
        try:
            closure = closures[uuid.UUID(str(version.id))]
            validate_project_mcp_definition(
                transport=version.transport,
                url=version.url,
                env=version.non_secret_env,
                headers=version.non_secret_headers,
                oauth=version.oauth_metadata,
                secret_slot_schemas=tuple(slot.payload_schema for slot in closure.slots),
                endpoint_policy=endpoint_policy,
            )
        except (
            AttributeError,
            KeyError,
            McpDefinitionPolicyError,
            TypeError,
            ValueError,
        ):
            raise RunSnapshotAssetStale from None


def validate_dependency_snapshots(
    rows: list[tuple[object, object]],
    snapshots: tuple[object, ...],
    *,
    catalog_generation: int,
) -> None:
    if len(rows) != len(snapshots):
        raise RunSnapshotAssetStale
    for (asset, version), snapshot in zip(rows, snapshots, strict=True):
        if (
            getattr(asset, "scope", None) != getattr(getattr(snapshot, "scope", None), "value", None)
            or getattr(asset, "id", None) != getattr(snapshot, "asset_id", None)
            or getattr(version, "id", None) != getattr(snapshot, "version_id", None)
            or getattr(version, "payload_checksum", None) != getattr(snapshot, "checksum", None)
            or getattr(snapshot, "catalog_generation", None) != catalog_generation
        ):
            raise RunSnapshotAssetStale
        if type(snapshot) is ResolvedSkillVersionSnapshot:
            try:
                requirements = tuple(
                    (
                        item.name,
                        item.target_env,
                        item.optional,
                    )
                    for item in parse_skill_secret_declarations(
                        version.secret_requirements,
                        request_id="run-snapshot-validation",
                    )
                )
            except SharedAssetError:
                raise RunSnapshotAssetStale from None
            if (
                version.files_sealed is not True
                or snapshot.file_count != version.file_count
                or snapshot.content_size_bytes != version.content_size_bytes
                or requirements
                != tuple(
                    (
                        item.name,
                        item.target_env,
                        item.optional,
                    )
                    for item in snapshot.secret_requirements
                )
            ):
                raise RunSnapshotAssetStale


def validate_main_dependency_boundary(
    closure: ResolvedRunAssetClosure,
    *,
    canonical_main: bool,
) -> None:
    skill_ids = tuple(item.version_id for item in closure.skills)
    mcp_ids = tuple(item.version_id for item in closure.mcps)
    main_skill_count = len(closure.main_skill_version_ids)
    main_mcp_count = len(closure.main_mcp_version_ids)
    if skill_ids[:main_skill_count] != closure.main_skill_version_ids or mcp_ids[:main_mcp_count] != closure.main_mcp_version_ids:
        raise RunSnapshotAssetStale
    if not canonical_main:
        if (
            closure.delegated_agents
            or skill_ids != closure.lead_agent.skill_version_ids
            or mcp_ids != closure.lead_agent.payload.mcp_version_ids
            or closure.main_skill_version_ids != skill_ids
            or closure.main_mcp_version_ids != mcp_ids
            or len({item.asset_id for item in closure.skills}) != len(closure.skills)
            or len({item.asset_id for item in closure.mcps}) != len(closure.mcps)
        ):
            raise RunSnapshotAssetStale
        return

    # For each kind and asset_id, the first persisted row belongs to Main's
    # current pool.  Historical rows may only follow that first row and
    # must be referenced by at least one delegated Agent.  This invariant
    # lets Worker reconstruct the Main/delegate boundary without a schema
    # column while dependency_order remains globally continuous.
    main_skill_asset_ids = {item.asset_id for item in closure.skills[:main_skill_count]}
    main_mcp_asset_ids = {item.asset_id for item in closure.mcps[:main_mcp_count]}
    if len(main_skill_asset_ids) != main_skill_count or len(main_mcp_asset_ids) != main_mcp_count:
        raise RunSnapshotAssetStale

    skill_by_version = {item.version_id: item for item in closure.skills}
    mcp_by_version = {item.version_id: item for item in closure.mcps}
    expected_skill_ids = list(closure.main_skill_version_ids)
    expected_mcp_ids = list(closure.main_mcp_version_ids)
    seen_skill_ids = set(expected_skill_ids)
    seen_mcp_ids = set(expected_mcp_ids)
    for agent in closure.delegated_agents:
        for version_id in agent.skill_version_ids:
            item = skill_by_version.get(version_id)
            if item is None:
                raise RunSnapshotAssetStale
            if version_id not in seen_skill_ids:
                if item.asset_id not in main_skill_asset_ids:
                    raise RunSnapshotAssetStale
                expected_skill_ids.append(version_id)
                seen_skill_ids.add(version_id)
        for version_id in agent.payload.mcp_version_ids:
            item = mcp_by_version.get(version_id)
            if item is None:
                raise RunSnapshotAssetStale
            if version_id not in seen_mcp_ids:
                if item.asset_id not in main_mcp_asset_ids:
                    raise RunSnapshotAssetStale
                expected_mcp_ids.append(version_id)
                seen_mcp_ids.add(version_id)
    if skill_ids != tuple(expected_skill_ids) or mcp_ids != tuple(expected_mcp_ids):
        raise RunSnapshotAssetStale


@dataclass(frozen=True)
class PlannedRunAssetRows:
    asset_rows: list[RunAssetVersionRow]
    skill_ref_rows: list[RunSkillVersionRefRow]


def plan_run_asset_rows(
    *,
    context: PrivateWorkContext,
    thread_id: str,
    run: PrivateRunRecord,
    lead_agent: ResolvedAgentSnapshot,
    resolved_closure: ResolvedRunAssetClosure | None,
    skills: list[tuple[SkillRow, SkillVersionRow]],
    skill_snapshots: tuple[ResolvedSkillVersionSnapshot, ...],
    mcps: list[tuple[McpServerRow, McpServerVersionRow]],
    mcp_snapshots: tuple[ResolvedMcpSnapshot, ...],
    prepared_legacy_skills: PreparedLegacyRunSkillSnapshots | None,
) -> PlannedRunAssetRows:
    """Build the Run asset and Skill reference rows in dependency order under one encoder budget."""

    asset_snapshot_encoder = _RunAssetSnapshotAdmissionEncoder(
        request_id=context.request_id,
    )
    lead_agent_snapshot_json = asset_snapshot_encoder.encode(lead_agent)
    asset_rows = [
        RunAssetVersionRow(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            thread_id=thread_id,
            run_id=run.run_id,
            asset_kind=AssetKind.AGENT.value,
            dependency_order=0,
            asset_scope=lead_agent.scope.value,
            asset_id=lead_agent.asset_id,
            version_id=lead_agent.version_id,
            payload_checksum=lead_agent.checksum,
            catalog_generation=lead_agent.catalog_generation,
            snapshot_schema_version=_r1_snapshot_schema_version(lead_agent_snapshot_json),
            snapshot_json=lead_agent_snapshot_json,
        )
    ]
    dependency_order = 1
    if resolved_closure is not None:
        for delegated_agent in resolved_closure.delegated_agents:
            delegated_snapshot_json = asset_snapshot_encoder.encode(delegated_agent)
            asset_rows.append(
                RunAssetVersionRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    asset_kind=AssetKind.AGENT.value,
                    dependency_order=dependency_order,
                    asset_scope=delegated_agent.scope.value,
                    asset_id=delegated_agent.asset_id,
                    version_id=delegated_agent.version_id,
                    payload_checksum=delegated_agent.checksum,
                    catalog_generation=lead_agent.catalog_generation,
                    snapshot_schema_version=_r1_snapshot_schema_version(delegated_snapshot_json),
                    snapshot_json=delegated_snapshot_json,
                )
            )
            dependency_order += 1
    skill_ref_rows: list[RunSkillVersionRefRow] = []
    for skill_index, ((asset, version), skill_snapshot) in enumerate(
        zip(
            skills,
            skill_snapshots,
            strict=True,
        )
    ):
        if type(skill_snapshot) is not ResolvedSkillVersionSnapshot:
            raise RunSnapshotAssetStale
        if prepared_legacy_skills is None:
            skill_snapshot_json = asset_snapshot_encoder.encode_skill_version(
                skill_snapshot,
            )
            skill_snapshot_schema_version = RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION
        else:
            skill_snapshot_json = asset_snapshot_encoder.include_legacy_skill(
                prepared_legacy_skills.snapshot_jsons[skill_index],
            )
            skill_snapshot_schema_version = RUN_ASSET_SNAPSHOT_SCHEMA_VERSION
        asset_rows.append(
            RunAssetVersionRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                asset_kind=AssetKind.SKILL.value,
                dependency_order=dependency_order,
                asset_scope=asset.scope,
                asset_id=asset.id,
                version_id=version.id,
                payload_checksum=version.payload_checksum,
                catalog_generation=lead_agent.catalog_generation,
                snapshot_schema_version=skill_snapshot_schema_version,
                snapshot_json=skill_snapshot_json,
            )
        )
        if prepared_legacy_skills is None:
            skill_ref_rows.append(
                RunSkillVersionRefRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    asset_kind=AssetKind.SKILL.value,
                    dependency_order=dependency_order,
                    asset_scope=asset.scope,
                    snapshot_schema_version=(RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION),
                    skill_project_id=(None if asset.scope == AssetScope.SYSTEM.value else asset.project_id),
                    skill_id=asset.id,
                    skill_version_id=version.id,
                    payload_checksum=version.payload_checksum,
                    file_count=version.file_count,
                    content_size_bytes=version.content_size_bytes,
                )
            )
        dependency_order += 1
    for (asset, version), mcp_snapshot in zip(
        mcps,
        mcp_snapshots,
        strict=True,
    ):
        mcp_snapshot_json = asset_snapshot_encoder.encode(mcp_snapshot)
        asset_rows.append(
            RunAssetVersionRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                asset_kind=AssetKind.MCP.value,
                dependency_order=dependency_order,
                asset_scope=asset.scope,
                asset_id=asset.id,
                version_id=version.id,
                payload_checksum=version.payload_checksum,
                catalog_generation=lead_agent.catalog_generation,
                snapshot_schema_version=_r1_snapshot_schema_version(mcp_snapshot_json),
                snapshot_json=mcp_snapshot_json,
            )
        )
        dependency_order += 1
    return PlannedRunAssetRows(asset_rows=asset_rows, skill_ref_rows=skill_ref_rows)
