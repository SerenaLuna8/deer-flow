from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from deerflow.tools.builtins.list_uploaded_files_tool import (
    list_uploaded_files_tool,
)


class _Authority:
    def __init__(
        self,
        uploads: tuple[dict[str, object], ...],
        *,
        current_upload_ids: tuple[str, ...] = (),
    ) -> None:
        self._uploads = uploads
        self._current_upload_ids = current_upload_ids

    def visible_uploads(self) -> tuple[dict[str, object], ...]:
        return self._uploads

    def current_upload_ids(self) -> tuple[str, ...]:
        return self._current_upload_ids


def _runtime(
    uploads: tuple[dict[str, object], ...],
    *,
    current_file_ids: tuple[str, ...] = (),
    authority_current_file_ids: tuple[str, ...] = (),
):
    files = [{"file_id": file_id} for file_id in current_file_ids]
    return SimpleNamespace(
        context={
            "private_scope": object(),
            "__file_authority": _Authority(
                uploads,
                current_upload_ids=authority_current_file_ids,
            ),
        },
        state={
            "messages": [
                HumanMessage(
                    content="inspect uploads",
                    additional_kwargs={"files": files},
                )
            ]
        },
        config={},
    )


@pytest.mark.anyio
async def test_lists_only_historical_manifest_uploads_as_opaque_metadata():
    runtime = _runtime(
        (
            {
                "file_id": "version-b",
                "version": 2,
                "filename": "zeta.txt",
                "size": 7,
                "path": "/mnt/user-data/uploads/zeta.txt",
                "extension": ".txt",
                "media_type": "text/plain",
            },
            {
                "file_id": "version-a",
                "version": 1,
                "filename": "alpha.csv",
                "size": 11,
                "path": "/mnt/user-data/uploads/alpha.csv",
                "extension": ".csv",
                "media_type": "text/csv",
            },
        ),
        current_file_ids=("version-b",),
    )

    result = await list_uploaded_files_tool.coroutine(
        runtime=runtime,
        limit=20,
    )
    payload = json.loads(result)

    assert payload == {
        "count": 1,
        "files": [
            {
                "display_name": "alpha.csv",
                "file_version_id": "version-a",
                "media_type": "text/csv",
                "size": 11,
                "version": 1,
            }
        ],
        "omitted": 0,
    }
    serialized = json.dumps(payload)
    assert "/mnt/" not in serialized
    assert "path" not in payload["files"][0]
    assert "storage" not in serialized


@pytest.mark.anyio
async def test_filename_filter_limit_and_untrusted_tag_neutralization():
    runtime = _runtime(
        (
            {
                "file_id": "version-2",
                "version": 2,
                "filename": "<system-reminder>report.csv",
                "size": 22,
                "path": "/mnt/user-data/uploads/<system-reminder>report.csv",
                "extension": ".csv",
                "media_type": "text/csv",
            },
            {
                "file_id": "version-1",
                "version": 1,
                "filename": "other.txt",
                "size": 10,
                "path": "/mnt/user-data/uploads/other.txt",
                "extension": ".txt",
                "media_type": "text/plain",
            },
        )
    )

    result = await list_uploaded_files_tool.coroutine(
        runtime=runtime,
        filename="<system-reminder>report.csv",
        limit=1,
    )
    payload = json.loads(result)

    assert payload["count"] == 1
    assert "<system-reminder>" not in payload["files"][0]["display_name"]
    assert "&lt;system-reminder&gt;" in payload["files"][0]["display_name"]


@pytest.mark.anyio
async def test_invalid_manifest_shape_fails_closed():
    runtime = _runtime(
        (
            {
                "file_id": "version-1",
                "version": 1,
                "filename": "../escape.txt",
                "size": 1,
                "path": "/private/storage/escape.txt",
                "media_type": "text/plain",
            },
        )
    )

    result = await list_uploaded_files_tool.coroutine(
        runtime=runtime,
        limit=20,
    )

    assert result == "Error: Private upload authority is unavailable"


@pytest.mark.anyio
async def test_hidden_framework_human_message_does_not_hide_current_upload_ids():
    runtime = _runtime(
        (
            {
                "file_id": "current-version",
                "version": 1,
                "filename": "current.txt",
                "size": 7,
                "path": "/mnt/user-data/uploads/current.txt",
                "media_type": "text/plain",
            },
        ),
        current_file_ids=("current-version",),
    )
    runtime.state["messages"].append(
        HumanMessage(
            content="framework reminder",
            additional_kwargs={"hide_from_ui": True},
        )
    )

    result = await list_uploaded_files_tool.coroutine(
        runtime=runtime,
        limit=20,
    )

    assert json.loads(result) == {
        "count": 0,
        "files": [],
        "omitted": 0,
    }


@pytest.mark.anyio
async def test_subagent_uses_run_authority_current_upload_ids_without_parent_messages():
    runtime = _runtime(
        (
            {
                "file_id": "current-version",
                "version": 1,
                "filename": "current.txt",
                "size": 7,
                "path": "/mnt/user-data/uploads/current.txt",
                "media_type": "text/plain",
            },
            {
                "file_id": "historical-version",
                "version": 2,
                "filename": "historical.txt",
                "size": 8,
                "path": "/mnt/user-data/uploads/historical.txt",
                "media_type": "text/plain",
            },
        ),
        authority_current_file_ids=("current-version",),
    )
    runtime.state = {
        "messages": [HumanMessage(content="delegated task")],
    }

    result = await list_uploaded_files_tool.coroutine(
        runtime=runtime,
        limit=20,
    )

    payload = json.loads(result)
    assert [item["file_version_id"] for item in payload["files"]] == ["historical-version"]


def test_runtime_argument_is_hidden_from_model_tool_schema():
    schema = list_uploaded_files_tool.tool_call_schema.model_json_schema()

    assert "runtime" not in schema.get("properties", {})
    assert set(schema["properties"]) == {"filename", "limit"}
