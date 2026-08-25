from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.errors import (
    LegacyAdmissionBusy,
    PrivateWorkAssetStale,
    PrivateWorkTooLarge,
)
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import (
    AssetKind,
    ResolvedSkillSnapshot,
    ResolvedSkillVersionSnapshot,
)
from app.shared_assets.run_snapshot_codec import (
    LEGACY_SKILL_ARCHIVE_CODEC,
    LEGACY_SKILL_FILE_FIXED_BYTES,
    LEGACY_SKILL_FRAME_FIXED_BYTES,
    RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
    RunAssetSnapshotTooLarge,
    encode_run_asset_snapshot,
    encoded_run_asset_snapshot_json_size,
)
from app.shared_assets.skill_repository import SkillVersionRecord
from app.shared_assets.skill_service import SkillService
from deerflow.config.run_skill_snapshot_config import (
    RunSkillSnapshotConfig,
    RunSkillSnapshotWriterMode,
)
from deerflow.persistence.shared_assets.skill_model import (
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.utils.asyncio import joined_to_thread

RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION = "run-skill-snapshot-writer-v2"
LEGACY_ADMISSION_BYTE_GATE_KEY = 1_285_249_287_551_683_557


@dataclass(frozen=True, slots=True)
class LegacyAdmissionEnvelope:
    source_bytes: int
    codec_working_set_bytes: int
    encoded_upper_bound_bytes: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.source_bytes,
                self.codec_working_set_bytes,
                self.encoded_upper_bound_bytes,
            )
        ):
            raise ValueError("Legacy Admission envelope is invalid")


@dataclass(frozen=True, slots=True)
class LegacyAdmissionPolicy:
    revision: int
    max_source_bytes_per_skill: int
    max_codec_working_set_bytes_per_skill: int
    max_encoded_bytes_per_run: int

    def canonical_payload(self) -> dict[str, int]:
        return dict(sorted(asdict(self).items()))

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def require_admissible(
        self,
        envelopes: tuple[LegacyAdmissionEnvelope, ...],
        *,
        request_id: str,
    ) -> int:
        encoded_total = 0
        for envelope in envelopes:
            if type(envelope) is not LegacyAdmissionEnvelope or envelope.source_bytes > self.max_source_bytes_per_skill or envelope.codec_working_set_bytes > self.max_codec_working_set_bytes_per_skill:
                raise PrivateWorkTooLarge(request_id)
            encoded_total += envelope.encoded_upper_bound_bytes
            if encoded_total > self.max_encoded_bytes_per_run:
                raise PrivateWorkTooLarge(request_id)
        return encoded_total


LEGACY_ADMISSION_POLICY = LegacyAdmissionPolicy(
    revision=2,
    max_source_bytes_per_skill=36 * 1024 * 1024,
    max_codec_working_set_bytes_per_skill=256 * 1024 * 1024,
    max_encoded_bytes_per_run=48 * 1024 * 1024,
)


class LegacyAdmissionConfigurationError(RuntimeError):
    """The operator-selected legacy release identity is not homogeneous."""


class RunSkillSnapshotWriterReconfigurationError(RuntimeError):
    """A running process attempted to change its Run Skill writer."""


class LegacyAdmissionByteGate:
    """One non-blocking transaction-scoped byte-bearing writer permit."""

    __slots__ = ()

    async def acquire(
        self,
        session: AsyncSession,
        *,
        request_id: str,
    ) -> None:
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise ValueError(
                "Legacy Admission gate requires the outer Admission transaction",
            )
        acquired = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": LEGACY_ADMISSION_BYTE_GATE_KEY},
        )
        if acquired is not True:
            raise LegacyAdmissionBusy(request_id)


@dataclass(frozen=True, slots=True)
class PreparedLegacyRunSkillSnapshots:
    snapshot_jsons: tuple[Mapping[str, object], ...]
    encoded_upper_bound_bytes: int
    actual_encoded_bytes: int
    policy_digest: str


