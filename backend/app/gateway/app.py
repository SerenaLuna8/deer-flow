import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.auth.proxy_identity import validate_proxy_identity_config
from app.gateway.auth_disabled import warn_if_auth_disabled_enabled
from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.config import get_gateway_config
from app.gateway.csrf_middleware import CSRFMiddleware, get_configured_cors_origins
from app.gateway.deps import gateway_platform_runtime
from app.gateway.routers import (
    admin_assets,
    admin_audit,
    admin_jobs,
    admin_operations,
    admin_projects,
    auth,
    github_webhooks,
    models,
    notifications,
    privacy_center,
    private_work,
    project_assets,
    project_audit,
    project_automations,
    project_connections,
    project_input_polish,
    project_invitations,
    project_lifecycle,
    project_members,
    project_memory,
    project_usage,
    projects,
)
from app.gateway.skill_version_body_limit import SkillVersionRequestBodyLimitMiddleware
from app.gateway.trace_middleware import TraceMiddleware, resolve_trace_enabled
from app.reliability.error_mapping import (
    ReliabilityHTTPException,
    reliability_http_exception_handler,
)
from deerflow.config import app_config as deerflow_app_config
from deerflow.logging_config import DEFAULT_LOG_DATE_FORMAT, DEFAULT_LOG_FORMAT, configure_logging
from deerflow.uploads.manager import cleanup_stale_upload_staging_files

AppConfig = deerflow_app_config.AppConfig
get_app_config = deerflow_app_config.get_app_config

# Default logging; lifespan overrides from config.yaml log_level.
logging.basicConfig(
    level=logging.INFO,
    format=DEFAULT_LOG_FORMAT,
    datefmt=DEFAULT_LOG_DATE_FORMAT,
)

logger = logging.getLogger(__name__)

# Upper bound (seconds) each lifespan shutdown hook is allowed to run.
# Bounds worker exit time so uvicorn's reload supervisor does not keep
# firing signals into a worker that is stuck waiting for shutdown cleanup.
_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def _asset_catalog_provider_lifespan(session_factory=None) -> AsyncGenerator[None, None]:
    """Install the app-owned provider and always clear the harness registry."""

    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.persistence.engine import get_session_factory

    if session_factory is None:

        def resolved_session_factory():
            current_factory = get_session_factory()
            if current_factory is None:
                raise RuntimeError("PostgreSQL session factory is unavailable for the asset catalog")
            return current_factory()

    else:
        resolved_session_factory = session_factory
    set_asset_catalog_provider(PostgresAssetCatalogProvider(resolved_session_factory))
    try:
        yield
    finally:
        set_asset_catalog_provider(None)


@asynccontextmanager
async def _gateway_runtime_lifespan(app: FastAPI, startup_config) -> AsyncGenerator[None, None]:
    """Keep the catalog provider alive with the Gateway platform services."""

    async with _asset_catalog_provider_lifespan():
        async with gateway_platform_runtime(app, startup_config):
            yield


