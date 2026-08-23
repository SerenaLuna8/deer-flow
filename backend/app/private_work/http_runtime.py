from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from fastapi import Request

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkInvalid, PrivateWorkUnavailable
from app.private_work.execution_profile import RequestedRunExecutionProfile
from app.private_work.inbound_dedupe import DuplicateInboundDelivery
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.runtime_context import prepare_private_run_config
from app.private_work.workload_profile import RequestedRunWorkloadProfile
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus
from deerflow.runtime.secret_context import redact_config_secrets
from deerflow.trace_context import (
    generate_trace_id,
    get_current_trace_id,
    normalize_trace_id,
)


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
    raw_execution_profile = getattr(body, "execution_profile", None)
    try:
        execution_profile = RequestedRunExecutionProfile(
            model_name=getattr(raw_execution_profile, "model_name", None),
            thinking_enabled=getattr(
                raw_execution_profile,
                "thinking_enabled",
                None,
            ),
            reasoning_effort=getattr(
                raw_execution_profile,
                "reasoning_effort",
                None,
            ),
        )
        workload_profile = RequestedRunWorkloadProfile(
            name=getattr(body, "workload_profile", "interactive"),
        )
    except TypeError:
        raise PrivateWorkInvalid(context.request_id) from None
    origin_trace_id = get_current_trace_id() or normalize_trace_id(context.request_id) or generate_trace_id()
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
        origin_trace_id=origin_trace_id,
        execution_profile=execution_profile,
        workload_profile=workload_profile,
    )
    if type(server_context) is PrivateRunAdmissionServerContext:
        trusted_admission_context = replace(
            server_context,
            origin_trace_id=origin_trace_id,
        )
    elif isinstance(server_context, Mapping) and server_context.get("non_interactive") is True:
        trusted_admission_context = PrivateRunAdmissionServerContext(
            non_interactive=True,
            origin_trace_id=origin_trace_id,
        )
    else:
        trusted_admission_context = PrivateRunAdmissionServerContext(
            origin_trace_id=origin_trace_id,
        )
    admitted = await admission_service.admit(
        context,
        thread_id,
        create_request,
        server_context=trusted_admission_context,
    )
    if admitted.inbound_delivery_replay:
        raise DuplicateInboundDelivery(admitted.run.run_id)
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
