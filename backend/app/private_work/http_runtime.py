from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.runtime_context import prepare_private_run_config
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus
from deerflow.runtime.secret_context import redact_config_secrets


def format_sse(
    event: str,
    data: object,
    *,
    event_id: str | None = None,
) -> str:
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines.extend(
        (
            f"event: {event}",
            f"data: {json.dumps(data, separators=(',', ':'))}",
        )
    )
    return "\n".join(lines) + "\n\n"


async def start_private_run(
    body: Any,
    thread_id: str,
    request: Request,
    context: PrivateWorkContext,
    *,
    run_id: str | None = None,
    server_context: PrivateRunAdmissionServerContext | Mapping[str, object] | None = None,
    admission_service: PrivateRunAdmissionService | None = None,
) -> RunRecord:
    """Persist a project-private Run and durable job for Worker execution."""

    if admission_service is None:
        admission_service = getattr(
            request.app.state,
            "private_run_admission_service",
            None,
        )
        if not isinstance(admission_service, PrivateRunAdmissionService):
            raise PrivateWorkUnavailable(context.request_id)

    raw_config = getattr(body, "config", None)
    raw_metadata = getattr(body, "metadata", None)
    raw_body_context = getattr(body, "context", None)
    config = prepare_private_run_config(
        thread_id=thread_id,
        opaque_scope=context.resource_scope,
        request_config=raw_config if isinstance(raw_config, Mapping) else None,
        metadata=raw_metadata if isinstance(raw_metadata, Mapping) else None,
        body_context=(raw_body_context if isinstance(raw_body_context, Mapping) else None),
    )
    persisted_context = dict(config.get("context", {}))
    persisted_context.pop("private_scope", None)
    persisted_config = {
        **config,
        "context": persisted_context,
        "configurable": dict(config.get("configurable", {})),
    }
    checkpoint = getattr(body, "checkpoint", None)
    checkpoint_id = getattr(checkpoint, "checkpoint_id", None)
    if isinstance(checkpoint_id, str) and checkpoint_id:
        persisted_config["configurable"]["checkpoint_id"] = checkpoint_id
    raw_command = getattr(body, "command", None)
    persisted_command = copy.deepcopy(raw_command) if isinstance(raw_command, Mapping) else None
    raw_stream_mode = getattr(body, "stream_mode", None)
    stream_mode = list(raw_stream_mode) if isinstance(raw_stream_mode, list) else ["values"]
    create_request = PrivateRunCreate(
        run_id=run_id or str(uuid.uuid4()),
        assistant_id=None,
        metadata=dict(config.get("metadata", {})),
        kwargs={
            "input": copy.deepcopy(getattr(body, "input", None)),
            "config": redact_config_secrets(persisted_config),
            "command": persisted_command,
            "stream_mode": stream_mode,
            "stream_subgraphs": getattr(body, "stream_subgraphs", False) is True,
        },
        multitask_strategy=getattr(body, "multitask_strategy", "reject"),
    )
    if type(server_context) is PrivateRunAdmissionServerContext:
        trusted_admission_context = server_context
    elif isinstance(server_context, Mapping) and server_context.get("non_interactive") is True:
        trusted_admission_context = PrivateRunAdmissionServerContext(
            non_interactive=True,
        )
    else:
        trusted_admission_context = None
    admitted = await admission_service.admit(
        context,
        thread_id,
        create_request,
        server_context=trusted_admission_context,
    )
    return RunRecord(
        run_id=admitted.run.run_id,
        thread_id=admitted.thread_id,
        assistant_id=admitted.run.assistant_id,
        status=RunStatus(admitted.run.status),
        on_disconnect=DisconnectMode(
            getattr(body, "on_disconnect", DisconnectMode.cancel.value),
        ),
        multitask_strategy=admitted.run.multitask_strategy,
        metadata=admitted.run.metadata,
        kwargs=admitted.run.kwargs,
        scope=admitted.opaque_runtime_scope,
        user_id=admitted.run.owner_user_id,
        created_at=admitted.run.created_at.isoformat(),
        updated_at=admitted.run.updated_at.isoformat(),
        model_name=admitted.run.model_name,
        store_only=True,
    )


__all__ = ["format_sse", "start_private_run"]
