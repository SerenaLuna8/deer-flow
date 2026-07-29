from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetValidationFailed,
)
from app.shared_assets.skill_review import (
    PostgresSkillReviewService,
    PostgresSkillVersionReader,
)

_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "skill_review"
_PACKAGE_SNAPSHOT_SCHEMA = json.loads((_SCHEMA_ROOT / "package_snapshot.v1.schema.json").read_text(encoding="utf-8"))
_FACTS_SCHEMA = json.loads((_SCHEMA_ROOT / "review_facts.v1.schema.json").read_text(encoding="utf-8"))
_REPORT_SCHEMA = json.loads((_SCHEMA_ROOT / "review_report.v1.schema.json").read_text(encoding="utf-8"))


def _actor(
    *,
    role: ProjectRole = ProjectRole.EDITOR,
    project_id: uuid.UUID | None = None,
) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=project_id or uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=3,
        request_id="request-1",
    )


def _skill_markdown() -> bytes:
    return b"---\nname: exact-skill\ndescription: Review this exact immutable Skill version.\nallowed-tools: []\n---\n\n# Exact Skill\n"


def _file(
    path: str,
    content: bytes,
    *,
    version_id: uuid.UUID,
):
    return SimpleNamespace(
        skill_version_id=version_id,
        path=path,
        media_type="text/markdown",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _payload_checksum(files: tuple[object, ...]) -> str:
    canonical = json.dumps(
        [
            {
                "path": item.path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in sorted(files, key=lambda item: item.path)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _record(skill_id: uuid.UUID, version_id: uuid.UUID):
    files = (
        _file(
            "SKILL.md",
            _skill_markdown(),
            version_id=version_id,
        ),
    )
    checksum = _payload_checksum(files)
    return (
        SimpleNamespace(
            row=SimpleNamespace(
                id=version_id,
                skill_id=skill_id,
                version_number=7,
                payload_checksum=checksum,
                created_at=datetime(2026, 7, 29, 8, 30, tzinfo=UTC),
            ),
            files=files,
        ),
        checksum,
    )


@pytest.mark.asyncio
async def test_postgres_reader_uses_signed_project_scope_and_exact_ids() -> None:
    actor = _actor()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    record, checksum = _record(skill_id, version_id)
    repository = SimpleNamespace(
        get_project_version=AsyncMock(return_value=record),
    )

    snapshot = await PostgresSkillVersionReader(repository).read(
        actor,
        skill_id=skill_id,
        version_id=version_id,
        expected_checksum=checksum,
    )

    repository.get_project_version.assert_awaited_once_with(
        actor,
        skill_id,
        version_id,
    )
    assert snapshot["subject"] == {
        "source": "postgres_skill_version",
        "category": "project",
        "name_hint": None,
        "display_ref": f"skill-version://{skill_id}/{version_id}",
        "skill_id": str(skill_id),
        "version_id": str(version_id),
        "version_number": 7,
        "payload_checksum": checksum,
        "version_created_at": "2026-07-29T08:30:00Z",
    }
    serialized_subject = json.dumps(snapshot["subject"])
    assert str(actor.user_id) not in serialized_subject
    assert str(actor.project_id) not in serialized_subject
    Draft202012Validator(_PACKAGE_SNAPSHOT_SCHEMA).validate(snapshot)


@pytest.mark.asyncio
async def test_postgres_reader_rejects_missing_capability_before_query() -> None:
    actor = _actor(role=ProjectRole.VIEWER)
    actor = ProjectContext(
        **{
            **vars(actor),
            "capabilities": actor.capabilities - frozenset({Capability.SHARED_ASSETS_READ}),
        }
    )
    repository = SimpleNamespace(get_project_version=AsyncMock())

    with pytest.raises(AssetForbidden):
        await PostgresSkillVersionReader(repository).read(
            actor,
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            expected_checksum="0" * 64,
        )

    repository.get_project_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_reader_rejects_stale_or_tampered_exact_version() -> None:
    actor = _actor()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    record, checksum = _record(skill_id, version_id)
    repository = SimpleNamespace(get_project_version=AsyncMock(return_value=record))
    reader = PostgresSkillVersionReader(repository)

    with pytest.raises(AssetConflict):
        await reader.read(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            expected_checksum="0" * 64,
        )

    record.files[0].content = b"tampered"
    with pytest.raises(AssetValidationFailed):
        await reader.read(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            expected_checksum=checksum,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected_checksum",
    [
        None,
        123,
        "",
        "A" * 64,
        "0" * 63,
        "0" * 65,
    ],
)
async def test_postgres_reader_rejects_invalid_expected_checksum(
    expected_checksum,
) -> None:
    repository = SimpleNamespace(get_project_version=AsyncMock())

    with pytest.raises(AssetValidationFailed):
        await PostgresSkillVersionReader(repository).read(
            _actor(),
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            expected_checksum=expected_checksum,
        )

    repository.get_project_version.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_field", ["id", "skill_id"])
async def test_postgres_reader_rechecks_record_exact_identity(
    wrong_field: str,
) -> None:
    actor = _actor()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    record, checksum = _record(skill_id, version_id)
    setattr(record.row, wrong_field, uuid.uuid4())
    repository = SimpleNamespace(
        get_project_version=AsyncMock(return_value=record),
    )

    with pytest.raises(AssetValidationFailed):
        await PostgresSkillVersionReader(repository).read(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            expected_checksum=checksum,
        )


class _Transaction:
    def __init__(self, state: dict[str, bool]) -> None:
        self._state = state

    async def __aenter__(self):
        self._state["in_transaction"] = True

    async def __aexit__(self, exc_type, exc, traceback):
        self._state["in_transaction"] = False


class _Session:
    def __init__(self, state: dict[str, bool]) -> None:
        self._state = state

    def begin(self) -> _Transaction:
        return _Transaction(self._state)


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, exc_type, exc, traceback):
        return None


@pytest.mark.asyncio
async def test_postgres_review_service_reads_in_one_transaction_and_analyzes_off_loop() -> None:
    actor = _actor()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    record, checksum = _record(skill_id, version_id)
    state = {"in_transaction": False}
    session = _Session(state)
    repository = SimpleNamespace()

    async def read_exact(*args, **kwargs):
        assert state["in_transaction"] is True
        files = record.files
        return {
            "schema_version": "deerflow.skill-package-snapshot.v1",
            "subject": {
                "source": "postgres_skill_version",
                "category": "project",
                "name_hint": None,
                "display_ref": f"skill-version://{skill_id}/{version_id}",
                "skill_id": str(skill_id),
                "version_id": str(version_id),
                "version_number": 7,
                "payload_checksum": checksum,
                "version_created_at": "2026-07-29T08:30:00Z",
            },
            "limits": {
                "max_files": 16_384,
                "max_file_bytes": 100 * 1024 * 1024,
                "max_total_bytes": 100 * 1024 * 1024,
            },
            "files": [
                {
                    "path": files[0].path,
                    "kind": "text",
                    "size": files[0].size_bytes,
                    "sha256": files[0].sha256,
                    "content": files[0].content.decode(),
                }
            ],
            "truncated": False,
            "reader_errors": [],
        }

    async def run_to_thread(function, /, *args, **kwargs):
        assert state["in_transaction"] is False
        return function(*args, **kwargs)

    with (
        patch(
            "app.shared_assets.skill_review.SkillRepository",
            return_value=repository,
        ),
        patch.object(
            PostgresSkillVersionReader,
            "read",
            AsyncMock(side_effect=read_exact),
        ),
        patch(
            "app.shared_assets.skill_review.asyncio.to_thread",
            AsyncMock(side_effect=run_to_thread),
        ) as to_thread,
    ):
        result = await PostgresSkillReviewService(lambda: _SessionContext(session)).review(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            expected_checksum=checksum,
        )
        repeated_result = await PostgresSkillReviewService(lambda: _SessionContext(session)).review(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            expected_checksum=checksum,
        )

    assert to_thread.await_count == 4
    assert result.facts["subject"]["package_digest"].startswith("sha256:")
    assert result.facts["subject"]["declared_name"] == "exact-skill"
    assert result.report["readiness"] == "publish_candidate"
    assert result.report["review"]["completed_at"] == "2026-07-29T08:30:00Z"
    assert f"skill-version://{skill_id}/{version_id}" in result.markdown_en
    Draft202012Validator(_FACTS_SCHEMA).validate(result.facts)
    Draft202012Validator(_REPORT_SCHEMA).validate(result.report)

    assert json.dumps(
        asdict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == json.dumps(
        asdict(repeated_result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
