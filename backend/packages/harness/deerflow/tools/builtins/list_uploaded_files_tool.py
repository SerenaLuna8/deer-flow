"""Project-scoped historical upload metadata query."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from langchain.tools import tool
from langchain_core.messages import HumanMessage

from deerflow.agents.human_input import read_human_input_response
from deerflow.agents.middlewares.input_sanitization_middleware import (
    neutralize_untrusted_tags,
)
from deerflow.file_authority import require_private_file_authority
from deerflow.tools.types import Runtime

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _current_upload_ids(
    runtime: Runtime,
    authority: object,
) -> frozenset[str]:
    """Return upload version ids attached to the latest user message."""

    result: set[str] = set()
    authority_current_upload_ids = getattr(
        authority,
        "current_upload_ids",
        None,
    )
    if callable(authority_current_upload_ids):
        raw_authority_ids = authority_current_upload_ids()
        if type(raw_authority_ids) is not tuple or any(type(file_id) is not str or not file_id for file_id in raw_authority_ids):
            raise RuntimeError("Private upload authority is unavailable")
        result.update(raw_authority_ids)

    state = runtime.state
    if not isinstance(state, dict):
        return frozenset(result)
    messages = state.get("messages")
    if not isinstance(messages, list):
        return frozenset(result)
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        additional_kwargs = message.additional_kwargs or {}
        if additional_kwargs.get("hide_from_ui") is True and read_human_input_response(additional_kwargs) is None:
            continue
        raw_files = additional_kwargs.get("files")
        if not isinstance(raw_files, list):
            return frozenset(result)
        result.update(
            file_id
            for item in raw_files
            if isinstance(item, dict)
            and isinstance(
                (file_id := item.get("file_id")),
                str,
            )
            and file_id
        )
        return frozenset(result)
    return frozenset(result)


def _validated_upload(raw: object) -> dict[str, object]:
    """Validate one opaque manifest item and remove its virtual path."""

    if type(raw) is not dict:
        raise RuntimeError("Private upload authority is unavailable")
    file_id = raw.get("file_id")
    version = raw.get("version")
    filename = raw.get("filename")
    size = raw.get("size")
    path = raw.get("path")
    media_type = raw.get("media_type")
    if (
        type(file_id) is not str
        or not file_id
        or type(version) is not int
        or version < 1
        or type(filename) is not str
        or not filename
        or PurePosixPath(filename).name != filename
        or type(size) is not int
        or size < 0
        or type(path) is not str
        or path != f"/mnt/user-data/uploads/{filename}"
        or type(media_type) is not str
        or not media_type
    ):
        raise RuntimeError("Private upload authority is unavailable")
    return {
        "file_version_id": file_id,
        "version": version,
        "display_name": neutralize_untrusted_tags(filename),
        "size": size,
        "media_type": neutralize_untrusted_tags(media_type),
    }


@tool("list_uploaded_files", parse_docstring=True)
async def list_uploaded_files_tool(
    runtime: Runtime,
    filename: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> str:
    """List historical uploads authorized for the current project thread.

    The result contains opaque file-version identifiers and display metadata
    only. It never scans host directories or exposes storage paths.

    Args:
        filename: Optional exact display-name filter.
        limit: Maximum results to return, from 1 through 100.
    """

    if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
        return "Error: limit must be an integer between 1 and 100"
    if filename is not None and (not isinstance(filename, str) or not filename or PurePosixPath(filename).name != filename):
        return "Error: filename must be a single display name"

    try:
        authority = require_private_file_authority(
            runtime.context,
            method="visible_uploads",
        )
        if authority is None:
            return "Error: Historical uploads are available only in project runs"
        raw_uploads = authority.visible_uploads()
        if type(raw_uploads) is not tuple:
            raise RuntimeError("Private upload authority is unavailable")
        current_ids = _current_upload_ids(runtime, authority)
        uploads: list[dict[str, object]] = []
        for raw in raw_uploads:
            validated = _validated_upload(raw)
            if validated["file_version_id"] in current_ids:
                continue
            if filename is not None and raw.get("filename") != filename:
                continue
            uploads.append(validated)
    except RuntimeError:
        return "Error: Private upload authority is unavailable"

    uploads.sort(
        key=lambda item: (
            str(item["display_name"]).casefold(),
            str(item["file_version_id"]),
        )
    )
    selected = uploads[:limit]
    return json.dumps(
        {
            "count": len(selected),
            "files": selected,
            "omitted": max(0, len(uploads) - len(selected)),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
