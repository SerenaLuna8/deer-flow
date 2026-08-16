"""Project-scoped auxiliary model call for polishing composer drafts."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import deerflow.utils.llm_text as llm_text
from app.gateway.deps import (
    get_current_agent_runtime_config,
    project_input_polish_context,
    require_project_private_open,
)
from app.gateway.private_work_schemas import PrivateWorkRoute, StrictPrivateWorkRequest
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import AssetKind, AssetSelection, ResolvedAgentSnapshot
from app.shared_assets.resolver import ProjectAssetResolver
from app.system_settings import (
    SystemModelMaterializationUnavailable,
    SystemModelMaterializer,
)
from deerflow.config.app_config import AppConfig
from deerflow.mcp_definition_policy import (
    McpEndpointPolicy,
    NetworkMcpEndpointPolicy,
)
from deerflow.models import ModelRuntimeProfile
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id
from deerflow.utils.oneshot_llm import run_oneshot_llm

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/private-work",
    tags=["project-input-polish"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)


class ProjectInputPolishRequest(StrictPrivateWorkRequest):
    text: str = Field(..., description="Draft text currently shown in the composer")
    locale: str | None = Field(default=None, description="Optional UI locale hint")
    thread_id: uuid.UUID


class ProjectInputPolishResponse(StrictPrivateWorkRequest):
    model_config = ConfigDict(extra="forbid", strict=True)

    rewritten_text: str
    changed: bool


def _clean_rewritten_text(text: str) -> str:
    candidate = llm_text.strip_think_blocks(text, truncate_unclosed=False)
    candidate = llm_text.strip_markdown_code_fence(candidate)
    return candidate.strip()


def _build_system_instruction() -> str:
    return (
        "You are ActWeave's pre-send prompt optimizer.\n"
        "Rewrite the user's rough draft into a clearer instruction for an AI agent before it is sent.\n"
        "Do not answer the task.\n"
        "Preserve the user's language, intent, entities, file paths, URLs, code blocks, and any leading slash command prefix exactly.\n"
        "Improve the draft by making the goal, scope, constraints, and desired output explicit when they are implied by the draft.\n"
        "Do not invent facts, business context, tools, file names, dates, metrics, or user preferences that are not implied.\n"
        "Prefer one concise paragraph or a short bullet list. Output only the rewritten draft."
    )


def _build_user_content(text: str, locale: str | None) -> str:
    locale_hint = locale.strip() if locale else "same language as the draft"
    return f"Locale hint: {locale_hint}\n\nRewrite this draft while preserving its intent:\n<draft>\n{text}\n</draft>"


class ProjectInputPolishService:
    """Validate exact project/Agent authority before one auxiliary model call."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: ProjectAssetResolver | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        model_materializer: SystemModelMaterializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._snapshots = RunSnapshotRepository(
            session_factory,
            endpoint_policy=endpoint_policy,
        )
        self._revalidator = PrivateWorkRevalidator()
        self._model_materializer = model_materializer

    async def validate_authority(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> ResolvedAgentSnapshot:
        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    lock=True,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)
                resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
                    session,
                    current,
                    AssetSelection(AssetKind.AGENT, thread.agent_asset_id),
                )
                if type(resolved) is not ResolvedAgentSnapshot or resolved.scope.value != thread.agent_scope:
                    raise PrivateWorkAssetStale(context.request_id)
                await self._snapshots.validate_agent_closure_in_session(
                    session,
                    context,
                    resolved,
                )
                return resolved
        except PrivateWorkError:
            raise
        except (AssetForbidden, AssetValidationFailed):
            raise PrivateWorkAssetStale(context.request_id) from None
        except (AssetResolutionUnavailable, RunSnapshotAssetStale):
            raise PrivateWorkAssetStale(context.request_id) from None
        except (AssetStorageUnavailable, DBAPIError):
            raise PrivateWorkUnavailable(context.request_id) from None

    async def polish(
        self,
        *,
        context: PrivateWorkContext,
        body: ProjectInputPolishRequest,
        config: AppConfig,
    ) -> ProjectInputPolishResponse:
        text = body.text.strip()
        if not text:
            raise PrivateWorkInvalid(context.request_id)
        if len(text) > config.input_polish.max_chars:
            raise PrivateWorkInvalid(context.request_id)
        if not config.input_polish.enabled:
            raise PrivateWorkNotFound(context.request_id)

        resolved = await self.validate_authority(
            context,
            str(body.thread_id),
        )
        try:
            runtime_config = config
            model_name = config.input_polish.model_name
            if self._model_materializer is not None:
                runtime_model = await self._model_materializer.materialize_active(
                    model_name or resolved.payload.model_ref,
                )
                runtime_config = config.with_runtime_models((runtime_model,))
                model_name = runtime_model.name
            raw = await run_oneshot_llm(
                system_instruction=_build_system_instruction(),
                user_content=_build_user_content(text, body.locale),
                run_name="project_input_polish",
                app_config=runtime_config,
                model_name=model_name,
                profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
            )
            rewritten = _clean_rewritten_text(raw)
        except SystemModelMaterializationUnavailable as exc:
            raise PrivateWorkUnavailable(context.request_id) from exc
        except Exception as exc:
            logger.exception(
                "Project input polish model call failed: request_id=%s",
                context.request_id,
            )
            raise PrivateWorkUnavailable(context.request_id) from exc
        if not rewritten:
            raise PrivateWorkUnavailable(context.request_id)
        return ProjectInputPolishResponse(
            rewritten_text=rewritten,
            changed=rewritten != text,
        )


def project_input_polish_service(request: Request) -> ProjectInputPolishService:
    service = getattr(request.app.state, "project_input_polish_service", None)
    if isinstance(service, ProjectInputPolishService):
        return service
    endpoint_policy = getattr(request.app.state, "mcp_endpoint_policy", None)
    if not isinstance(endpoint_policy, NetworkMcpEndpointPolicy):
        raise private_work_http_exception(PrivateWorkUnavailable(get_current_trace_id() or generate_trace_id()))
    service = ProjectInputPolishService(
        get_session_factory(),
        endpoint_policy=endpoint_policy,
        model_materializer=getattr(
            request.app.state,
            "system_model_materializer",
            None,
        ),
    )
    request.app.state.project_input_polish_service = service
    return service


@router.post("/input-polish", response_model=ProjectInputPolishResponse)
async def polish_project_input(
    project_id: uuid.UUID,
    body: ProjectInputPolishRequest,
    context: PrivateWorkContext = Depends(project_input_polish_context),
    service: ProjectInputPolishService = Depends(project_input_polish_service),
    config: AppConfig = Depends(get_current_agent_runtime_config),
) -> ProjectInputPolishResponse:
    del project_id
    try:
        return await service.polish(context=context, body=body, config=config)
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None


__all__ = [
    "ProjectInputPolishRequest",
    "ProjectInputPolishResponse",
    "ProjectInputPolishService",
    "project_input_polish_service",
    "router",
]
