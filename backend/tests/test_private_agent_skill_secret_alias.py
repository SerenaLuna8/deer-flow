from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.private_work import private_agent_runtime as runtime_module
from app.private_work.asset_runtime_contracts import (
    PrivateAgentManifest,
    PrivateSkillManifest,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.snapshot_repository import (
    RunAssetSnapshot,
    RunSkillCredentialSnapshot,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.run_snapshot_codec import encode_run_asset_snapshot
from app.shared_assets.skill_credential_closure import (
    LockedSkillCredentialClosure,
    LockedSkillCredentialMaterial,
)
from deerflow.skills.types import (
    SecretRequirement,
    Skill,
    SkillCategory,
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


@pytest.mark.asyncio
async def test_private_runtime_maps_credential_source_to_only_declared_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    binding_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    actor = ProjectContext(
        user_id=user_id,
        project_id=project_id,
        membership_id=membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="runtime-skill-secret-alias",
    )
    context = PrivateWorkContext.from_project(actor)
    manifest = PrivateSkillManifest(
        asset_id=skill_id,
        version_id=skill_version_id,
        relative_root="demo",
    )
    safe_manifest = PrivateAgentManifest(
        agent_asset_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        description="alias runtime",
        payload_schema_version=3,
        agents_instructions="",
        soul="",
        identity="",
        user_context="",
        model_ref="test-model",
        tool_groups=(),
        skills=(manifest,),
        mcps=(),
    )
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "custom" / "demo"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# demo\n", encoding="utf-8")
    skill = Skill(
        name="demo",
        description="alias runtime",
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_file,
        relative_path=Path("demo"),
        category=SkillCategory.CUSTOM,
        required_secrets=(SecretRequirement("TARGET_API_KEY", optional=False),),
        enabled=True,
        runtime_read_only=True,
    )
    skill_content = b"# demo\n"
    skill_checksum = hashlib.sha256(
        json.dumps(
            [
                {
                    "path": "SKILL.md",
                    "sha256": hashlib.sha256(skill_content).hexdigest(),
                    "size_bytes": len(skill_content),
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    resolved_skill = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=skill_id,
        version_id=skill_version_id,
        checksum=skill_checksum,
        catalog_generation=1,
        dependency_version_ids=(),
        files=(SkillArchiveFile("SKILL.md", skill_content, "text/markdown"),),
        secret_requirements=(SkillSecretRequirementSnapshot("TARGET_API_KEY", False),),
    )
    asset_snapshot = RunAssetSnapshot(
        asset_kind=AssetKind.SKILL.value,
        dependency_order=0,
        asset_scope=AssetScope.PROJECT.value,
        asset_id=skill_id,
        version_id=skill_version_id,
        payload_checksum=skill_checksum,
        catalog_generation=1,
        snapshot_json=encode_run_asset_snapshot(resolved_skill),
    )
    credential_snapshot = RunSkillCredentialSnapshot(
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        secret_name="TARGET_API_KEY",
        source_env_field_name="PROVIDER_TOKEN",
        skill_credential_binding_id=binding_id,
        binding_revision=4,
        credential_id=credential_id,
        credential_version_id=credential_version_id,
    )

    class _SnapshotRepository:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def list_assets_in_session(self, *_args: object, **_kwargs: object):
            return (asset_snapshot,)

        async def list_skill_credentials_in_session(
            self,
            *_args: object,
            **_kwargs: object,
        ):
            return (credential_snapshot,)

        async def lock_admitted_skill_credentials_in_session(
            self,
            *_args: object,
            **_kwargs: object,
        ):
            return closure.materials

    class _RunRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, **_kwargs: object):
            return SimpleNamespace(status="running")

    async def active(*_args: object, **_kwargs: object) -> bool:
        return True

    async def resolve(*_args: object, **_kwargs: object) -> ProjectContext:
        return actor

    closure = LockedSkillCredentialClosure(
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        config_revision=4,
        materials=(
            LockedSkillCredentialMaterial(
                binding_id=binding_id,
                binding_revision=4,
                skill_id=skill_id,
                skill_version_id=skill_version_id,
                env_name="TARGET_API_KEY",
                credential_id=credential_id,
                credential_version_id=credential_version_id,
                credential_field_group="env",
                credential_field_name="PROVIDER_TOKEN",
                envelope_id=uuid.uuid4(),
                envelope=SimpleNamespace(
                    key_id="test-key",
                    nonce=b"n" * 12,
                    ciphertext=b"c" * 32,
                ),
            ),
        ),
    )

    decrypted_payloads: list[dict[str, object]] = []

    def decrypt(*_args: object, **_kwargs: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "env": {
                "PROVIDER_TOKEN": "provider-secret-value",
                "UNRELATED_TOKEN": "unrelated-secret-value",
            },
        }
        decrypted_payloads.append(payload)
        return payload

    monkeypatch.setattr(runtime_module, "RunSnapshotRepository", _SnapshotRepository)
    monkeypatch.setattr(runtime_module, "PrivateRunRepository", _RunRepository)
    monkeypatch.setattr(
        runtime_module.PrivateRunAuthorizationService,
        "is_active",
        active,
    )
    monkeypatch.setattr(
        runtime_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(
        runtime_module.CredentialKeyring,
        "from_environment",
        staticmethod(lambda: object()),
    )
    monkeypatch.setattr(runtime_module, "decrypt_credential_payload", decrypt)
    runtime = runtime_module.PrivateAgentRuntime(
        context=context,
        run_id="runtime-alias-run",
        resolver=SimpleNamespace(),
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        safe_manifest=safe_manifest,
        skill_root=skill_root,
        skills=(skill,),
        mcp_snapshots=(),
        authorization_boundary=object(),
    )
    skill_path = skill.get_container_file_path("/mnt/skills")

    carrier = await runtime.materialize_skill_scoped_secrets(
        "/mnt/skills",
        {skill_path: frozenset({"TARGET_API_KEY"})},
    )

    assert carrier == {
        skill_path: {"TARGET_API_KEY": "provider-secret-value"},
    }
    assert "PROVIDER_TOKEN" not in repr(carrier)
    assert "UNRELATED_TOKEN" not in repr(carrier)
    assert decrypted_payloads == [{}]
