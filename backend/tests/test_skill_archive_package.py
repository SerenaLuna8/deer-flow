from __future__ import annotations

import io
import stat
import struct
import tarfile
import zipfile

import pytest

from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.skill_archive import load_skill_archive_package


def _manifest(name: str = "meeting-brief") -> bytes:
    return (f"---\nname: {name}\ndescription: Summarize a meeting safely\n---\n\n# Usage\n").encode()


def _zip(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
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


def _zip_with_empty_members(member_count: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", _manifest())
        for index in range(member_count - 1):
            archive.writestr(f"references/{index:05d}.txt", b"")
    return buffer.getvalue()


def _eocd_offset(payload: bytes) -> int:
    offset = payload.rfind(b"PK\x05\x06")
    assert offset >= 0
    return offset


def _with_zip64_metadata(
    payload: bytes,
    *,
    entry_count: int | None = None,
    central_directory_size: int | None = None,
    central_directory_offset: int | None = None,
) -> bytes:
    eocd_offset = _eocd_offset(payload)
    (
        _signature,
        _disk_number,
        _central_directory_disk,
        _entries_on_disk,
        original_entry_count,
        original_directory_size,
        original_directory_offset,
        _comment_bytes,
    ) = struct.unpack_from("<4s4H2LH", payload, eocd_offset)
    zip64_record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        entry_count if entry_count is not None else original_entry_count,
        entry_count if entry_count is not None else original_entry_count,
        (central_directory_size if central_directory_size is not None else original_directory_size),
        (central_directory_offset if central_directory_offset is not None else original_directory_offset),
    )
    locator = struct.pack(
        "<4sLQL",
        b"PK\x06\x07",
        0,
        eocd_offset,
        1,
    )
    classic_eocd = bytearray(payload[eocd_offset:])
    struct.pack_into(
        "<2H2L",
        classic_eocd,
        8,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
    )
    return payload[:eocd_offset] + zip64_record + locator + bytes(classic_eocd)


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


def _tar_metadata_bomb(*, format: int) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer,
        mode="w:gz",
        format=format,
    ) as archive:
        if format == tarfile.PAX_FORMAT:
            path = "skill/SKILL.md"
        else:
            path = f"skill/{'a' * (2 * 1024 * 1024)}/SKILL.md"
        content = _manifest()
        info = tarfile.TarInfo(path)
        info.size = len(content)
        if format == tarfile.PAX_FORMAT:
            info.pax_headers = {"comment": "x" * (2 * 1024 * 1024)}
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        (
            "meeting-brief.zip",
            _zip(
                {
                    "SKILL.md": _manifest(),
                    "scripts/summarize.py": b"print('ok')\n",
                }
            ),
        ),
        (
            "meeting-brief.skill",
            _zip(
                {
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/references/format.md": b"# Format\n",
                }
            ),
        ),
        (
            "meeting-brief.tar",
            _tar(
                {
                    "meeting-brief/SKILL.md": _manifest(),
                    "meeting-brief/assets/example.txt": b"example\n",
                }
            ),
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
        ),
    ],
)
def test_load_skill_archive_package_supports_safe_standard_formats(
    filename: str,
    payload: bytes,
) -> None:
    files = load_skill_archive_package(
        payload,
        filename=filename,
        request_id="req-safe-package",
    )

    assert files[0].path == "SKILL.md"
    assert files[0].content == _manifest()
    assert all(not item.path.startswith("meeting-brief/") for item in files)
    assert {item.media_type for item in files}


def test_load_skill_archive_package_supports_bounded_zip64_metadata() -> None:
    payload = _with_zip64_metadata(
        _zip(
            {
                "skill/SKILL.md": _manifest(),
                "skill/references/format.md": b"# Format\n",
            }
        )
    )

    files = load_skill_archive_package(
        payload,
        filename="meeting-brief.zip",
        request_id="req-safe-zip64-package",
    )

    assert [item.path for item in files] == [
        "SKILL.md",
        "references/format.md",
    ]


def test_load_skill_archive_package_discards_empty_directory_entries() -> None:
    files = load_skill_archive_package(
        _zip(
            {
                "meeting-brief/SKILL.md": _manifest(),
                "meeting-brief/templates/": b"",
            }
        ),
        filename="meeting-brief.zip",
        request_id="req-empty-directory",
    )

    assert [item.path for item in files] == ["SKILL.md"]


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
    ],
)
def test_load_skill_archive_package_rejects_unsafe_paths(
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
    ("filename", "payload"),
    [
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
            "ads.tar",
            _tar(
                {
                    "skill/SKILL.md": _manifest(),
                    "skill/scripts/run.sh:hidden.txt": b"hidden",
                }
            ),
        ),
    ],
)
def test_load_skill_archive_package_rejects_ntfs_ads_paths(
    filename: str,
    payload: bytes,
) -> None:
    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename=filename,
            request_id="req-ads-package",
        )


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
def test_load_skill_archive_package_rejects_non_regular_tar_members(
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
def test_load_skill_archive_package_rejects_zip_symlinks(
    symlink: str,
) -> None:
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
        ("unsupported.rar", b"not-rar"),
        ("broken.zip", b"not-zip"),
    ],
)
def test_load_skill_archive_package_rejects_ambiguous_or_invalid_packages(
    filename: str,
    payload: bytes,
) -> None:
    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename=filename,
            request_id="req-invalid-package",
        )


