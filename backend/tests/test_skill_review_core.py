import io
import json
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator

from deerflow.skills.review import (
    ArchivePackageReader,
    LocalDirectoryReader,
    analyze_skill_package,
    build_inline_snapshot,
)
from deerflow.skills.review.cli import main as review_cli_main
from deerflow.skills.review.models import DEFAULT_PACKAGE_LIMITS, PackageLimits
from deerflow.skills.review.renderer import build_static_report

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "skill_review"


def _valid_skill(name: str = "demo-skill") -> str:
    return f"---\nname: {name}\ndescription: Demo skill. Invoke when testing deterministic review.\nallowed-tools: []\n---\n\n# Demo\n\nFollow the steps and stop.\n"


def _validate_contract(schema_name: str, instance: dict) -> None:
    schema = json.loads((CONTRACTS_DIR / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(instance)


def test_review_defaults_match_dev_package_boundary() -> None:
    assert DEFAULT_PACKAGE_LIMITS.max_files == 16_384
    assert DEFAULT_PACKAGE_LIMITS.max_file_bytes == 100 * 1024 * 1024
    assert DEFAULT_PACKAGE_LIMITS.max_total_bytes == 100 * 1024 * 1024


def test_inline_review_produces_all_v1_contracts() -> None:
    snapshot = build_inline_snapshot(_valid_skill(), name_hint="demo-skill")
    facts = analyze_skill_package(snapshot)
    report = build_static_report(
        facts,
        completed_at="2026-07-29T00:00:00Z",
    )

    _validate_contract("package_snapshot.v1.schema.json", snapshot)
    _validate_contract("review_facts.v1.schema.json", facts)
    _validate_contract("review_report.v1.schema.json", report)
    assert facts["subject"]["declared_name"] == "demo-skill"
    assert facts["subject"]["package_digest"].startswith("sha256:")
    assert report["readiness"] == "publish_candidate"


def test_local_and_archive_readers_are_path_independent(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "SKILL.md").write_text(_valid_skill(), encoding="utf-8")
    (package / "references").mkdir()
    (package / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")

    archive = tmp_path / "package.skill"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("SKILL.md", _valid_skill())
        output.writestr("references/guide.md", "# Guide\n")

    local_facts = analyze_skill_package(LocalDirectoryReader(package).read())
    archive_facts = analyze_skill_package(ArchivePackageReader(archive).read())

    assert local_facts["subject"]["package_digest"] == archive_facts["subject"]["package_digest"]


def test_archive_reader_caps_actual_decompressed_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeInfo:
        filename = "SKILL.md"
        file_size = 1
        external_attr = 0

        def is_dir(self) -> bool:
            return False

    class FakeMember(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    class FakeZip:
        def __init__(self, archive_path, mode):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

        def infolist(self):
            return [FakeInfo()]

        def open(self, info):
            return FakeMember(b"x" * 20)

    monkeypatch.setattr(zipfile, "ZipFile", FakeZip)

    snapshot = ArchivePackageReader(
        tmp_path / "spoofed.skill",
        limits=PackageLimits(max_file_bytes=10, max_total_bytes=100),
    ).read()

    assert snapshot["truncated"] is True
    assert any(error["code"] == "file_too_large" and error["path"] == "SKILL.md" for error in snapshot["reader_errors"])
    assert snapshot["files"][0]["kind"] == "binary"
    assert snapshot["files"][0]["size"] == 11


def test_archive_reader_caps_actual_total_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeInfo:
        external_attr = 0

        def __init__(self, filename: str) -> None:
            self.filename = filename
            self.file_size = 1

        def is_dir(self) -> bool:
            return False

    class FakeMember(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    class FakeZip:
        def __init__(self, archive_path, mode):
            self._members = [
                FakeInfo("SKILL.md"),
                FakeInfo("references/large.md"),
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

        def infolist(self):
            return self._members

        def open(self, info):
            return FakeMember(b"x" * 6)

    monkeypatch.setattr(zipfile, "ZipFile", FakeZip)

    snapshot = ArchivePackageReader(
        tmp_path / "spoofed.skill",
        limits=PackageLimits(max_file_bytes=100, max_total_bytes=10),
    ).read()

    assert snapshot["truncated"] is True
    assert any(error["code"] == "total_size_exceeded" and error["path"] == "references/large.md" for error in snapshot["reader_errors"])
    assert [entry["path"] for entry in snapshot["files"]] == ["SKILL.md"]


def test_review_cli_fails_on_blocker(tmp_path: Path, capsys) -> None:
    (tmp_path / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    exit_code = review_cli_main(
        [
            str(tmp_path),
            "--format",
            "text",
            "--fail-on",
            "blocker",
        ]
    )

    assert exit_code == 1
    assert "structure.missing-description" in capsys.readouterr().out
