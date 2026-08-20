from __future__ import annotations

import io
import stat
import struct
import tarfile
import zipfile

import pytest

from app.shared_assets.errors import AssetValidationFailed, SkillArchiveLimitExceeded
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_archive import (
    dump_skill_distribution_zip,
    load_skill_archive_package,
)


def _manifest(name: str = "meeting-brief") -> bytes:
    return (f"---\nname: {name}\ndescription: Summarize a meeting safely\n---\n\n# Usage\n").encode()


def _zip(
    files: dict[str, bytes],
    *,
    symlink: str | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "SKILL.md")
    return buffer.getvalue()


def _tar(
    files: dict[str, bytes],
    *,
    mode: str = "w",
    special: tuple[str, bytes] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if special is not None:
            path, member_type = special
            info = tarfile.TarInfo(path)
            info.type = member_type
            info.linkname = "SKILL.md"
            archive.addfile(info)
    return buffer.getvalue()


def _zip_eocd_offset(payload: bytes) -> int:
    offset = payload.rfind(b"PK\x05\x06")
    assert offset >= 0
    return offset


@pytest.mark.parametrize(
    ("filename", "payload", "expected_paths"),
    [
        (
            "meeting-brief.zip",
            _zip(
                {
                    "SKILL.md": _manifest(),
                    "scripts/summarize.py": b"print('ok')\n",
                }
            ),
            ["SKILL.md", "scripts/summarize.py"],
        ),
        (
            "meeting-brief.skill",
            _zip(
                {
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/references/format.md": b"# Format\n",
                }
            ),
            ["SKILL.md", "references/format.md"],
        ),
        (
            "meeting-brief.tar",
            _tar(
                {
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/assets/example.txt": b"example\n",
                }
            ),
            ["SKILL.md", "assets/example.txt"],
        ),
        (
            "meeting-brief.tar.gz",
            _tar(
                {
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/templates/report.md": b"# Report\n",
                },
                mode="w:gz",
            ),
            ["SKILL.md", "templates/report.md"],
        ),
        (
            "meeting-brief.tgz",
            _tar(
                {
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/prompts/system.txt": b"Be concise.\n",
                },
                mode="w:gz",
            ),
            ["SKILL.md", "prompts/system.txt"],
        ),
    ],
)
def test_load_skill_archive_accepts_bounded_standard_formats(
    filename: str,
    payload: bytes,
    expected_paths: list[str],
) -> None:
    files = load_skill_archive_package(
        payload,
        filename=filename,
        request_id="req-safe-package",
    )

    assert [item.path for item in files] == expected_paths
    assert files[0].content == _manifest()
    assert all(item.media_type for item in files)


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        (
            "meeting-brief.zip",
            _zip(
                {
                    "__MACOSX/meeting-brief/._SKILL.md": b"apple-double",
                    "meeting-brief/.DS_Store": b"finder",
                    "meeting-brief/._SKILL.md": b"apple-double",
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/scripts/run": b"print('ok')\n",
                }
            ),
        ),
        (
            "meeting-brief.tar",
            _tar(
                {
                    "__MACOSX/meeting-brief/._SKILL.md": b"apple-double",
                    "meeting-brief/.DS_Store": b"finder",
                    "meeting-brief/._SKILL.md": b"apple-double",
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/scripts/run": b"print('ok')\n",
                }
            ),
        ),
    ],
)
def test_load_skill_archive_filters_macos_metadata_before_wrapper_detection(
    filename: str,
    payload: bytes,
) -> None:
    files = load_skill_archive_package(
        payload,
        filename=filename,
        request_id="req-macos-metadata",
    )

    assert [item.path for item in files] == ["SKILL.md", "scripts/run"]


