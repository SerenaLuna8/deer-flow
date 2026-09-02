from __future__ import annotations

import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Response, status

from app.gateway.routers.project_asset_routes.common import (
    ASSET_ERRORS,
    _agent_asset_item,
    _agent_definition_call,
    _agent_definition_response,
    _asset_call,
    _current_version_asset_call,
    _decode_skill_files,
    _version_call,
    _version_history,
    get_agent_service,
    get_mcp_service,
    get_skill_service,
    raise_asset_domain,
)
from app.gateway.routers.project_asset_routes.contracts import (
    AgentAssetMutationResponse,
    AgentCapabilityBindingsRequest,
    AgentCreateRequest,
    AgentDefinitionResponse,
    AgentInstructionsRequest,
    AssetMutationResponse,
    CreateAssetRequest,
    CurrentVersionAssetMutationResponse,
    ExpectedAssetVersionRequest,
    ExpectedRevisionRequest,
    McpVersionHistoryResponse,
    McpVersionRequest,
    McpVersionResponse,
    SkillActivationRequest,
    SkillDeleteResponse,
    SkillVersionHistoryResponse,
    SkillVersionRequest,
    SkillVersionResponse,
)
from app.gateway.routers.project_asset_routes.mcp import _mcp_definition
from app.shared_assets import (
    AgentCapabilityBindings,
    AgentInstructions,
    AgentPayload,
    AssetStorageUnavailable,
    CreateAgent,
    CreateMcpServer,
    SkillAssetRef,
)