async def _ensure_admin_user() -> None:
    """Log first-boot setup status without mutating legacy private data."""
    from app.gateway.deps import get_local_provider

    try:
        provider = get_local_provider()
    except RuntimeError:
        # Auth persistence may not be initialized in some test/boot paths.
        # Skip admin migration work rather than failing gateway startup.
        logger.warning("Auth persistence not ready; skipping admin bootstrap check")
        return

    admin_count = await provider.count_admin_users()

    if admin_count == 0:
        logger.info("=" * 60)
        logger.info("  First boot detected — no admin account exists.")
        logger.info("  Visit /setup to complete admin account creation.")
        logger.info("=" * 60)
        return


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""

    # Load config and check necessary environment variables at startup.
    # `startup_config` is a local snapshot used only for one-shot bootstrap
    # work (logging level, platform engines, channels). Request-time
    # config resolution always routes through `get_app_config()` in
    # `app/gateway/deps.py::get_config()` so `config.yaml` edits become
    # visible without a process restart. We deliberately do NOT cache this
    # snapshot on `app.state` to keep that contract enforceable.
    try:
        startup_config = get_app_config()
        validate_proxy_identity_config(startup_config.auth)
        configure_logging(startup_config)
        logger.info("Configuration loaded successfully")
        warn_if_auth_disabled_enabled()
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")

    # Pre-warm tiktoken encoding cache so the first memory-injection request
    # never blocks on the BPE data download (which hits an OpenAI/Azure URL
    # that may be unreachable in restricted networks — see issue #3402).
    # When memory.token_counting is "char", token counting never touches
    # tiktoken, so skip the warm-up entirely (avoids even the 5s probe in
    # network-restricted deployments — see issue #3429).
    if startup_config.memory.token_counting == "char":
        logger.info("memory.token_counting='char'; skipping tiktoken warm-up (network-free token estimation)")
    else:
        try:
            from deerflow.agents.memory.prompt import warm_tiktoken_cache

            warmed = await asyncio.wait_for(
                asyncio.to_thread(warm_tiktoken_cache),
                timeout=5,
            )
            if warmed:
                logger.info("tiktoken encoding cache warmed successfully")
            else:
                logger.warning("tiktoken encoding cache warm-up failed; token counting will use character-based fallback until tiktoken loads successfully")
        except TimeoutError:
            logger.warning("tiktoken encoding cache warm-up timed out; token counting will use character-based fallback until tiktoken loads successfully")
        except Exception:
            logger.warning("tiktoken warm-up skipped", exc_info=True)

    try:
        removed_upload_staging_files = await asyncio.to_thread(cleanup_stale_upload_staging_files)
        if removed_upload_staging_files:
            logger.info("Removed %d stale upload staging file(s)", removed_upload_staging_files)
    except Exception:
        logger.warning("Upload staging file cleanup skipped", exc_info=True)

    async with _gateway_runtime_lifespan(app, startup_config):
        logger.info("Gateway platform runtime initialised")

        await _ensure_admin_user()

        # Start IM channel service if any channels are configured
        try:
            from app.channels.service import start_channel_service

            channel_service = await start_channel_service(startup_config, app=app)
            logger.info("Channel service started: %s", channel_service.get_status())
        except Exception:
            logger.exception("No IM channels configured or channel service failed to start")

        try:
            yield
        finally:
            try:
                await auth.close_oidc_service()
            except Exception:
                logger.exception("Failed to close OIDC service")

            # Stop channel service on shutdown (bounded to prevent worker hang)
            try:
                from app.channels.service import stop_channel_service

                await asyncio.wait_for(
                    stop_channel_service(),
                    timeout=_SHUTDOWN_HOOK_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Channel service shutdown exceeded %.1fs; proceeding with worker exit.",
                    _SHUTDOWN_HOOK_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.exception("Failed to stop channel service")

    logger.info("Shutting down API Gateway")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    config = get_gateway_config()
    docs_url = "/docs" if config.enable_docs else None
    redoc_url = "/redoc" if config.enable_docs else None
    openapi_url = "/openapi.json" if config.enable_docs else None

    app = FastAPI(
        title="DeerFlow API Gateway",
        description="""
## DeerFlow API Gateway

API Gateway for DeerFlow - A LangGraph-based AI agent backend with sandbox execution capabilities.

### Features

- **Models Management**: Query and retrieve available AI models
- **Project Private Work**: Access project-scoped conversations and files
- **Health Monitoring**: System health check endpoints

### Architecture

This gateway provides project-scoped runtime endpoints and administrative operations.
        """,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        openapi_tags=[
            {
                "name": "models",
                "description": "Operations for querying available AI models and their configurations",
            },
            {
                "name": "project-input-polish",
                "description": "Polish a composer draft under current project authority",
            },
            {
                "name": "health",
                "description": "Health check and system status endpoints",
            },
        ],
    )

    # M6 public reliability errors intentionally serialize at the response
    # root, not through FastAPI's default {"detail": ...} wrapper.
    app.add_exception_handler(
        ReliabilityHTTPException,
        reliability_http_exception_handler,
    )

    # Bound JSON/base64 and multipart Skill archives at the ASGI receive
    # boundary so oversized bodies never reach route-level parsing.
    app.add_middleware(SkillVersionRequestBodyLimitMiddleware)

    # Auth: reject unauthenticated requests to non-public paths (fail-closed safety net)
    app.add_middleware(AuthMiddleware)

    # CSRF: Double Submit Cookie pattern for state-changing requests
    app.add_middleware(CSRFMiddleware)

    # CORS: the unified nginx endpoint is same-origin by default. Split-origin
    # browser clients must opt in with this explicit Gateway allowlist so CORS
    # and CSRF origin checks share the same source of truth.
    cors_origins = sorted(get_configured_cors_origins())
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                "Content-Location",
                "Location",
                "Retry-After",
                "X-Trace-Id",
            ],
        )

    # Request trace correlation: when logging.enhance.enabled=true, bind one
    # trace id per Gateway HTTP request and write it to response start headers.
    # `logging` is registered as restart-required (see reload_boundary.py) so we
    # snapshot the flag from the startup AppConfig instead of reading live; a
    # runtime toggle would otherwise leave the log formatter (installed once by
    # configure_logging() at lifespan startup) out of sync with the middleware.
    app.add_middleware(TraceMiddleware, enabled=_resolve_trace_enabled_for_app_construction())

    # Include routers
    # Models API is mounted at /api/models
    app.include_router(models.router)

    # Project-scoped SaaS API is mounted at /api/projects.
    app.include_router(projects.router)
    app.include_router(project_members.router)
    app.include_router(project_invitations.router)
    app.include_router(notifications.router)
    app.include_router(project_lifecycle.router)
    app.include_router(project_usage.router)
    app.include_router(project_audit.router)
    app.include_router(project_assets.catalog_router)
    app.include_router(project_assets.project_router)
    # Readiness must precede the dynamic /{task_id} project Automation route.
    app.include_router(project_automations.readiness_router)
    app.include_router(project_automations.router)
    app.include_router(private_work.router)
    app.include_router(privacy_center.router)
    app.include_router(project_memory.router)
    app.include_router(project_connections.router)
    app.include_router(project_input_polish.router)
    app.include_router(admin_assets.admin_router)
    app.include_router(admin_assets.admin_project_router)
    app.include_router(admin_operations.router)
    app.include_router(admin_projects.router)
    app.include_router(admin_jobs.router)
    app.include_router(admin_audit.router)

    # Auth API is mounted at /api/v1/auth
    app.include_router(auth.router)

    # GitHub webhooks API is mounted at /api/webhooks/github
    # Exempt from auth and CSRF middleware (see auth_middleware._PUBLIC_PATH_PREFIXES
    # and csrf_middleware.should_check_csrf); authenticity is enforced via the
    # X-Hub-Signature-256 HMAC against GITHUB_WEBHOOK_SECRET.
    # Including this router transitively imports app.gateway.github, which
    # registers the GitHub channel's ChannelRunPolicy as an import side-effect.
    #
    # Fail-closed: only mount the route when a webhook secret is configured
    # (or when the explicit DEER_FLOW_ALLOW_UNVERIFIED_GITHUB_WEBHOOKS=1
    # dev opt-in is set). A misconfigured deployment without a secret cannot
    # serve forged deliveries because the URL responds 404 — there is no
    # handler to reach.
    if github_webhooks.is_route_enabled():
        app.include_router(github_webhooks.router)
        logger.info("GitHub webhooks route mounted at /api/webhooks/github")
    else:
        logger.warning("GitHub webhooks route NOT mounted: GITHUB_WEBHOOK_SECRET unset and DEER_FLOW_ALLOW_UNVERIFIED_GITHUB_WEBHOOKS not set. /api/webhooks/github will respond 404. Configure either env var to enable the route.")

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """Health check endpoint.

        Returns:
            Service health status information.
        """
        return {"status": "healthy", "service": "deer-flow-gateway"}

    return app


def _resolve_trace_enabled_for_app_construction() -> bool:
    """Resolve the trace middleware flag without making imports require config.yaml."""
    try:
        return resolve_trace_enabled(get_app_config())
    except FileNotFoundError:
        # Startup lifespan still performs strict config loading before serving.
        logger.debug("config.yaml not found while constructing Gateway app; TraceMiddleware disabled for this app instance")
        return False


# Create app instance for uvicorn
app = create_app()
