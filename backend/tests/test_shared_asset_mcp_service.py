from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.exc import TimeoutError as SATimeoutError

from app.audit.models import AuditAction
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetValidationFailed
from deerflow.mcp.definition import ExactMcpEndpointPolicy


def _context(role: ProjectRole = ProjectRole.EDITOR) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-mcp-unit",
    )


def _safe_definition(service_module):
    return service_module.McpDefinition(
        description="Issue tracker",
        transport="http",
        url="https://mcp.example.test",
        oauth={
            "enabled": True,
            "token_url": "https://identity.example.test/oauth/token",
            "client_id": "public-client",
        },
    )


def _endpoint_policy(*endpoints: str) -> ExactMcpEndpointPolicy:
    return ExactMcpEndpointPolicy(frozenset(endpoints))


def _system_context() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="req-system-bootstrap-only",
    )


def test_mcp_service_exposes_frozen_contracts_and_scoped_repository() -> None:
    package = importlib.import_module("app.shared_assets")
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    repository_module = importlib.import_module("app.shared_assets.mcp_repository")
    audit_module = importlib.import_module("app.shared_assets.audit")

    assert package.McpService is service_module.McpService
    for value_type in (
        service_module.CreateMcpServer,
        service_module.McpCredentialSlot,
        service_module.McpDefinition,
        service_module.McpAssetView,
        service_module.McpVersionView,
    ):
        assert dataclasses.is_dataclass(value_type)
        assert value_type.__dataclass_params__.frozen is True
    assert "payload_checksum" in {field.name for field in dataclasses.fields(service_module.McpVersionView)}

    for name, method in inspect.getmembers(repository_module.McpRepository, predicate=inspect.isfunction):
        if not name.startswith("_"):
            assert "project_id" not in inspect.signature(method).parameters, name
    project_get = inspect.signature(repository_module.McpRepository.get_project_asset)
    assert list(project_get.parameters) == ["self", "context", "asset_id", "for_update"]
    approve = inspect.signature(service_module.McpService.approve)
    assert list(approve.parameters)[4] == "credential_versions"
    configure_grants = inspect.signature(service_module.McpService.configure_system_credential_grants)
    assert list(configure_grants.parameters) == [
        "self",
        "actor",
        "asset_id",
        "version_id",
        "credential_versions",
        "expected_active_grant_versions",
    ]
    assert audit_module._ACTIONS["mcp.credential_grants.configure"] is AuditAction.ASSET_UPDATED


