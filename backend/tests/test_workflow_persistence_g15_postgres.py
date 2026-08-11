from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from support.private_thread_seed import seed_private_thread_database

from deerflow.persistence.workflows.sql import (
    WorkflowAuthorityMissing,
    WorkflowCodeRequirementCreate,
    WorkflowControlOperationCreate,
    WorkflowCredentialGrantConflict,
    WorkflowCredentialGrantPut,
    WorkflowCredentialSlotCreate,
    WorkflowDefinitionArchive,
    WorkflowDefinitionConflict,
    WorkflowDefinitionCreate,
    WorkflowDefinitionListQuery,
    WorkflowDefinitionUpdate,
    WorkflowDraftCASConflict,
    WorkflowDraftUpdate,
    WorkflowHttpRequirementCreate,
    WorkflowModelRefCreate,
    WorkflowPublishIdempotencyConflict,
    WorkflowRepository,
    WorkflowVersionPublish,
)

pytestmark = pytest.mark.postgres


async def _create_definition(
    repository: WorkflowRepository,
    *,
    project_id: uuid.UUID,
    actor_id: str,
    name: str,
    checksum: str,
) -> uuid.UUID:
    definition, _draft = await repository.create_definition(
        project_id=project_id,
        actor_user_id=actor_id,
        command=WorkflowDefinitionCreate(
            name=name,
            description=f"{name} description",
            spec_schema_version=1,
            canvas_schema_version=1,
            spec={"schema_version": 1, "nodes": []},
            canvas={"schema_version": 1, "viewport": {"x": 0, "y": 0}},
            draft_checksum=checksum,
        ),
    )
    assert definition.draft_revision == 1
    assert definition.draft_checksum == checksum
    assert definition.current_published_version_number is None
    return definition.workflow_id


async def _seed_project_credential(
    session,
    *,
    project_id: uuid.UUID,
    actor_id: str,
    name: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """INSERT INTO credentials
               (id,scope,project_id,name,display_name,credential_type,status,
                is_delete,version,created_by_user_id)
               VALUES (:id,'project',:project,:name,:display,'http_bearer',
                       'active',false,1,:actor)"""
        ),
        {
            "id": credential_id,
            "project": project_id,
            "name": name,
            "display": name,
            "actor": actor_id,
        },
    )
    await session.execute(
        sa.text(
            """INSERT INTO credential_versions
               (id,credential_id,version_number,status,payload_schema_version,
                payload_schema,created_by_user_id)
               VALUES (:id,:credential,1,'active',1,'{}',:actor)"""
        ),
        {
            "id": credential_version_id,
            "credential": credential_id,
            "actor": actor_id,
        },
    )
    await session.execute(
        sa.text(
            """UPDATE credentials SET current_version_id=:version
               WHERE id=:credential"""
        ),
        {"version": credential_version_id, "credential": credential_id},
    )
    return credential_id, credential_version_id


def _grant_command(
    credential_id: uuid.UUID,
    credential_version_id: uuid.UUID,
    checksum: str,
) -> WorkflowCredentialGrantPut:
    return WorkflowCredentialGrantPut(
        credential_id=credential_id,
        expected_credential_version_id=credential_version_id,
        expected_slot_schema_checksum=checksum,
        resolved_slot_schema_checksum=checksum,
    )