def _zlib_compress_bound(source_bytes: int) -> int:
    return source_bytes + (source_bytes >> 12) + (source_bytes >> 14) + (source_bytes >> 25) + 13


def _base64_encoded_size(source_bytes: int) -> int:
    return 4 * ((source_bytes + 2) // 3)


def _legacy_v3_envelope(
    snapshot: ResolvedSkillVersionSnapshot,
    *,
    path_bytes: int,
    media_type_bytes: int,
) -> LegacyAdmissionEnvelope:
    frame_bytes = LEGACY_SKILL_FRAME_FIXED_BYTES + snapshot.content_size_bytes + snapshot.file_count * LEGACY_SKILL_FILE_FIXED_BYTES + path_bytes + media_type_bytes
    compressed_upper = _zlib_compress_bound(frame_bytes)
    archive_base64_upper = _base64_encoded_size(compressed_upper)
    stub: dict[str, object] = {
        "schema_version": RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
        "kind": AssetKind.SKILL.value,
        "scope": snapshot.scope.value,
        "asset_id": str(snapshot.asset_id),
        "version_id": str(snapshot.version_id),
        "checksum": snapshot.checksum,
        "catalog_generation": snapshot.catalog_generation,
        "dependency_version_ids": [str(value) for value in snapshot.dependency_version_ids],
        "skill": {
            "codec": LEGACY_SKILL_ARCHIVE_CODEC,
            "file_count": snapshot.file_count,
            "content_size": snapshot.content_size_bytes,
            "uncompressed_size": frame_bytes,
            "compressed_size": compressed_upper,
            "archive_base64": "",
            "secret_requirements": [
                {
                    "name": item.name,
                    "target_env": item.target_env,
                    "optional": item.optional,
                }
                for item in snapshot.secret_requirements
            ],
        },
    }
    encoded_upper = encoded_run_asset_snapshot_json_size(stub) + archive_base64_upper
    return LegacyAdmissionEnvelope(
        source_bytes=snapshot.content_size_bytes,
        codec_working_set_bytes=(snapshot.content_size_bytes + frame_bytes + compressed_upper + archive_base64_upper + encoded_upper),
        encoded_upper_bound_bytes=encoded_upper,
    )


def _encode_exact_legacy_skill(
    asset: SkillRow,
    version: SkillVersionRow,
    snapshot: ResolvedSkillVersionSnapshot,
    rows: tuple[SkillVersionFileRow, ...],
    request_id: str,
) -> dict[str, object]:
    files = SkillService._verified_archive_files(  # noqa: SLF001
        SkillVersionRecord(version, rows),
        request_id,
    )
    encoded = encode_run_asset_snapshot(
        ResolvedSkillSnapshot(
            kind=AssetKind.SKILL,
            scope=snapshot.scope,
            asset_id=uuid.UUID(str(asset.id)),
            version_id=uuid.UUID(str(version.id)),
            checksum=version.payload_checksum,
            catalog_generation=snapshot.catalog_generation,
            dependency_version_ids=snapshot.dependency_version_ids,
            files=files,
            secret_requirements=snapshot.secret_requirements,
        )
    )
    if encoded.get("schema_version") != RUN_ASSET_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Legacy Run Skill codec returned an invalid schema")
    return encoded


class LegacyRunSkillSnapshotWriter:
    """The sole byte-bearing legacy v3 Run Skill Admission writer."""

    __slots__ = ()

    @staticmethod
    def _validate_locked_pair(
        asset: SkillRow,
        version: SkillVersionRow,
        snapshot: ResolvedSkillVersionSnapshot,
    ) -> None:
        if (
            type(asset) is not SkillRow
            or type(version) is not SkillVersionRow
            or type(snapshot) is not ResolvedSkillVersionSnapshot
            or snapshot.kind is not AssetKind.SKILL
            or asset.id != snapshot.asset_id
            or asset.scope != snapshot.scope.value
            or version.id != snapshot.version_id
            or version.skill_id != asset.id
            or version.payload_checksum != snapshot.checksum
            or version.file_count != snapshot.file_count
            or version.content_size_bytes != snapshot.content_size_bytes
            or version.files_sealed is not True
        ):
            raise PrivateWorkAssetStale("legacy-run-skill-snapshot")

    async def prepare(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        locked_skills: tuple[tuple[SkillRow, SkillVersionRow], ...],
        snapshots: tuple[ResolvedSkillVersionSnapshot, ...],
    ) -> PreparedLegacyRunSkillSnapshots:
        if (
            not isinstance(session, AsyncSession)
            or not session.in_transaction()
            or not isinstance(request_id, str)
            or not request_id
            or not isinstance(locked_skills, tuple)
            or not isinstance(snapshots, tuple)
            or len(locked_skills) != len(snapshots)
        ):
            raise ValueError("Legacy Run Skill writer input is invalid")
        if not locked_skills:
            return PreparedLegacyRunSkillSnapshots(
                snapshot_jsons=(),
                encoded_upper_bound_bytes=0,
                actual_encoded_bytes=0,
                policy_digest=LEGACY_ADMISSION_POLICY.canonical_digest(),
            )

        pairs = tuple(
            (asset, version, snapshot)
            for (asset, version), snapshot in zip(
                locked_skills,
                snapshots,
                strict=True,
            )
        )
        for asset, version, snapshot in pairs:
            try:
                self._validate_locked_pair(asset, version, snapshot)
            except PrivateWorkAssetStale:
                raise PrivateWorkAssetStale(request_id) from None

        # A content-only lower bound can reject known oversize payloads without
        # querying child metadata, attempting the permit, or detoasting bytes.
        minimum_envelopes = tuple(
            _legacy_v3_envelope(
                snapshot,
                path_bytes=0,
                media_type_bytes=0,
            )
            for _asset, _version, snapshot in pairs
        )
        LEGACY_ADMISSION_POLICY.require_admissible(
            minimum_envelopes,
            request_id=request_id,
        )

        envelopes: list[LegacyAdmissionEnvelope] = []
        for _asset, version, snapshot in pairs:
            metadata = (
                await session.execute(
                    select(
                        func.count(SkillVersionFileRow.path),
                        func.coalesce(
                            func.sum(SkillVersionFileRow.size_bytes),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                func.octet_length(SkillVersionFileRow.path),
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                func.octet_length(
                                    SkillVersionFileRow.media_type,
                                ),
                            ),
                            0,
                        ),
                    ).where(
                        SkillVersionFileRow.skill_version_id == version.id,
                    )
                )
            ).one()
            file_count, content_size, path_bytes, media_type_bytes = (int(value) for value in metadata)
            if file_count != snapshot.file_count or content_size != snapshot.content_size_bytes:
                raise PrivateWorkAssetStale(request_id)
            envelopes.append(
                _legacy_v3_envelope(
                    snapshot,
                    path_bytes=path_bytes,
                    media_type_bytes=media_type_bytes,
                )
            )
        encoded_upper_bound = LEGACY_ADMISSION_POLICY.require_admissible(
            tuple(envelopes),
            request_id=request_id,
        )

        await LegacyAdmissionByteGate().acquire(
            session,
            request_id=request_id,
        )

        encoded_snapshots: list[Mapping[str, object]] = []
        actual_encoded_bytes = 0
        for asset, version, snapshot in pairs:
            rows = tuple(
                (
                    await session.execute(
                        select(SkillVersionFileRow)
                        .where(
                            SkillVersionFileRow.skill_version_id == version.id,
                        )
                        .order_by(SkillVersionFileRow.path.collate("C"))
                        .with_for_update(read=True, of=SkillVersionFileRow)
                    )
                )
                .scalars()
                .all()
            )
            try:
                encoded = await joined_to_thread(
                    _encode_exact_legacy_skill,
                    asset,
                    version,
                    snapshot,
                    rows,
                    request_id,
                )
            except AssetValidationFailed:
                raise PrivateWorkAssetStale(request_id) from None
            except RunAssetSnapshotTooLarge:
                raise PrivateWorkTooLarge(request_id) from None
            finally:
                for row in rows:
                    session.expunge(row)
            actual_encoded_bytes += encoded_run_asset_snapshot_json_size(
                encoded,
            )
            if actual_encoded_bytes > LEGACY_ADMISSION_POLICY.max_encoded_bytes_per_run or actual_encoded_bytes > encoded_upper_bound:
                raise PrivateWorkTooLarge(request_id)
            encoded_snapshots.append(encoded)

        return PreparedLegacyRunSkillSnapshots(
            snapshot_jsons=tuple(encoded_snapshots),
            encoded_upper_bound_bytes=encoded_upper_bound,
            actual_encoded_bytes=actual_encoded_bytes,
            policy_digest=LEGACY_ADMISSION_POLICY.canonical_digest(),
        )