@pytest.mark.asyncio
async def test_runtime_system_mcp_authoring_and_generic_approval_stop_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("bootstrap-only rejection must not open storage")

    service = service_module.McpService(ExplodingFactory())
    actor = _system_context()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()

    with pytest.raises(AssetForbidden):
        await service.create_asset(
            actor,
            service_module.CreateMcpServer("runtime-system", "Runtime System"),
        )
    with pytest.raises(AssetForbidden):
        await service.create_version(
            actor,
            asset_id,
            _safe_definition(service_module),
            expected_asset_version=1,
        )
    with pytest.raises(AssetForbidden):
        await service.approve(
            actor,
            asset_id,
            version_id,
            {"primary": uuid.uuid4()},
            expected_asset_version=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        {"transport": "stdio", "command": "uvx", "env": {"API_TOKEN": "never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "headers": {"Authorization": "Bearer never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "oauth": {"client_secret": "never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "oauth": {"refreshToken": "never-log-me"}},
    ],
)
async def test_mcp_definition_rejects_secret_fields_before_storage(
    definition: dict[str, object],
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    service = service_module.McpService(ExplodingFactory())
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )
    assert exc_info.value.request_id == "req-mcp-unit"
    assert "never-log-me" not in str(exc_info.value)
    assert "never-log-me" not in repr(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "secret_value"),
    [
        ({"transport": "stdio", "command": "uvx", "env": {"CLIENTSECRET": "client-value"}}, "client-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"PRIVATEKEY": "private-value"}}, "private-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"APIKEY": "api-value"}}, "api-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"ACCESSTOKEN": "access-value"}}, "access-value"),
        ({"transport": "stdio", "command": "uvx", "env": {"PUBLIC_SETTING": "Bearer bearer-value"}}, "bearer-value"),
        ({"transport": "http", "url": "https://mcp.test", "headers": {"X-Mode": "Basic basic-value"}}, "basic-value"),
        ({"transport": "http", "url": "https://mcp.test", "routing": {"auth": "client_secret=client-value"}}, "client-value"),
        ({"transport": "http", "url": "https://mcp.test", "routing": {"auth": ["password=password-value"]}}, "password-value"),
        ({"transport": "http", "url": "https://mcp.test", "tool_overrides": {"auth": {"value": "token=token-value"}}}, "token-value"),
        (
            {
                "transport": "http",
                "url": "https://mcp.test",
                "tool_overrides": {"auth": "-----BEGIN PRIVATE KEY-----\nprivate-value"},
            },
            "private-value",
        ),
    ],
)
async def test_mcp_definition_rejects_compact_keys_and_recursive_secret_values_before_storage(
    definition: dict[str, object],
    secret_value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )
    assert secret_value not in str(exc_info.value)
    assert secret_value not in repr(exc_info.value)
    assert secret_value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "secret_value"),
    [
        pytest.param(
            {
                "description": "connect with password=description-marker",
                "transport": "http",
                "url": "https://mcp.test",
            },
            "description-marker",
            id="description-assignment",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp --client-secret=command-marker",
            },
            "command-marker",
            id="command-option",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp",
                "args": ("--verbose", "--api-key=args-marker"),
            },
            "args-marker",
            id="args-option",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test/tools?api%5Fkey=query-marker",
            },
            "query-marker",
            id="url-sensitive-query",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://public:userinfo-marker@mcp.test/tools",
            },
            "userinfo-marker",
            id="url-userinfo",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp",
                "env": {"CLIENTSECRET": "env-key-marker"},
            },
            "env-key-marker",
            id="env-key",
        ),
        pytest.param(
            {
                "transport": "stdio",
                "command": "mcp",
                "env": {"PUBLIC_SETTING": "Bearer env-value-marker"},
            },
            "env-value-marker",
            id="env-value",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "headers": {"X-Auth": "header-key-marker"},
            },
            "header-key-marker",
            id="header-auth-carrier",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "oauth": {
                    "enabled": True,
                    "extra_token_params": {"resource": "client_secret=oauth-marker"},
                },
            },
            "oauth-marker",
            id="oauth-recursive-value",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "routing": {
                    "fallback": "https://route.test/api?access%5Ftoken=routing-marker",
                },
            },
            "routing-marker",
            id="routing-url-query",
        ),
        pytest.param(
            {
                "transport": "http",
                "url": "https://mcp.test",
                "tool_overrides": {
                    "search": {"argument": "--private-key=override-marker"},
                },
            },
            "override-marker",
            id="tool-override-option",
        ),
    ],
)
async def test_mcp_definition_field_complete_secret_scan_runs_before_storage(
    definition: dict[str, object],
    secret_value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )
    assert secret_value not in str(exc_info.value)
    assert secret_value not in repr(exc_info.value)
    assert secret_value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--api-key", "args-api-key-marker"), id="api-key"),
        pytest.param(("--token", "args-token-marker"), id="token"),
        pytest.param(("--access-token", "args-access-token-marker"), id="access-token"),
        pytest.param(("--refresh-token", "args-refresh-token-marker"), id="refresh-token"),
        pytest.param(("--client-secret", "args-client-secret-marker"), id="client-secret"),
        pytest.param(("--password", "args-password-marker"), id="password"),
        pytest.param(("--private-key", "args-private-key-marker"), id="private-key"),
    ],
)
async def test_mcp_definition_rejects_separated_secret_carrier_args_before_storage(
    args: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    marker = args[1]
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(transport="stdio", command="mcp", args=args),
            expected_asset_version=1,
        )
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_mcp_definition_rejects_separated_secret_carrier_command_before_storage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    command = "mcp --client-secret command-secret-marker"
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(transport="stdio", command=command),
            expected_asset_version=1,
        )
    assert command not in str(exc_info.value)
    assert command not in repr(exc_info.value)
    assert command not in caplog.text
    assert "command-secret-marker" not in caplog.text


@pytest.mark.asyncio
async def test_mcp_definition_rejects_secret_carrier_option_without_inspecting_next_arg() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    observed: list[str] = []

    class ObservedSecret(str):
        def __len__(self) -> int:
            observed.append("length")
            return super().__len__()

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(
                transport="stdio",
                command="mcp",
                args=("--api-key", ObservedSecret("uninspected-secret-marker")),
            ),
            expected_asset_version=1,
        )
    assert observed == []


def test_mcp_sensitive_cli_option_delegates_to_shared_key_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    inspected: list[str] = []

    def fake_sensitive_key(value: str) -> bool:
        inspected.append(value)
        return value == "future_carrier"

    monkeypatch.setattr(
        service_module.McpService,
        "_sensitive_key",
        staticmethod(fake_sensitive_key),
    )

    assert service_module.McpService._is_sensitive_cli_option("--future-carrier=marker") is True
    assert inspected == ["future_carrier"]