def test_dump_skill_distribution_zip_is_deterministic_root_layout_and_filtered() -> None:
    files = (
        SkillArchiveFile("SKILL.md", _manifest(), "text/markdown"),
        SkillArchiveFile("scripts/run.py", b"print('ok')\n", "text/x-python"),
        SkillArchiveFile("evals/case.json", b"{}\n", "application/json"),
        SkillArchiveFile("examples/evals/kept.json", b"{}\n", "application/json"),
        SkillArchiveFile("vendor/node_modules/pkg/index.js", b"ignored\n", "text/javascript"),
        SkillArchiveFile("scripts/__pycache__/run.pyc", b"ignored", "application/octet-stream"),
        SkillArchiveFile("references/.DS_Store", b"ignored", "application/octet-stream"),
        SkillArchiveFile("scripts/helper.pyc", b"ignored", "application/octet-stream"),
    )

    first = dump_skill_distribution_zip(files, request_id="req-export")
    second = dump_skill_distribution_zip(files, request_id="req-export")

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first), mode="r") as archive:
        assert archive.namelist() == [
            "SKILL.md",
            "examples/evals/kept.json",
            "scripts/run.py",
        ]
        assert archive.read("SKILL.md") == _manifest()
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert stat.S_IFMT(info.external_attr >> 16) == stat.S_IFREG
            assert stat.S_IMODE(info.external_attr >> 16) == 0o644

    imported = load_skill_archive_package(
        first,
        filename="meeting-brief-v7.zip",
        request_id="req-export-roundtrip",
    )
    assert [(item.path, item.content) for item in imported] == [
        ("SKILL.md", _manifest()),
        ("examples/evals/kept.json", b"{}\n"),
        ("scripts/run.py", b"print('ok')\n"),
    ]


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        (
            "traversal.zip",
            _zip(
                {
                    "skill/SKILL.md": _manifest(),
                    "skill/../../outside.txt": b"no",
                }
            ),
        ),
        (
            "absolute.zip",
            _zip(
                {
                    "skill/SKILL.md": _manifest(),
                    "/etc/passwd": b"no",
                }
            ),
        ),
        (
            "windows.zip",
            _zip(
                {
                    "skill/SKILL.md": _manifest(),
                    r"C:\Windows\secret.txt": b"no",
                }
            ),
        ),
        (
            "traversal.tar",
            _tar(
                {
                    "skill/SKILL.md": _manifest(),
                    "skill/../outside.txt": b"no",
                }
            ),
        ),
        (
            "ads.zip",
            _zip(
                {
                    "skill/SKILL.md": _manifest(),
                    "skill/scripts/run.sh:hidden.txt": b"hidden",
                }
            ),
        ),
        (
            "macos-traversal.zip",
            _zip(
                {
                    "skill/SKILL.md": _manifest(),
                    "__MACOSX/../outside": b"no",
                }
            ),
        ),
    ],
)
def test_load_skill_archive_rejects_unsafe_paths(
    filename: str,
    payload: bytes,
) -> None:
    with pytest.raises(AssetValidationFailed) as exc_info:
        load_skill_archive_package(
            payload,
            filename=filename,
            request_id="req-unsafe-package",
        )

    assert exc_info.value.request_id == "req-unsafe-package"


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
        tarfile.GNUTYPE_SPARSE,
    ],
)
def test_load_skill_archive_rejects_non_regular_tar_members(
    member_type: bytes,
) -> None:
    payload = _tar(
        {"skill/SKILL.md": _manifest()},
        special=("skill/unsafe", member_type),
    )

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename="unsafe.tar",
            request_id="req-tar-special",
        )