@dataclass(frozen=True, slots=True)
class RunSkillSnapshotWriterReadback:
    writer_mode: RunSkillSnapshotWriterMode
    artifact_version: str
    legacy_policy_digest: str
    ready: bool

    def as_public_dict(self) -> dict[str, object]:
        return asdict(self)


_frozen_writer_readback: RunSkillSnapshotWriterReadback | None = None


def freeze_run_skill_snapshot_writer(
    config: RunSkillSnapshotConfig,
) -> RunSkillSnapshotWriterReadback:
    global _frozen_writer_readback
    if type(config) is not RunSkillSnapshotConfig:
        raise LegacyAdmissionConfigurationError(
            "Run Skill snapshot writer configuration is invalid",
        )
    policy_digest = LEGACY_ADMISSION_POLICY.canonical_digest()
    if config.writer_mode == "legacy_v3" and (config.expected_artifact_version != RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION or config.expected_legacy_policy_digest != policy_digest):
        raise LegacyAdmissionConfigurationError(
            "Legacy Run Skill writer release identity is unavailable",
        )
    selected = RunSkillSnapshotWriterReadback(
        writer_mode=config.writer_mode,
        artifact_version=RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
        legacy_policy_digest=policy_digest,
        ready=True,
    )
    if _frozen_writer_readback is None:
        _frozen_writer_readback = selected
    elif _frozen_writer_readback != selected:
        raise RunSkillSnapshotWriterReconfigurationError(
            "Run Skill snapshot writer mode is restart-required",
        )
    return _frozen_writer_readback


def frozen_run_skill_snapshot_writer() -> RunSkillSnapshotWriterReadback:
    """Return the process writer, defaulting safely to the v4 release."""

    if _frozen_writer_readback is None:
        return freeze_run_skill_snapshot_writer(RunSkillSnapshotConfig())
    return _frozen_writer_readback


def reset_run_skill_snapshot_writer_for_testing() -> None:
    global _frozen_writer_readback
    _frozen_writer_readback = None


__all__ = [
    "LEGACY_ADMISSION_POLICY",
    "LEGACY_ADMISSION_BYTE_GATE_KEY",
    "RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION",
    "LegacyAdmissionConfigurationError",
    "LegacyAdmissionByteGate",
    "LegacyAdmissionEnvelope",
    "LegacyAdmissionPolicy",
    "LegacyRunSkillSnapshotWriter",
    "PreparedLegacyRunSkillSnapshots",
    "RunSkillSnapshotWriterReadback",
    "RunSkillSnapshotWriterReconfigurationError",
    "freeze_run_skill_snapshot_writer",
    "frozen_run_skill_snapshot_writer",
    "reset_run_skill_snapshot_writer_for_testing",
]