@pytest.mark.parametrize(
    "carrier",
    [
        "APIKEY",
        "api_key",
        "api-key",
        "ApiKey",
        "CLIENTSECRET",
        "client_secret",
        "client-secret",
        "ClientSecret",
        "ACCESSTOKEN",
        "access_token",
        "access-token",
        "AccessToken",
        "PRIVATEKEY",
        "private_key",
        "private-key",
        "PrivateKey",
        "secret",
        "passwd",
        "access-key",
        "auth",
        "authorization",
        "cookie",
        "credential",
    ],
)
def test_mcp_sensitive_cli_option_normalizes_shared_taxonomy_variants(carrier: str) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    assert service_module.McpService._sensitive_key(carrier) is True
    for token in (
        carrier,
        f"-{carrier}",
        f"--{carrier}",
        f"--{carrier}=taxonomy-assignment-marker",
        f"--{carrier.replace('-', '_').swapcase()}",
    ):
        assert service_module.McpService._is_sensitive_cli_option(token) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "carrier",
    [
        "APIKEY",
        "CLIENTSECRET",
        "ACCESSTOKEN",
        "PRIVATEKEY",
        "secret",
        "passwd",
        "access-key",
        "auth",
        "authorization",
        "cookie",
        "credential",
    ],
)
async def test_mcp_definition_rejects_compact_or_undashed_secret_carrier_before_storage(
    carrier: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    marker = f"{carrier.lower()}-undashed-marker"
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(
                transport="stdio",
                command="mcp",
                args=(carrier, marker),
            ),
            expected_asset_version=1,
        )
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)
    assert marker not in caplog.text


@pytest.mark.parametrize(
    "definition",
    [
        pytest.param(
            {
                "description": "Ordinary passwordless issue tracker",
                "transport": "http",
                "url": "https://mcp.test/tools?mode=read",
            },
            id="ordinary-description-url",
        ),
        pytest.param(
            {
                "description": "Remote tools",
                "transport": "http",
                "url": "https://mcp.test/tools",
                "routing": {
                    "strategy": "round_robin",
                    "fallback": "https://route.test/api?mode=read",
                },
                "tool_overrides": {
                    "search": {"enabled": True, "description": "ordinary public search"},
                },
            },
            id="ordinary-routing-overrides",
        ),
    ],
)
def test_mcp_definition_field_complete_scan_allows_nonsecret_metadata(
    definition: dict[str, object],
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    normalized = service_module.McpService._validate_definition(
        _context(),
        service_module.McpDefinition(**definition),
        endpoint_policy=_endpoint_policy(str(definition["url"])),
    )

    assert normalized.description == definition["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"command": ["TOKEN"]},
        {"env": "TOKEN"},
        {"env": ["TOKEN", "TOKEN"]},
        {"oauth": [""]},
    ],
)
async def test_mcp_slot_schema_is_validated_before_storage(schema: dict[str, object]) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("invalid slot schema must not open storage")

    definition = dataclasses.replace(
        _safe_definition(service_module),
        credential_slots=(service_module.McpCredentialSlot("primary", "Auth", schema),),
    )
    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            definition,
            expected_asset_version=1,
        )


@pytest.mark.asyncio
async def test_editor_cannot_approve_credential_mcp_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must happen before storage")

    service = service_module.McpService(ExplodingFactory())
    with pytest.raises(AssetForbidden):
        await service.approve(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            uuid.uuid4(),
            {"primary": uuid.uuid4()},
            expected_asset_version=3,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_versions",
    [
        {"": uuid.uuid4()},
        {"primary": "not-a-uuid"},
        [("primary", uuid.uuid4())],
    ],
)
async def test_mcp_approval_rejects_invalid_slot_mapping_before_storage(
    credential_versions: object,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("invalid slot mapping must not open storage")

    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).approve(
            _context(ProjectRole.ADMIN),
            uuid.uuid4(),
            uuid.uuid4(),
            credential_versions,
            expected_asset_version=3,
        )


def test_mcp_approval_copies_slot_mapping_before_async_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    original_id = uuid.uuid4()
    caller_mapping = {"primary": original_id}

    normalized = service_module.McpService._validate_credential_bindings(
        _context(ProjectRole.ADMIN),
        caller_mapping,
    )
    caller_mapping["primary"] = uuid.uuid4()

    assert normalized == {"primary": original_id}
    with pytest.raises(TypeError):
        normalized["primary"] = uuid.uuid4()


@pytest.mark.parametrize(
    "field_update",
    [
        {"routing": {"nested": {"apiKey": "never-log-me"}}},
        {"tool_overrides": {"search": {"private_key": "never-log-me"}}},
    ],
)
def test_mcp_definition_rejects_nested_secret_config(field_update: dict[str, object]) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(_safe_definition(service_module), **field_update)

    with pytest.raises(AssetValidationFailed) as exc_info:
        service_module.McpService._validate_definition(
            _context(),
            definition,
            endpoint_policy=_endpoint_policy(str(definition.url)),
        )
    assert "never-log-me" not in str(exc_info.value)