def register_asset_routes(
    router: APIRouter,
    actor_dependency,
    *,
    include_shared_asset_mutations: bool = True,
    include_project_asset_delete: bool = False,
    include_skill_export: bool = True,
) -> None:
    async def create_agent(body: AgentCreateRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        try:
            result = await service.create_project(
                actor,
                CreateAgent(body.slug, body.display_name),
                AgentPayload(
                    description=body.description,
                    agents_instructions=body.agents_instructions,
                    soul=body.soul,
                    identity=body.identity,
                    user_context=body.user_context,
                    model_ref=body.model_ref,
                    model_settings=body.model_settings,
                    tool_groups=tuple(body.tool_groups),
                    skill_refs=tuple(SkillAssetRef(item.scope, item.asset_id) for item in body.skill_refs),
                    mcp_version_ids=tuple(body.mcp_version_ids),
                ),
            )
            return _agent_definition_response(result, actor.request_id)
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    async def get_agent(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        return await _agent_definition_call(
            actor,
            lambda: service.get(actor, asset_id),
        )

    async def update_agent_instructions(asset_id: uuid.UUID, body: AgentInstructionsRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        instructions = AgentInstructions(
            agents_instructions=body.agents_instructions,
            soul=body.soul,
            identity=body.identity,
            user_context=body.user_context,
        )
        return await _agent_definition_call(
            actor,
            lambda: service.update_instructions(
                actor,
                asset_id,
                instructions,
                expected_asset_version=body.expected_revision,
            ),
        )

    async def update_agent_capability_bindings(
        asset_id: uuid.UUID,
        body: AgentCapabilityBindingsRequest,
        actor=Depends(actor_dependency),
        service=Depends(get_agent_service),
    ):
        return await _agent_definition_call(
            actor,
            lambda: service.update_capability_bindings(
                actor,
                asset_id,
                AgentCapabilityBindings(
                    tuple(SkillAssetRef(item.scope, item.asset_id) for item in body.skill_refs),
                    tuple(body.mcp_version_ids),
                ),
                expected_asset_version=body.expected_revision,
            ),
        )

    async def delete_agent(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(get_agent_service)):
        try:
            await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_revision,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def get_skill(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _current_version_asset_call(
            actor,
            lambda: service.get(actor, asset_id),
        )

    async def create_skill_version(asset_id: uuid.UUID, body: SkillVersionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        files = _decode_skill_files(body, actor.request_id)
        return await _version_call(actor, lambda: service.create_version_from_archive(actor, asset_id, files, expected_asset_version=body.expected_revision), SkillVersionResponse)

    async def get_skill_versions(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _version_history(actor, lambda: service.get_version_history(actor, asset_id), SkillVersionHistoryResponse)

    async def activate_skill_version(asset_id: uuid.UUID, version_id: uuid.UUID, body: SkillActivationRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        return await _version_call(
            actor,
            lambda: service.activate_version(
                actor,
                asset_id,
                version_id,
                expected_asset_version=body.expected_revision,
                expected_payload_checksum=body.expected_payload_checksum,
                expected_secret_revision=body.expected_secret_revision,
            ),
            SkillVersionResponse,
        )

    async def delete_skill(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(get_skill_service)):
        try:
            result = await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_revision,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return SkillDeleteResponse(
            skill_id=asset_id,
            affected_agent_count=result.affected_agent_count,
            request_id=actor.request_id,
        )

    async def create_mcp(body: CreateAssetRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.create_asset(actor, CreateMcpServer(body.slug, body.display_name)))

    async def get_mcp(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _asset_call(actor, lambda: service.get(actor, asset_id))

    async def create_mcp_version(asset_id: uuid.UUID, body: McpVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_call(actor, lambda: service.create_version(actor, asset_id, _mcp_definition(body), expected_asset_version=body.expected_asset_version), McpVersionResponse)

    async def delete_mcp(asset_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        try:
            await service.delete(
                actor,
                asset_id,
                expected_asset_version=body.expected_asset_version,
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def get_mcp_versions(asset_id: uuid.UUID, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_history(actor, lambda: service.get_version_history(actor, asset_id), McpVersionHistoryResponse)

    async def publish_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_call(actor, lambda: service.publish(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), McpVersionResponse)

    async def submit_mcp(asset_id: uuid.UUID, version_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(get_mcp_service)):
        return await _version_call(actor, lambda: service.submit_approval(actor, asset_id, version_id, expected_asset_version=body.expected_asset_version), McpVersionResponse)

    def add_status_route(
        segment: str,
        action: Literal["activate", "archive", "enable", "suspend"],
        service_dependency,
    ) -> None:
        async def change_current(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            async def operation():
                return await getattr(service, action)(
                    actor,
                    asset_id,
                    expected_asset_version=body.expected_revision,
                )

            return await _current_version_asset_call(actor, operation)

        async def change_legacy(asset_id: uuid.UUID, body: ExpectedAssetVersionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            async def operation():
                return await getattr(service, action)(
                    actor,
                    asset_id,
                    expected_asset_version=body.expected_asset_version,
                )

            return await _asset_call(actor, operation)

        current_contract = segment == "skills"
        agent_contract = segment == "agents"

        async def change_agent(asset_id: uuid.UUID, body: ExpectedRevisionRequest, actor=Depends(actor_dependency), service=Depends(service_dependency)):
            try:
                result = await getattr(service, action)(
                    actor,
                    asset_id,
                    expected_asset_version=body.expected_revision,
                )
                return AgentAssetMutationResponse(
                    item=_agent_asset_item(result),
                    request_id=actor.request_id,
                )
            except ASSET_ERRORS as exc:
                raise_asset_domain(exc)

        router.add_api_route(
            f"/{segment}/{{asset_id}}/{action}",
            change_agent if agent_contract else (change_current if current_contract else change_legacy),
            methods=["POST"],
            response_model=(AgentAssetMutationResponse if agent_contract else (CurrentVersionAssetMutationResponse if current_contract else AssetMutationResponse)),
            name=f"{action}_{segment}",
        )

    async def export_skill_version(
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        actor=Depends(actor_dependency),
        service=Depends(get_skill_service),
    ):
        try:
            package = await service.export_distribution_package(
                actor,
                asset_id,
                version_id,
            )
            if (
                not isinstance(package.filename, str)
                or re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?-v[1-9][0-9]*\.zip",
                    package.filename,
                )
                is None
                or not isinstance(package.content, bytes)
            ):
                raise AssetStorageUnavailable(actor.request_id)
            return Response(
                content=package.content,
                media_type="application/zip",
                headers={
                    "Content-Disposition": (f'attachment; filename="{package.filename}"'),
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except ASSET_ERRORS as exc:
            raise_asset_domain(exc)

    read_routes = (
        ("/agents/{asset_id}", get_agent, ["GET"], AgentDefinitionResponse, 200),
        ("/skills/{asset_id}", get_skill, ["GET"], CurrentVersionAssetMutationResponse, 200),
        ("/skills/{asset_id}/versions", get_skill_versions, ["GET"], SkillVersionHistoryResponse, 200),
        ("/mcp-servers/{asset_id}", get_mcp, ["GET"], AssetMutationResponse, 200),
        ("/mcp-servers/{asset_id}/versions", get_mcp_versions, ["GET"], McpVersionHistoryResponse, 200),
    )
    shared_asset_write_routes = (
        ("/agents", create_agent, ["POST"], AgentDefinitionResponse, 201),
        ("/agents/{asset_id}/instructions", update_agent_instructions, ["PUT"], AgentDefinitionResponse, 200),
        ("/agents/{asset_id}/capability-bindings", update_agent_capability_bindings, ["PUT"], AgentDefinitionResponse, 200),
        ("/skills/{asset_id}/versions", create_skill_version, ["POST"], SkillVersionResponse, 201),
        ("/skills/{asset_id}/versions/{version_id}/activate", activate_skill_version, ["POST"], SkillVersionResponse, 200),
        ("/mcp-servers", create_mcp, ["POST"], AssetMutationResponse, 201),
        ("/mcp-servers/{asset_id}/versions", create_mcp_version, ["POST"], McpVersionResponse, 201),
        ("/mcp-servers/{asset_id}/versions/{version_id}/publish", publish_mcp, ["POST"], McpVersionResponse, 200),
        ("/mcp-servers/{asset_id}/versions/{version_id}/submit-approval", submit_mcp, ["POST"], McpVersionResponse, 200),
    )
    routes = read_routes
    if include_shared_asset_mutations:
        routes = (*routes, *shared_asset_write_routes)
    for path, endpoint, methods, response_model, code in routes:
        router.add_api_route(path, endpoint, methods=methods, response_model=response_model, status_code=code)
    if include_skill_export:
        router.add_api_route(
            "/skills/{asset_id}/versions/{version_id}/export",
            export_skill_version,
            methods=["GET"],
            response_model=None,
            status_code=status.HTTP_200_OK,
            name="export_skill_version",
        )
    if include_project_asset_delete:
        router.add_api_route(
            "/agents/{asset_id}",
            delete_agent,
            methods=["DELETE"],
            response_model=None,
            status_code=status.HTTP_204_NO_CONTENT,
            name="delete_project_agent",
        )
        router.add_api_route(
            "/skills/{asset_id}",
            delete_skill,
            methods=["DELETE"],
            response_model=SkillDeleteResponse,
            status_code=status.HTTP_200_OK,
            name="delete_project_skill",
        )
        router.add_api_route(
            "/mcp-servers/{asset_id}",
            delete_mcp,
            methods=["DELETE"],
            response_model=None,
            status_code=status.HTTP_204_NO_CONTENT,
            name="delete_project_mcp",
        )
    if include_shared_asset_mutations:
        add_status_route("agents", "enable", get_agent_service)
        add_status_route("agents", "suspend", get_agent_service)
        add_status_route("mcp-servers", "archive", get_mcp_service)
        add_status_route("mcp-servers", "activate", get_mcp_service)
        add_status_route("mcp-servers", "suspend", get_mcp_service)
        add_status_route("skills", "enable", get_skill_service)
        add_status_route("skills", "suspend", get_skill_service)