def test_load_skill_archive_package_enforces_file_and_uncompressed_byte_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_FILES", 2)
    with pytest.raises(AssetValidationFailed):
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
    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_BYTES", len(_manifest()) + 1)
    with pytest.raises(AssetValidationFailed):
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


def test_zip_member_count_is_rejected_before_zipfile_allocates_zipinfo_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    payload = _zip_with_empty_members(
        skill_archive.MAX_SKILL_ARCHIVE_MEMBERS + 1,
    )

    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("ZipFile must not parse an oversized central directory")

    monkeypatch.setattr(skill_archive.zipfile, "ZipFile", fail_if_constructed)

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename="too-many-empty-files.zip",
            request_id="req-zip-member-preflight",
        )


def test_zip_central_directory_actual_entry_count_must_match_eocd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    payload = bytearray(
        _zip(
            {
                "SKILL.md": _manifest(),
                "references/format.md": b"# Format\n",
            }
        )
    )
    eocd_offset = _eocd_offset(payload)
    struct.pack_into("<2H", payload, eocd_offset + 8, 1, 1)

    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("ZipFile must not parse forged EOCD metadata")

    monkeypatch.setattr(skill_archive.zipfile, "ZipFile", fail_if_constructed)

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            bytes(payload),
            filename="forged-entry-count.zip",
            request_id="req-zip-forged-entry-count",
        )


@pytest.mark.parametrize(
    ("field_offset", "value_delta"),
    [
        (12, 1),
        (16, 1),
    ],
)
def test_zip_central_directory_size_and_offset_must_match_eocd_range(
    field_offset: int,
    value_delta: int,
) -> None:
    payload = bytearray(_zip({"SKILL.md": _manifest()}))
    eocd_offset = _eocd_offset(payload)
    original = struct.unpack_from("<L", payload, eocd_offset + field_offset)[0]
    struct.pack_into(
        "<L",
        payload,
        eocd_offset + field_offset,
        original + value_delta,
    )

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            bytes(payload),
            filename="forged-directory-range.zip",
            request_id="req-zip-forged-range",
        )


def test_zip64_member_count_and_directory_range_are_preflighted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    base = _zip({"SKILL.md": _manifest()})
    oversized_count = _with_zip64_metadata(
        base,
        entry_count=skill_archive.MAX_SKILL_ARCHIVE_MEMBERS + 1,
    )
    invalid_range = _with_zip64_metadata(
        base,
        central_directory_offset=1,
    )

    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("ZipFile must not parse forged ZIP64 metadata")

    monkeypatch.setattr(skill_archive.zipfile, "ZipFile", fail_if_constructed)

    for payload in (oversized_count, invalid_range):
        with pytest.raises(AssetValidationFailed):
            load_skill_archive_package(
                payload,
                filename="forged-zip64.zip",
                request_id="req-forged-zip64",
            )


def test_zip_central_directory_has_an_independent_size_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import skill_archive

    payload = _zip(
        {
            "SKILL.md": _manifest(),
            "references/format.md": b"# Format\n",
        }
    )
    eocd_offset = _eocd_offset(payload)
    central_directory_size = struct.unpack_from(
        "<L",
        payload,
        eocd_offset + 12,
    )[0]
    monkeypatch.setattr(
        skill_archive,
        "_MAX_ZIP_CENTRAL_DIRECTORY_BYTES",
        central_directory_size - 1,
    )

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename="over-budget-directory.zip",
            request_id="req-zip-directory-budget",
        )


def test_system_and_project_skill_archives_share_the_file_count_cap() -> None:
    from app.shared_assets.bootstrap.skill_archive import _SkillArchive
    from app.shared_assets.skill_archive import MAX_SKILL_ARCHIVE_FILES

    files_schema = _SkillArchive.model_json_schema()["properties"]["files"]

    assert files_schema["maxItems"] == MAX_SKILL_ARCHIVE_FILES == 16_384


@pytest.mark.parametrize("format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_compressed_tar_metadata_counts_toward_the_decompressed_stream_cap(
    monkeypatch: pytest.MonkeyPatch,
    format: int,
) -> None:
    from app.shared_assets import skill_archive

    payload = _tar_metadata_bomb(format=format)
    assert len(payload) < 32 * 1024
    monkeypatch.setattr(skill_archive, "MAX_SKILL_ARCHIVE_BYTES", 1000)

    with pytest.raises(AssetValidationFailed):
        load_skill_archive_package(
            payload,
            filename="metadata-bomb.tar.gz",
            request_id="req-tar-metadata-bomb",
        )