@pytest.mark.parametrize("symlink", ["skill/link", "skill/link/"])
def test_load_skill_archive_rejects_zip_symlinks(symlink: str) -> None:
    payload = _zip(
        {"skill/SKILL.md": _manifest()},
        symlink=symlink,
    )

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename="unsafe.zip",
            request_id="req-zip-symlink",
        )


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("missing.zip", _zip({"readme.md": b"# Missing\n"})),
        (
            "two-roots.zip",
            _zip(
                {
                    "first/SKILL.md": _manifest("first"),
                    "second/SKILL.md": _manifest("second"),
                }
            ),
        ),
        (
            "nested-skill.zip",
            _zip(
                {
                    "SKILL.md": _manifest(),
                    "references/SKILL.md": _manifest("nested"),
                }
            ),
        ),
        (
            "case-collision.zip",
            _zip(
                {
                    "SKILL.md": _manifest(),
                    "assets/README.md": b"first",
                    "assets/readme.md": b"second",
                }
            ),
        ),
        (
            "file-directory-collision.zip",
            _zip(
                {
                    "SKILL.md": _manifest(),
                    "assets": b"file",
                    "assets/example.txt": b"nested",
                }
            ),
        ),
        ("unsupported.rar", b"not-rar"),
        ("broken.zip", b"not-zip"),
    ],
)
def test_load_skill_archive_rejects_ambiguous_or_invalid_packages(
    filename: str,
    payload: bytes,
) -> None:
    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename=filename,
            request_id="req-invalid-package",
        )


def test_load_skill_archive_enforces_file_and_uncompressed_byte_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_FILES", 2)
    with pytest.raises(SkillArchiveLimitExceeded):
        load_skill_archive_package(
            _zip(
                {
                    "SKILL.md": _manifest(),
                    "one.txt": b"1",
                    "two.txt": b"2",
                }
            ),
            filename="too-many.zip",
            request_id="req-count-cap",
        )

    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_FILES", 16_384)
    monkeypatch.setattr(
        skill_archive,
        "MAX_SKILL_ARCHIVE_BYTES",
        len(_manifest()) + 1,
    )
    with pytest.raises(SkillArchiveLimitExceeded):
        load_skill_archive_package(
            _tar(
                {
                    "SKILL.md": _manifest(),
                    "two-bytes.txt": b"12",
                }
            ),
            filename="too-large.tar",
            request_id="req-byte-cap",
        )

    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_BYTES", 100 * 1024 * 1024)
    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_FILE_BYTES", 1)
    with pytest.raises(SkillArchiveLimitExceeded):
        load_skill_archive_package(
            _zip({"SKILL.md": _manifest()}),
            filename="file-too-large.zip",
            request_id="req-file-cap",
        )


def test_zip_member_count_is_rejected_before_zipfile_allocates_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    payload = bytearray(_zip({"SKILL.md": _manifest()}))
    eocd_offset = _zip_eocd_offset(payload)
    oversized_count = skill_archive.MAX_SKILL_ARCHIVE_MEMBERS + 1
    struct.pack_into(
        "<2H",
        payload,
        eocd_offset + 8,
        oversized_count,
        oversized_count,
    )

    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("ZipFile must not parse oversized metadata")

    monkeypatch.setattr(skill_archive.zipfile, "ZipFile", fail_if_constructed)

    with pytest.raises(SkillArchiveLimitExceeded):
        load_skill_archive_package(
            bytes(payload),
            filename="too-many-files.zip",
            request_id="req-zip-member-preflight",
        )


@pytest.mark.parametrize("field_offset", [12, 16])
def test_zip_central_directory_range_must_match_eocd(
    field_offset: int,
) -> None:
    payload = bytearray(_zip({"SKILL.md": _manifest()}))
    eocd_offset = _zip_eocd_offset(payload)
    original = struct.unpack_from("<L", payload, eocd_offset + field_offset)[0]
    struct.pack_into("<L", payload, eocd_offset + field_offset, original + 1)

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            bytes(payload),
            filename="forged-directory-range.zip",
            request_id="req-zip-forged-range",
        )


def test_compressed_tar_metadata_counts_toward_stream_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        content = _manifest()
        info = tarfile.TarInfo("skill/SKILL.md")
        info.size = len(content)
        info.pax_headers = {"comment": "x" * (128 * 1024)}
        archive.addfile(info, io.BytesIO(content))
    payload = buffer.getvalue()
    assert len(payload) < 16 * 1024
    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_BYTES", 1_000)

    with pytest.raises(SkillArchiveLimitExceeded):
        load_skill_archive_package(
            payload,
            filename="metadata-bomb.tar.gz",
            request_id="req-tar-metadata-bomb",
        )