@pytest.mark.asyncio
async def test_definition_cursor_archive_and_complete_version_history(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    try:
        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            alpha_id = await _create_definition(
                repository,
                project_id=project_id,
                actor_id=actor_id,
                name="Alpha match",
                checksum="1" * 64,
            )
            beta_id = await _create_definition(
                repository,
                project_id=project_id,
                actor_id=actor_id,
                name="Beta match",
                checksum="2" * 64,
            )
            gamma_id = await _create_definition(
                repository,
                project_id=project_id,
                actor_id=actor_id,
                name="Gamma",
                checksum="3" * 64,
            )
            node_id = uuid.uuid4()
            code_node_id = uuid.uuid4()
            http_node_id = uuid.uuid4()
            first_publish = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=alpha_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=1,
                    expected_draft_checksum="1" * 64,
                    graph_schema_version=1,
                    canvas_schema_version=1,
                    compiler_contract_version=1,
                    semantic_checksum="a" * 64,
                    model_refs=(
                        WorkflowModelRefCreate(
                            node_id=node_id,
                            purpose="primary",
                            logical_model_name="test-model",
                        ),
                    ),
                    credential_slots=(
                        WorkflowCredentialSlotCreate(
                            slot_id="http.auth",
                            name="HTTP auth",
                            purpose="header",
                            payload_schema={
                                "type": "object",
                                "properties": {"token": {"type": "string"}},
                            },
                            payload_schema_checksum="c" * 64,
                        ),
                    ),
                    code_requirements=(
                        WorkflowCodeRequirementCreate(
                            node_id=code_node_id,
                            runtime_contract="python3.12-v1",
                        ),
                    ),
                    http_requirements=(
                        WorkflowHttpRequirementCreate(
                            node_id=http_node_id,
                            method="POST",
                            endpoint_policy_id="approved.api",
                            injection_profile_id="header.api-key",
                            credential_slot_id="http.auth",
                        ),
                    ),
                    idempotency_hash="d" * 64,
                    request_digest="e" * 64,
                ),
            )
            assert first_publish.created is True
            first_version = first_publish.record
            semantic_replay = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=alpha_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=1,
                    expected_draft_checksum="1" * 64,
                    graph_schema_version=1,
                    canvas_schema_version=1,
                    compiler_contract_version=1,
                    semantic_checksum="a" * 64,
                    idempotency_hash="7" * 64,
                    request_digest="8" * 64,
                ),
            )
            assert semantic_replay.created is False
            assert semantic_replay.record.version_id == first_version.version_id

            page_one = await repository.list_definitions(
                project_id,
                WorkflowDefinitionListQuery(
                    query="match",
                    lifecycle="active",
                    publication="all",
                    sort="name_asc",
                    limit=1,
                ),
            )
            assert [item.name for item in page_one.items] == ["Alpha match"]
            assert page_one.items[0].current_published_version_number == 1
            assert page_one.items[0].draft_revision == 1
            assert page_one.items[0].draft_checksum == "1" * 64
            assert page_one.next_cursor is not None
            page_two = await repository.list_definitions(
                project_id,
                WorkflowDefinitionListQuery(
                    query="match",
                    lifecycle="active",
                    publication="all",
                    sort="name_asc",
                    cursor=page_one.next_cursor,
                    limit=1,
                ),
            )
            assert [item.name for item in page_two.items] == ["Beta match"]
            assert page_two.next_cursor is None
            assert (
                await repository.list_definitions(
                    project_id,
                    WorkflowDefinitionListQuery(query="no such workflow"),
                )
            ).items == ()
            with pytest.raises(ValueError):
                await repository.list_definitions(
                    project_id,
                    WorkflowDefinitionListQuery(
                        lifecycle="archived",
                        cursor=page_one.next_cursor,
                    ),
                )

            published = await repository.list_definitions(
                project_id,
                WorkflowDefinitionListQuery(publication="published"),
            )
            assert [item.workflow_id for item in published.items] == [alpha_id]
            draft_only = await repository.list_definitions(
                project_id,
                WorkflowDefinitionListQuery(
                    publication="draft_only",
                    sort="name_asc",
                ),
            )
            assert [item.workflow_id for item in draft_only.items] == [beta_id, gamma_id]

            updated = await repository.update_definition(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=beta_id,
                command=WorkflowDefinitionUpdate(
                    expected_revision=1,
                    description="Updated description",
                ),
            )
            assert updated.description == "Updated description"
            assert updated.draft_revision == 1
            assert updated.draft_checksum == "2" * 64

            archived = await repository.archive_definition(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=gamma_id,
                command=WorkflowDefinitionArchive(expected_revision=1),
            )
            assert archived.status == "archived"
            assert archived.revision == 2
            assert archived.draft_revision == 1
            assert archived.draft_checksum == "3" * 64
            assert archived.current_published_version_number is None
            with pytest.raises(WorkflowDefinitionConflict):
                await repository.archive_definition(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=gamma_id,
                    command=WorkflowDefinitionArchive(expected_revision=1),
                )

            exact = await repository.get_version(
                project_id,
                alpha_id,
                first_version.version_id,
            )
            assert exact is not None
            assert exact.materialize_spec() == {"schema_version": 1, "nodes": []}
            assert exact.materialize_canvas()["viewport"] == {"x": 0, "y": 0}
            assert exact.model_refs[0].node_id == node_id
            assert exact.credential_slots[0].materialize_payload_schema()["type"] == "object"
            assert exact.code_requirements[0].node_id == code_node_id
            assert exact.code_requirements[0].runtime_contract == "python3.12-v1"
            assert exact.http_requirements[0].node_id == http_node_id
            assert exact.http_requirements[0].method == "POST"
            assert exact.http_requirements[0].endpoint_policy_id == "approved.api"
            assert exact.http_requirements[0].injection_profile_id == "header.api-key"
            assert exact.http_requirements[0].credential_slot_id == "http.auth"
            assert exact.published_by == actor_id
            assert exact.active_grants == ()
            assert exact.missing_required_slot_ids == ("http.auth",)
            assert exact.executable is False
            for statement in (
                "UPDATE workflow_version_code_requirements SET runtime_contract='python3.12-v1' WHERE workflow_version_id=:version",
                "DELETE FROM workflow_version_code_requirements WHERE workflow_version_id=:version",
                "UPDATE workflow_version_http_requirements SET method='GET' WHERE workflow_version_id=:version",
                "DELETE FROM workflow_version_http_requirements WHERE workflow_version_id=:version",
            ):
                with pytest.raises(sa.exc.DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            sa.text(statement),
                            {"version": first_version.version_id},
                        )
            with pytest.raises(sa.exc.DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        sa.text(
                            """INSERT INTO workflow_version_code_requirements
                               (workflow_version_id,project_id,node_id,runtime_contract)
                               VALUES (:version,:project,:node,'python3.13-v1')"""
                        ),
                        {
                            "version": first_version.version_id,
                            "project": project_id,
                            "node": uuid.uuid4(),
                        },
                    )
            with pytest.raises(sa.exc.DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        sa.text(
                            """INSERT INTO workflow_version_http_requirements
                               (workflow_version_id,project_id,node_id,method,
                                endpoint_policy_id,injection_profile_id,credential_slot_id)
                               VALUES (:version,:project,:node,'GET','approved.api',
                                       'header.api-key','missing.slot')"""
                        ),
                        {
                            "version": first_version.version_id,
                            "project": project_id,
                            "node": uuid.uuid4(),
                        },
                    )

            saved = await repository.save_draft(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=alpha_id,
                command=WorkflowDraftUpdate(
                    expected_revision=1,
                    spec_schema_version=1,
                    canvas_schema_version=1,
                    spec={"schema_version": 1, "nodes": [], "revision": "B"},
                    canvas={"schema_version": 1, "viewport": {"x": 1, "y": 2}},
                    draft_checksum="4" * 64,
                ),
            )
            second_publish = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=alpha_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=saved.revision,
                    expected_draft_checksum=saved.draft_checksum,
                    graph_schema_version=1,
                    canvas_schema_version=1,
                    compiler_contract_version=1,
                    semantic_checksum="b" * 64,
                    idempotency_hash="f" * 64,
                    request_digest="0" * 64,
                ),
            )
            assert second_publish.created is True
            second_version = second_publish.record
            history_one = await repository.list_version_history(
                project_id,
                alpha_id,
                limit=1,
            )
            assert [item.version_id for item in history_one.items] == [second_version.version_id]
            assert history_one.next_cursor is not None
            history_two = await repository.list_version_history(
                project_id,
                alpha_id,
                cursor=history_one.next_cursor,
                limit=1,
            )
            assert [item.version_id for item in history_two.items] == [first_version.version_id]
            assert history_two.items[0].code_requirements == exact.code_requirements
            assert history_two.items[0].http_requirements == exact.http_requirements
            assert history_two.next_cursor is None
            assert (
                await repository.list_version_history(
                    project_id,
                    beta_id,
                )
            ).items == ()
            with pytest.raises(WorkflowAuthorityMissing):
                await repository.list_version_history(project_id, uuid.uuid4())
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_definition_slot_contract_accepts_uppercase_underscore_and_colon_in_real_postgres(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    slot_ids = ("_Auth", "Auth", "foo:bar")
    try:
        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            workflow_id = await _create_definition(
                repository,
                project_id=project_id,
                actor_id=actor_id,
                name="Broad slot contract",
                checksum="1" * 64,
            )
            result = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=1,
                    expected_draft_checksum="1" * 64,
                    graph_schema_version=1,
                    canvas_schema_version=1,
                    compiler_contract_version=1,
                    semantic_checksum="2" * 64,
                    credential_slots=tuple(
                        WorkflowCredentialSlotCreate(
                            slot_id=slot_id,
                            name=slot_id,
                            purpose="http_auth",
                            payload_schema={
                                "type": "object",
                                "properties": {"token": {"type": "string"}},
                                "required": ["token"],
                                "additionalProperties": False,
                            },
                            payload_schema_checksum=str(index) * 64,
                        )
                        for index, slot_id in enumerate(slot_ids, start=1)
                    ),
                    idempotency_hash="3" * 64,
                    request_digest="4" * 64,
                ),
            )
            assert {slot.slot_id for slot in result.record.credential_slots} == set(slot_ids)
            stored = tuple(
                await session.scalars(
                    sa.text(
                        """SELECT slot_id FROM workflow_version_credential_slots
                           WHERE workflow_version_id=:version ORDER BY slot_id"""
                    ),
                    {"version": result.record.version_id},
                )
            )
            assert set(stored) == set(slot_ids)
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_control_receipt_recomputes_real_scope_and_db_rejects_forged_or_partial_shapes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    try:
        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            workflow_a = await _create_definition(
                repository,
                project_id=project_id,
                actor_id=actor_id,
                name="Scope A",
                checksum="1" * 64,
            )
            workflow_b = await _create_definition(
                repository,
                project_id=project_id,
                actor_id=actor_id,
                name="Scope B",
                checksum="2" * 64,
            )
            definition_a = await repository.get_definition(
                project_id,
                workflow_a,
            )
            assert definition_a is not None
            command = WorkflowControlOperationCreate(
                project_id=project_id,
                workflow_id=workflow_a,
                operation="update",
                idempotency_hash="3" * 64,
                request_digest="4" * 64,
                created_by=actor_id,
                result_revision=definition_a.revision,
                result_created_at=definition_a.created_at,
                result_updated_at=definition_a.updated_at,
                result_name=definition_a.name,
                result_description=definition_a.description,
                result_lifecycle="active",
                result_draft_revision=definition_a.draft_revision,
                result_draft_checksum=definition_a.draft_checksum,
            )
            recorded = await repository.record_control_operation(command)
            assert recorded.scope_key == f"definition:{workflow_a}"
            assert (
                await repository.get_control_operation(
                    project_id=project_id,
                    operation="update",
                    workflow_id=workflow_b,
                    idempotency_hash="3" * 64,
                    request_digest="4" * 64,
                )
                is None
            )
            assert (
                await repository.get_control_operation(
                    project_id=project_id,
                    operation="update",
                    workflow_id=workflow_a,
                    idempotency_hash="3" * 64,
                    request_digest="4" * 64,
                )
                == recorded
            )

            definition_receipt_sql = """INSERT INTO workflow_control_operations
                (project_id,workflow_id,operation,scope_key,idempotency_hash,
                 request_digest,created_by,result_revision,result_created_at,
                 result_updated_at,result_name,result_description,
                 result_lifecycle,result_draft_revision,result_draft_checksum{extra_columns})
                VALUES
                (:project,:workflow,'update',:scope,:key,:digest,:actor,1,
                 now(),now(),'Forged','', 'active',1,:draft_checksum{extra_values})"""
            with pytest.raises(sa.exc.DBAPIError) as forged_scope:
                async with session.begin_nested():
                    await session.execute(
                        sa.text(
                            definition_receipt_sql.format(
                                extra_columns="",
                                extra_values="",
                            )
                        ),
                        {
                            "project": project_id,
                            "workflow": workflow_a,
                            "scope": f"definition:{workflow_b}",
                            "key": "5" * 64,
                            "digest": "6" * 64,
                            "actor": actor_id,
                            "draft_checksum": "7" * 64,
                        },
                    )
            assert "ck_workflow_control_operations_scope_shape" in str(forged_scope.value)

            with pytest.raises(sa.exc.DBAPIError) as partial_credential:
                async with session.begin_nested():
                    await session.execute(
                        sa.text(
                            """INSERT INTO workflow_control_operations
                               (project_id,workflow_id,operation,scope_key,
                                idempotency_hash,request_digest,created_by,
                                result_slot_id,result_credential_id,
                                result_updated_at)
                               VALUES
                               (:project,:workflow,'draft_grant_put',:scope,
                                :key,:digest,:actor,'http.auth',:credential,now())"""
                        ),
                        {
                            "project": project_id,
                            "workflow": workflow_a,
                            "scope": f"draft-slot:{workflow_a}:http.auth",
                            "key": "7" * 64,
                            "digest": "8" * 64,
                            "actor": actor_id,
                            "credential": uuid.uuid4(),
                        },
                    )
            assert "ck_workflow_control_operations_credential_shape" in str(partial_credential.value)

            with pytest.raises(sa.exc.DBAPIError) as non_credential:
                async with session.begin_nested():
                    await session.execute(
                        sa.text(
                            definition_receipt_sql.format(
                                extra_columns=(",result_credential_id,result_credential_version_id,result_checksum"),
                                extra_values=",:credential,:credential_version,:checksum",
                            )
                        ),
                        {
                            "project": project_id,
                            "workflow": workflow_a,
                            "scope": f"definition:{workflow_a}",
                            "key": "9" * 64,
                            "digest": "a" * 64,
                            "actor": actor_id,
                            "draft_checksum": "b" * 64,
                            "credential": uuid.uuid4(),
                            "credential_version": uuid.uuid4(),
                            "checksum": "c" * 64,
                        },
                    )
            assert "ck_workflow_control_operations_credential_shape" in str(non_credential.value)
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_publish_idempotency_and_credential_intent_grant_rotation_are_durable(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    slot_checksum = "7" * 64
    try:
        async with seed.factory() as session, session.begin():
            repository = WorkflowRepository(session)
            workflow_id = await _create_definition(
                repository,
                project_id=project_id,
                actor_id=actor_id,
                name="Credential workflow",
                checksum="5" * 64,
            )
            first_credential = await _seed_project_credential(
                session,
                project_id=project_id,
                actor_id=actor_id,
                name=f"workflow-first-{uuid.uuid4().hex[:8]}",
            )
            second_credential = await _seed_project_credential(
                session,
                project_id=project_id,
                actor_id=actor_id,
                name=f"workflow-second-{uuid.uuid4().hex[:8]}",
            )
            intent = await repository.put_draft_grant_intent(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                slot_id="http.auth",
                resolved_draft_revision=1,
                command=_grant_command(*first_credential, slot_checksum),
            )
            assert intent.credential_id == first_credential[0]
            await repository.put_draft_grant_intent(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                slot_id="removed.slot",
                resolved_draft_revision=1,
                command=_grant_command(*first_credential, "8" * 64),
            )

            publish_command = WorkflowVersionPublish(
                expected_draft_revision=1,
                expected_draft_checksum="5" * 64,
                graph_schema_version=1,
                canvas_schema_version=1,
                compiler_contract_version=1,
                semantic_checksum="6" * 64,
                credential_slots=(
                    WorkflowCredentialSlotCreate(
                        slot_id="http.auth",
                        name="HTTP auth",
                        purpose="header",
                        payload_schema={"type": "object"},
                        payload_schema_checksum=slot_checksum,
                    ),
                    WorkflowCredentialSlotCreate(
                        slot_id="http.optional",
                        name="HTTP optional binding",
                        purpose="header",
                        payload_schema={"type": "object"},
                        payload_schema_checksum="8" * 64,
                    ),
                ),
                idempotency_hash="9" * 64,
                request_digest="a" * 64,
            )
            published = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=publish_command,
            )
            assert published.created is True
            version = published.record
            assert [grant.slot_id for grant in version.active_grants] == ["http.auth"]
            assert version.missing_required_slot_ids == ("http.optional",)
            assert version.executable is False
            await repository.save_draft(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=WorkflowDraftUpdate(
                    expected_revision=1,
                    spec_schema_version=1,
                    canvas_schema_version=1,
                    spec={"schema_version": 1, "nodes": [], "changed": True},
                    canvas={"schema_version": 1, "viewport": {"x": 3, "y": 4}},
                    draft_checksum="c" * 64,
                    credential_slot_ids=("http.auth",),
                ),
            )
            retained_intents = (
                (
                    await session.execute(
                        sa.text(
                            """SELECT slot_id,slot_schema_checksum
                             FROM workflow_draft_credential_grant_intents
                            WHERE project_id=:project AND workflow_id=:workflow
                            ORDER BY slot_id"""
                        ),
                        {"project": project_id, "workflow": workflow_id},
                    )
                )
                .mappings()
                .all()
            )
            assert retained_intents == [
                {
                    "slot_id": "http.auth",
                    "slot_schema_checksum": slot_checksum,
                }
            ]
            stale_publish = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=2,
                    expected_draft_checksum="c" * 64,
                    graph_schema_version=1,
                    canvas_schema_version=1,
                    compiler_contract_version=1,
                    semantic_checksum="d" * 64,
                    credential_slots=(
                        WorkflowCredentialSlotCreate(
                            slot_id="http.auth",
                            name="HTTP auth",
                            purpose="header",
                            payload_schema={"type": "object", "minProperties": 1},
                            payload_schema_checksum="e" * 64,
                        ),
                    ),
                    idempotency_hash="5" * 64,
                    request_digest="6" * 64,
                ),
            )
            assert stale_publish.created is True
            assert stale_publish.record.active_grants == ()
            assert stale_publish.record.missing_required_slot_ids == ("http.auth",)
            early_replay = await repository.get_publish_replay(
                project_id,
                workflow_id,
                "9" * 64,
                "a" * 64,
            )
            assert early_replay is not None
            assert early_replay.version_id == version.version_id
            assert (
                await repository.get_publish_replay(
                    uuid.uuid4(),
                    workflow_id,
                    "9" * 64,
                    "a" * 64,
                )
            ) is None
            with pytest.raises(WorkflowPublishIdempotencyConflict):
                await repository.get_publish_replay(
                    project_id,
                    workflow_id,
                    "9" * 64,
                    "b" * 64,
                )
            replay = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=publish_command,
            )
            assert replay.created is False
            assert replay.record.version_id == version.version_id
            with pytest.raises(WorkflowPublishIdempotencyConflict):
                await repository.publish_version(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=WorkflowVersionPublish(
                        expected_draft_revision=1,
                        expected_draft_checksum="5" * 64,
                        graph_schema_version=1,
                        canvas_schema_version=1,
                        compiler_contract_version=1,
                        semantic_checksum="6" * 64,
                        idempotency_hash="9" * 64,
                        request_digest="b" * 64,
                    ),
                )
            operation_count = await session.scalar(
                sa.text(
                    """SELECT count(*) FROM workflow_control_operations
                       WHERE workflow_id=:workflow AND idempotency_hash=:key"""
                ),
                {"workflow": workflow_id, "key": "9" * 64},
            )
            assert operation_count == 1
            for statement in (
                """UPDATE workflow_control_operations
                   SET request_digest=:changed
                   WHERE workflow_id=:workflow""",
                """DELETE FROM workflow_control_operations
                   WHERE workflow_id=:workflow""",
            ):
                with pytest.raises(sa.exc.DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            sa.text(statement),
                            {"changed": "f" * 64, "workflow": workflow_id},
                        )
            with pytest.raises(sa.exc.DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        sa.text(
                            """INSERT INTO workflow_control_operations
                               (project_id,workflow_id,operation,scope_key,idempotency_hash,
                                request_digest,result_version_id,created_by)
                               VALUES (:project,:workflow,'publish',:scope,:key,:digest,
                                       :version,:actor)"""
                        ),
                        {
                            "project": uuid.uuid4(),
                            "workflow": workflow_id,
                            "scope": f"definition:{workflow_id}",
                            "key": "1" * 64,
                            "digest": "2" * 64,
                            "version": version.version_id,
                            "actor": actor_id,
                        },
                    )
            with pytest.raises(sa.exc.DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        sa.text(
                            """INSERT INTO workflow_control_operations
                               (project_id,workflow_id,operation,scope_key,idempotency_hash,
                                request_digest,result_version_id,created_by)
                               VALUES (:project,:workflow,'publish',:scope,:key,:digest,
                                       :version,:actor)"""
                        ),
                        {
                            "project": project_id,
                            "workflow": workflow_id,
                            "scope": f"definition:{workflow_id}",
                            "key": "3" * 64,
                            "digest": "4" * 64,
                            "version": uuid.uuid4(),
                            "actor": actor_id,
                        },
                    )
            grants = (
                (
                    await session.execute(
                        sa.text(
                            """SELECT slot_id,status,credential_id
                           FROM workflow_credential_grants
                           WHERE workflow_version_id=:version
                           ORDER BY slot_id,status"""
                        ),
                        {"version": version.version_id},
                    )
                )
                .mappings()
                .all()
            )
            assert grants == [
                {
                    "slot_id": "http.auth",
                    "status": "active",
                    "credential_id": first_credential[0],
                }
            ]

            same = await repository.put_version_grant(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                version_id=version.version_id,
                slot_id="http.auth",
                command=_grant_command(*first_credential, slot_checksum),
            )
            rotated = await repository.put_version_grant(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                version_id=version.version_id,
                slot_id="http.auth",
                command=_grant_command(*second_credential, slot_checksum),
            )
            assert same.grant_id != rotated.grant_id
            assert rotated.status == "active"
            assert rotated.credential_id == second_credential[0]
            with pytest.raises(WorkflowCredentialGrantConflict):
                await repository.put_version_grant(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    version_id=version.version_id,
                    slot_id="http.auth",
                    command=WorkflowCredentialGrantPut(
                        credential_id=second_credential[0],
                        expected_credential_version_id=second_credential[1],
                        expected_slot_schema_checksum="0" * 64,
                        resolved_slot_schema_checksum="0" * 64,
                    ),
                )
            revoked = await repository.revoke_version_grant(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                version_id=version.version_id,
                slot_id="http.auth",
            )
            assert revoked is not None
            assert revoked.status == "revoked"
            assert revoked.revision == 2
            assert (
                await repository.revoke_version_grant(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    version_id=version.version_id,
                    slot_id="http.auth",
                )
            ) is None

            await repository.put_version_grant(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                version_id=version.version_id,
                slot_id="http.optional",
                command=_grant_command(*second_credential, "8" * 64),
            )
            late_publish_replay = await repository.get_publish_replay(
                project_id,
                workflow_id,
                "9" * 64,
                "a" * 64,
            )
            assert late_publish_replay is not None
            assert late_publish_replay.missing_required_slot_ids == ("http.optional",)
            assert late_publish_replay.executable is False
            command_replay = await repository.publish_version(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                command=publish_command,
            )
            assert command_replay.created is False
            assert command_replay.record.missing_required_slot_ids == ("http.optional",)
            assert command_replay.record.executable is False

            deleted = await repository.delete_draft_grant_intent(
                project_id=project_id,
                actor_user_id=actor_id,
                workflow_id=workflow_id,
                slot_id="http.auth",
                resolved_draft_revision=2,
            )
            assert deleted is not None
            assert (
                await repository.delete_draft_grant_intent(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    slot_id="http.auth",
                    resolved_draft_revision=2,
                )
            ) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_failed_publish_transaction_leaves_no_version_pointer_or_receipt(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    try:
        async with seed.factory() as session, session.begin():
            workflow_id = await _create_definition(
                WorkflowRepository(session),
                project_id=project_id,
                actor_id=actor_id,
                name="Rollback workflow",
                checksum="d" * 64,
            )

        class _ForceRollback(Exception):
            pass

        with pytest.raises(_ForceRollback):
            async with seed.factory() as session, session.begin():
                await WorkflowRepository(session).publish_version(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=WorkflowVersionPublish(
                        expected_draft_revision=1,
                        expected_draft_checksum="d" * 64,
                        graph_schema_version=1,
                        canvas_schema_version=1,
                        compiler_contract_version=1,
                        semantic_checksum="e" * 64,
                        credential_slots=(
                            WorkflowCredentialSlotCreate(
                                slot_id="http.auth",
                                name="HTTP auth",
                                purpose="header",
                                payload_schema={"type": "object"},
                                payload_schema_checksum="1" * 64,
                            ),
                        ),
                        code_requirements=(
                            WorkflowCodeRequirementCreate(
                                node_id=uuid.uuid4(),
                                runtime_contract="python3.12-v1",
                            ),
                        ),
                        http_requirements=(
                            WorkflowHttpRequirementCreate(
                                node_id=uuid.uuid4(),
                                method="GET",
                                endpoint_policy_id="approved.api",
                                injection_profile_id="header.api-key",
                                credential_slot_id="http.auth",
                            ),
                        ),
                        idempotency_hash="f" * 64,
                        request_digest="0" * 64,
                    ),
                )
                raise _ForceRollback

        async with seed.factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        sa.text(
                            """SELECT current_published_version_id,
                                  (SELECT count(*) FROM workflow_versions
                                    WHERE workflow_id=:workflow) AS versions,
                                  (SELECT count(*) FROM workflow_version_code_requirements r
                                     JOIN workflow_versions v ON v.id=r.workflow_version_id
                                    WHERE v.workflow_id=:workflow) AS code_requirements,
                                  (SELECT count(*) FROM workflow_version_http_requirements r
                                     JOIN workflow_versions v ON v.id=r.workflow_version_id
                                    WHERE v.workflow_id=:workflow) AS http_requirements,
                                  (SELECT count(*)
                                     FROM workflow_control_operations
                                    WHERE workflow_id=:workflow) AS operations
                             FROM workflow_definitions
                            WHERE id=:workflow AND project_id=:project"""
                        ),
                        {"workflow": workflow_id, "project": project_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row == {
                "current_published_version_id": None,
                "versions": 0,
                "code_requirements": 0,
                "http_requirements": 0,
                "operations": 0,
            }
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_publish_key_converges_on_one_version_and_receipt(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    try:
        async with seed.factory() as session, session.begin():
            workflow_id = await _create_definition(
                WorkflowRepository(session),
                project_id=project_id,
                actor_id=actor_id,
                name="Concurrent publish workflow",
                checksum="1" * 64,
            )

        command = WorkflowVersionPublish(
            expected_draft_revision=1,
            expected_draft_checksum="1" * 64,
            graph_schema_version=1,
            canvas_schema_version=1,
            compiler_contract_version=1,
            semantic_checksum="2" * 64,
            idempotency_hash="3" * 64,
            request_digest="4" * 64,
        )

        async def publish_once():
            async with seed.factory() as session, session.begin():
                return await WorkflowRepository(session).publish_version(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=command,
                )

        first, second = await asyncio.gather(publish_once(), publish_once())
        assert first.version_id == second.version_id
        assert {first.created, second.created} == {True, False}
        async with seed.factory() as session, session.begin():
            counts = (
                (
                    await session.execute(
                        sa.text(
                            """SELECT
                             (SELECT count(*) FROM workflow_versions
                               WHERE workflow_id=:workflow) AS versions,
                             (SELECT count(*) FROM workflow_control_operations
                               WHERE workflow_id=:workflow) AS operations"""
                        ),
                        {"workflow": workflow_id},
                    )
                )
                .mappings()
                .one()
            )
            assert counts == {"versions": 1, "operations": 1}
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_save_then_publish_uses_one_lock_order_and_publish_cas_fails(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    project_id = seed.owner_a.project_id
    actor_id = str(seed.owner_a.user_id)
    definition_locked = asyncio.Event()
    publish_attempted = asyncio.Event()
    try:
        async with seed.factory() as session, session.begin():
            workflow_id = await _create_definition(
                WorkflowRepository(session),
                project_id=project_id,
                actor_id=actor_id,
                name="Save publish lock order",
                checksum="7" * 64,
            )

        async def save_first():
            async with seed.factory() as session, session.begin():
                repository = WorkflowRepository(session)
                # Mirror the service's already-authorized transaction, then
                # freeze Definition before Draft.  Repository re-locks are
                # deliberate and prove the public mutation keeps this order.
                await repository._lock_authority(project_id, actor_id)
                assert (
                    await repository.get_definition(
                        project_id,
                        workflow_id,
                        lock=True,
                    )
                    is not None
                )
                definition_locked.set()
                await publish_attempted.wait()
                return await repository.save_draft(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=WorkflowDraftUpdate(
                        expected_revision=1,
                        spec_schema_version=1,
                        canvas_schema_version=1,
                        spec={"schema_version": 1, "nodes": [], "saved": True},
                        canvas={"schema_version": 1, "viewport": {}},
                        draft_checksum="8" * 64,
                    ),
                )

        async def publish_waiter():
            await definition_locked.wait()
            async with seed.factory() as session, session.begin():
                publish_attempted.set()
                return await WorkflowRepository(session).publish_version(
                    project_id=project_id,
                    actor_user_id=actor_id,
                    workflow_id=workflow_id,
                    command=WorkflowVersionPublish(
                        expected_draft_revision=1,
                        expected_draft_checksum="7" * 64,
                        graph_schema_version=1,
                        canvas_schema_version=1,
                        compiler_contract_version=1,
                        semantic_checksum="9" * 64,
                        idempotency_hash="a" * 64,
                        request_digest="b" * 64,
                    ),
                )

        saved, publish_result = await asyncio.wait_for(
            asyncio.gather(
                save_first(),
                publish_waiter(),
                return_exceptions=True,
            ),
            timeout=10,
        )
        assert not isinstance(saved, BaseException)
        assert saved.revision == 2
        assert isinstance(publish_result, WorkflowDraftCASConflict)

        async with seed.factory() as session, session.begin():
            state = (
                (
                    await session.execute(
                        sa.text(
                            """SELECT d.revision,
                                  w.current_published_version_id,
                                  (SELECT count(*) FROM workflow_versions
                                    WHERE workflow_id=w.id) AS versions,
                                  (SELECT count(*) FROM workflow_control_operations
                                    WHERE workflow_id=w.id) AS operations
                             FROM workflow_definitions w
                             JOIN workflow_drafts d ON d.workflow_id=w.id
                            WHERE w.id=:workflow AND w.project_id=:project"""
                        ),
                        {"workflow": workflow_id, "project": project_id},
                    )
                )
                .mappings()
                .one()
            )
            assert state == {
                "revision": 2,
                "current_published_version_id": None,
                "versions": 0,
                "operations": 0,
            }
    finally:
        await seed.engine.dispose()