def test_mcp_definition_rejects_project_oauth_until_private_runtime_supports_it() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(
        _safe_definition(service_module),
        oauth={
            "enabled": True,
            "token_url": "https://identity.example.test/oauth/token",
            "grant_type": "client_credentials",
            "client_id": "public-client",
            "token_field": "access_token",
            "token_type_field": "token_type",
            "expires_in_field": "expires_in",
        },
    )

    with pytest.raises(AssetValidationFailed):
        service_module.McpService._validate_definition(
            _context(),
            definition,
            endpoint_policy=_endpoint_policy(str(definition.url)),
        )


def test_mcp_definition_keeps_packaged_system_oauth_read_compatible() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(
        _safe_definition(service_module),
        oauth={
            "enabled": True,
            "token_url": "https://identity.example.test/oauth/token",
            "grant_type": "client_credentials",
            "client_id": "public-client",
            "token_field": "access_token",
        },
    )

    normalized = service_module.McpService._validate_definition(
        _system_context(),
        definition,
    )

    assert normalized.oauth["token_url"] == "https://identity.example.test/oauth/token"


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["submit_approval", "approve", "publish"])
async def test_mcp_transition_revalidates_historical_project_definition(
    transition: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    actor = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    slot = service_module.McpCredentialSlot(
        "primary",
        "Authorization header",
        {"headers": ("Authorization",)},
    )
    slots = (
        SimpleNamespace(
            id=uuid.uuid4(),
            name=slot.name,
            purpose=slot.purpose,
            payload_schema={"headers": ["Authorization"]},
            required=True,
        ),
    )
    definition = service_module.McpDefinition(
        transport="http",
        url="https://historical.example.test/mcp",
        credential_slots=(slot,) if transition != "publish" else (),
    )
    row = SimpleNamespace(
        id=version_id,
        mcp_server_id=asset_id,
        workflow_status=("pending_approval" if transition == "approve" else "draft"),
        description=definition.description,
        transport=definition.transport,
        command=definition.command,
        args=list(definition.args),
        url=definition.url,
        non_secret_env=dict(definition.env),
        non_secret_headers=dict(definition.headers),
        oauth_metadata=dict(definition.oauth),
        routing=dict(definition.routing),
        tool_overrides=dict(definition.tool_overrides),
        timeout_seconds=definition.timeout_seconds,
        payload_checksum=service_module.McpService._checksum(definition),
    )
    record = SimpleNamespace(
        row=row,
        slots=slots if transition != "publish" else (),
        grants=(),
    )
    asset = SimpleNamespace(status="active", version=1)

    class Session:
        async def flush(self) -> None:
            return None

    class Repository:
        session = Session()

        async def get_project_asset(
            self,
            _actor,
            _asset_id,
            *,
            for_update: bool,
        ):
            assert for_update is True
            return asset

        async def lock_project(self, _actor) -> None:
            return None

        async def _get_project_asset_after_lock(self, _actor, _asset_id):
            return asset

        async def get_project_version(
            self,
            _actor,
            _asset_id,
            _version_id,
            *,
            for_update: bool,
        ):
            assert for_update is True
            return record

    service = service_module.McpService(
        lambda: None,
        endpoint_policy=_endpoint_policy("https://allowed.example.test/mcp"),
    )

    async def execute(_actor, operation, governance=None):
        del governance
        return await operation(Repository())

    service._execute = execute
    service._version_view = lambda value: value

    with pytest.raises(AssetValidationFailed):
        if transition == "submit_approval":
            await service.submit_approval(
                actor,
                asset_id,
                version_id,
                expected_asset_version=1,
            )
        elif transition == "approve":
            await service.approve(
                actor,
                asset_id,
                version_id,
                {"primary": uuid.uuid4()},
                expected_asset_version=1,
            )
        else:
            await service.publish(
                actor,
                asset_id,
                version_id,
                expected_asset_version=1,
            )


@pytest.mark.asyncio
async def test_mcp_pool_timeout_is_mapped_to_safe_storage_unavailable() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class TimeoutFactory:
        def __call__(self):
            raise SATimeoutError("postgresql://admin:never-log-me@db.example.test/app")

    with pytest.raises(service_module.AssetStorageUnavailable) as exc_info:
        await service_module.McpService(TimeoutFactory()).get(_context(), uuid.uuid4())
    assert exc_info.value.__cause__ is None
    assert "never-log-me" not in str(exc_info.value)
    assert "never-log-me" not in repr(exc_info.value)
    assert "postgresql" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_programming_session_error_is_not_mapped_to_503() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    programming_error = InvalidRequestError("programming failure")

    class InvalidFactory:
        def __call__(self):
            raise programming_error

    with pytest.raises(InvalidRequestError) as exc_info:
        await service_module.McpService(InvalidFactory()).get(_context(), uuid.uuid4())
    assert exc_info.value is programming_error
