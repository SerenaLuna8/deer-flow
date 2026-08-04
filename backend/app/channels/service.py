"""ChannelService — manages the lifecycle of all IM channels."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from app.channels.base import Channel
from app.channels.instance_identity import normalize_channel_instance_id
from app.channels.manager import DEFAULT_GATEWAY_URL, DEFAULT_LANGGRAPH_URL, ChannelManager
from app.channels.message_bus import MessageBus
from app.channels.store import ChannelStore

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig
    from deerflow.config.channel_connections_config import ChannelConnectionsConfig

# Channel name → import path for lazy loading
_CHANNEL_REGISTRY: dict[str, str] = {
    "dingtalk": "app.channels.dingtalk:DingTalkChannel",
    "discord": "app.channels.discord:DiscordChannel",
    "feishu": "app.channels.feishu:FeishuChannel",
    "github": "app.channels.github:GitHubChannel",
    "slack": "app.channels.slack:SlackChannel",
    "telegram": "app.channels.telegram:TelegramChannel",
    "wechat": "app.channels.wechat:WechatChannel",
    "wecom": "app.channels.wecom:WeComChannel",
}

# Keys that indicate a user has configured credentials for a channel.
_CHANNEL_CREDENTIAL_KEYS: dict[str, list[str]] = {
    "dingtalk": ["client_id", "client_secret"],
    "discord": ["bot_token"],
    "feishu": ["app_id", "app_secret"],
    "slack": ["bot_token", "app_token"],
    "telegram": ["bot_token"],
    "wecom": ["bot_id", "bot_secret"],
    "wechat": ["bot_token"],
}

_CHANNELS_LANGGRAPH_URL_ENV = "DEER_FLOW_CHANNELS_LANGGRAPH_URL"
_CHANNELS_GATEWAY_URL_ENV = "DEER_FLOW_CHANNELS_GATEWAY_URL"


def _make_group_identity_candidates(identity_hasher: Any | None = None):
    """Build the app-owned HMAC lookup used for non-login group guests."""

    if identity_hasher is None:
        from app.channel_group_bindings.identity import (
            AuditChannelGroupIdentityHasher,
        )

        identity_hasher = AuditChannelGroupIdentityHasher()

    def candidates(
        provider: str,
        channel_instance_id,
        external_account_id: str,
        workspace_id: str,
    ) -> tuple[tuple[str, str], ...]:
        if not external_account_id or not workspace_id:
            return ()
        account_refs = identity_hasher.account_refs(
            provider,
            channel_instance_id,
            external_account_id,
        )
        group_refs = identity_hasher.group_refs(
            provider,
            channel_instance_id,
            workspace_id,
        )
        if not account_refs or not group_refs:
            raise RuntimeError("channel identity keyring is inconsistent")
        # A group can have been bound under a retained key while a member is
        # first seen after rotation under the active key. Preserve each
        # account/group pair as one coordinate, but include every retained-key
        # combination so that mixed-generation rows remain readable.
        return tuple((account_ref, group_ref) for account_ref in dict.fromkeys(account_refs) for group_ref in dict.fromkeys(group_refs))

    return candidates


def _channel_has_credentials(name: str, channel_config: dict[str, Any]) -> bool:
    cred_keys = _CHANNEL_CREDENTIAL_KEYS.get(name, [])
    return any(not isinstance(channel_config.get(key), bool) and channel_config.get(key) is not None and str(channel_config[key]).strip() for key in cred_keys)


def _resolve_service_url(config: dict[str, Any], config_key: str, env_key: str, default: str) -> str:
    value = config.pop(config_key, None)
    if isinstance(value, str) and value.strip():
        return value
    env_value = os.getenv(env_key, "").strip()
    if env_value:
        return env_value
    return default


def _make_connection_repo(connection_config: ChannelConnectionsConfig | None):
    """Build the inbound authority repository independently of legacy flags.

    ``channel_connections.*`` only exposes deployment-config compatibility
    providers. It cannot bypass PostgreSQL inbound authority or gate an exact
    database-backed project instance.
    """
    try:
        from deerflow.persistence.channel_connections import ChannelConnectionRepository
        from deerflow.persistence.engine import get_session_factory
    except Exception:
        logger.exception("Failed to import channel connection repository")
        return None

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        logger.warning("Channel inbound authority persistence is not initialized")
        return None
    if session_factory is None:
        logger.warning("Channel inbound authority persistence is not available")
        return None
    return ChannelConnectionRepository(
        session_factory,
        external_identity_candidates=_make_group_identity_candidates(),
    )


def _make_project_connection_service(connection_repo: Any | None):
    if connection_repo is None:
        return None
    session_factory = getattr(connection_repo, "session_factory", None)
    if session_factory is None:
        logger.warning("Channel connection repository has no session factory; project callback service is unavailable")
        return None
    from app.private_work.connection_service import ProjectConnectionService

    return ProjectConnectionService(
        session_factory,
        repository=connection_repo,
    )


def _make_project_inbound_dispatcher(
    connection_repo: Any | None,
    gateway_app: Any | None,
    instance_authority_guard: Any | None = None,
):
    if connection_repo is None or gateway_app is None:
        return None
    session_factory = getattr(connection_repo, "session_factory", None)
    scoped_checkpointer = getattr(
        getattr(gateway_app, "state", None),
        "project_scoped_checkpointer",
        None,
    )
    if session_factory is None or scoped_checkpointer is None:
        logger.warning("Project inbound channel runtime dependencies are unavailable")
        return None

    from app.private_work.connection_inbound import (
        ConnectionInboundResolver,
        ProjectInboundDispatcher,
        build_gateway_project_run_launcher,
    )
    from app.private_work.thread_service import PrivateThreadService

    thread_service = PrivateThreadService(session_factory, scoped_checkpointer)
    resolver = ConnectionInboundResolver(
        repository=connection_repo,
        session_factory=session_factory,
        thread_service=thread_service,
    )
    return ProjectInboundDispatcher(
        resolver,
        build_gateway_project_run_launcher(app=gateway_app),
        instance_authority_guard=instance_authority_guard,
    )


class ChannelService:
    """Manages the lifecycle of all configured IM channels.

    Reads configuration from ``config.yaml`` under the ``channels`` key,
    instantiates enabled channels, and starts the ChannelManager dispatcher.
    """

    def __init__(
        self,
        channels_config: dict[str, Any] | None = None,
        *,
        connection_repo: Any | None = None,
        connection_service: Any | None = None,
        channel_group_binding_service: Any | None = None,
        gateway_app: Any | None = None,
    ) -> None:
        from app.channels.instance_authority import ChannelInstanceAuthorityGuard

        self._instance_authority_guard = ChannelInstanceAuthorityGuard()
        self.bus = MessageBus(
            instance_authority_guard=self._instance_authority_guard,
        )
        self.store = ChannelStore(connection_repo) if connection_repo is not None else None
        self._connection_repo = connection_repo
        self._connection_service = connection_service or _make_project_connection_service(connection_repo)
        self._channel_group_binding_service = channel_group_binding_service
        private_inbound_dispatcher = _make_project_inbound_dispatcher(
            connection_repo,
            gateway_app,
            self._instance_authority_guard,
        )
        config = dict(channels_config or {})
        langgraph_url = _resolve_service_url(config, "langgraph_url", _CHANNELS_LANGGRAPH_URL_ENV, DEFAULT_LANGGRAPH_URL)
        gateway_url = _resolve_service_url(config, "gateway_url", _CHANNELS_GATEWAY_URL_ENV, DEFAULT_GATEWAY_URL)
        default_session = config.pop("session", None)
        channel_sessions = {name: channel_config.get("session") for name, channel_config in config.items() if isinstance(channel_config, dict)}
        self.manager = ChannelManager(
            bus=self.bus,
            store=self.store,
            langgraph_url=langgraph_url,
            gateway_url=gateway_url,
            default_session=default_session if isinstance(default_session, dict) else None,
            channel_sessions=channel_sessions,
            connection_repo=connection_repo,
            private_inbound_dispatcher=private_inbound_dispatcher,
        )
        self._channels: dict[str, Any] = {}  # name -> Channel instance
        self._config = config
        self._instance_configs: dict[str, tuple[str, dict[str, Any]]] = {name: (name, dict(channel_config)) for name, channel_config in config.items() if name in _CHANNEL_REGISTRY and isinstance(channel_config, dict)}
        self._running = False
        self._readiness_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_app_config(
        cls,
        app_config: AppConfig | None = None,
        *,
        gateway_app: Any | None = None,
    ) -> ChannelService:
        """Create a ChannelService from the application config."""
        if app_config is None:
            from deerflow.config.app_config import get_app_config

            app_config = get_app_config()
        channels_config = {}
        # extra fields are allowed by AppConfig (extra="allow")
        extra = app_config.model_extra or {}
        if "channels" in extra:
            channels_config = dict(extra["channels"] or {})
        connection_config = getattr(app_config, "channel_connections", None)
        connection_repo = _make_connection_repo(connection_config)
        channel_group_binding_service = getattr(
            getattr(gateway_app, "state", None),
            "project_channel_group_binding_service",
            None,
        )
        return cls(
            channels_config=channels_config,
            connection_repo=connection_repo,
            channel_group_binding_service=channel_group_binding_service,
            gateway_app=gateway_app,
        )

    async def start(self) -> None:
        """Start the manager and all enabled channels."""
        if self._running:
            return

        await self.manager.start()
        self._running = True

        ready_status = await self.ensure_ready_channels(attempts=2)
        ready_count = sum(1 for ready in ready_status.values() if ready)
        logger.info("ChannelService started with %d/%d ready channels", ready_count, len(ready_status))

    async def ensure_ready_channels(self, *, attempts: int = 1) -> dict[str, bool]:
        """Start or restart enabled configured channels that are not ready."""
        ready_status: dict[str, bool] = {}
        for name, channel_config in self._config.items():
            if not isinstance(channel_config, dict):
                continue
            if not channel_config.get("enabled", False):
                if _channel_has_credentials(name, channel_config):
                    logger.warning(
                        "A configured channel has credentials configured but is disabled. Set enabled: true under its channels entry in config.yaml to activate it.",
                    )
                else:
                    logger.info("A configured channel is disabled, skipping")
                continue

            ready_status[name] = await self.ensure_channel_ready(name, attempts=attempts)
        return ready_status

    async def ensure_channel_ready(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        *,
        attempts: int = 1,
    ) -> bool:
        """Ensure a single enabled channel is running using its current config."""
        if not self._running:
            logger.warning("ChannelService is not running; cannot ensure channel readiness")
            return False

        if config is not None:
            self._config[name] = dict(config)

        # Serialize per channel: readiness is polled from request handlers, so
        # concurrent calls must not stop/start the same channel worker twice.
        lock = self._readiness_locks.setdefault(name, asyncio.Lock())
        async with lock:
            channel_config = self._config.get(name)
            if not channel_config or not isinstance(channel_config, dict):
                logger.warning("No config for requested channel")
                return False
            if not channel_config.get("enabled", False):
                return False

            channel = self._channels.get(name)
            if channel is not None and channel.is_running:
                return True

            if channel is not None:
                try:
                    await channel.stop()
                except Exception:
                    logger.exception("Error stopping non-running channel before readiness retry")
                    return False
                if self._channels.get(name) is channel:
                    self._channels.pop(name, None)

            max_attempts = max(1, attempts)
            for attempt in range(max_attempts):
                if attempt > 0:
                    logger.info("Retrying channel startup after readiness check")
                if await self._start_channel(name, channel_config):
                    return True
            return False

    async def ensure_channel_instance_ready(
        self,
        channel_instance_id: str,
        provider: str,
        config: dict[str, Any] | None = None,
        *,
        attempts: int = 1,
    ) -> bool:
        """Ensure one exact provider instance is running.

        Unlike :meth:`ensure_channel_ready`, this method never uses the
        provider name as the runtime lookup key, so multiple project instances
        of the same provider can coexist safely.
        """

        instance_id = normalize_channel_instance_id(
            provider,
            channel_instance_id,
        )
        if not self._running:
            logger.warning("ChannelService is not running; cannot ensure channel readiness")
            return False
        if provider not in _CHANNEL_REGISTRY:
            logger.warning("Unknown channel type")
            return False
        if config is not None:
            self._instance_configs[instance_id] = (provider, dict(config))

        lock = self._readiness_locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            entry = self._instance_configs.get(instance_id)
            if entry is None or entry[0] != provider:
                logger.warning("No config for requested channel instance")
                return False
            channel_config = entry[1]
            if not channel_config.get("enabled", False):
                return False

            channel = self._channels.get(instance_id)
            if channel is not None and channel.is_running:
                return True
            if channel is not None:
                try:
                    await channel.stop()
                except Exception:
                    logger.exception("Error stopping non-running channel instance before readiness retry")
                    return False
                if self._channels.get(instance_id) is channel:
                    self._channels.pop(instance_id, None)

            for attempt in range(max(1, attempts)):
                if attempt > 0:
                    logger.info("Retrying channel instance startup after readiness check")
                if await self._start_channel(
                    provider,
                    channel_config,
                    channel_instance_id=instance_id,
                ):
                    return True
            return False

    async def stop(self) -> None:
        """Stop all channels and the manager."""
        for name, channel in list(self._channels.items()):
            try:
                await channel.stop()
                logger.info("Channel stopped")
            except Exception:
                logger.exception("Error stopping channel")
        self._channels.clear()

        await self.manager.stop()
        self._running = False
        logger.info("ChannelService stopped")

    def _load_channel_config(self, name: str) -> dict[str, Any] | None:
        """Load the latest config for a specific channel from disk.

        Uses ``get_app_config()`` which detects file changes via config
        signature, so edits to ``config.yaml`` are picked up without a process
        restart.
        Falls back to the cached ``self._config`` when config loading fails.
        """
        try:
            from deerflow.config.app_config import get_app_config

            app_config = get_app_config()
            extra = app_config.model_extra or {}
            channels_config = dict(extra.get("channels") or {})
            channel_config = channels_config.get(name)
            if isinstance(channel_config, dict):
                # Update the cached config so get_status() stays consistent.
                self._config[name] = channel_config
                self._instance_configs[name] = (name, dict(channel_config))
                return channel_config
        except Exception:
            logger.exception("Failed to reload config for channel %s, using cached version", name)
        return self._config.get(name)

    async def restart_channel(self, name: str, *, reload_config: bool = True) -> bool:
        """Restart a specific channel. Returns True if successful."""
        channel = self._channels.get(name)
        if channel is not None:
            try:
                await channel.stop()
            except Exception:
                logger.exception("Error stopping channel for restart")
                return False
            if self._channels.get(name) is channel:
                self._channels.pop(name, None)

        if reload_config:
            # Reading config.yaml and the runtime store is disk IO; keep it
            # off the event loop.
            config = await asyncio.to_thread(self._load_channel_config, name)
        else:
            config = self._config.get(name)
        if not config or not isinstance(config, dict):
            logger.warning("No config for requested channel")
            return False

        if not config.get("enabled", False):
            logger.info("Channel %s is disabled, skipping restart", name)
            return True

        return await self._start_channel(name, config)

    async def configure_channel(self, name: str, config: dict[str, Any]) -> bool:
        """Apply runtime config for a channel and restart it if the service is running."""
        self._config[name] = dict(config)
        self._instance_configs[name] = (name, dict(config))
        if not self._running:
            return True
        # The caller just supplied the authoritative config (e.g. credentials
        # entered in the browser that are never written to config.yaml) — a
        # file reload here would clobber it with the stale on-disk entry.
        return await self.restart_channel(name, reload_config=False)

    async def configure_channel_instance(
        self,
        channel_instance_id: str,
        provider: str,
        config: dict[str, Any],
    ) -> bool:
        """Apply runtime configuration for one exact project channel instance."""

        instance_id = normalize_channel_instance_id(
            provider,
            channel_instance_id,
        )
        self._instance_configs[instance_id] = (provider, dict(config))
        if not self._running:
            return True
        return await self.restart_channel_instance(instance_id)

    def set_channel_instance_authority(
        self,
        authority: Any,
    ) -> None:
        """Install the Coordinator's exact lease revalidation callback."""

        self._instance_authority_guard.set_authority(authority)

    async def restart_channel_instance(self, channel_instance_id: str) -> bool:
        """Restart one exact channel instance without touching its siblings."""

        entry = self._instance_configs.get(channel_instance_id)
        if entry is None:
            logger.warning("No config for requested channel instance")
            return False
        provider, config = entry
        channel = self._channels.get(channel_instance_id)
        if channel is not None:
            try:
                await channel.stop()
            except Exception:
                logger.exception("Error stopping channel instance for restart")
                return False
            if self._channels.get(channel_instance_id) is channel:
                self._channels.pop(channel_instance_id, None)
        if not config.get("enabled", False):
            return True
        return await self._start_channel(
            provider,
            config,
            channel_instance_id=channel_instance_id,
        )

    async def remove_channel(self, name: str) -> bool:
        """Remove runtime config for a channel and stop it if currently running."""
        channel = self._channels.get(name)
        if channel is not None:
            try:
                await channel.stop()
            except Exception:
                logger.exception("Error stopping channel for removal")
                return False
            if self._channels.get(name) is channel:
                self._channels.pop(name, None)
        self._config.pop(name, None)
        self._instance_configs.pop(name, None)
        logger.info("Channel stopped and removed")
        return True

    async def remove_channel_instance(self, channel_instance_id: str) -> bool:
        """Stop and forget one exact channel instance."""

        channel = self._channels.get(channel_instance_id)
        if channel is None:
            self._instance_configs.pop(channel_instance_id, None)
            return True
        try:
            await channel.stop()
            if self._channels.get(channel_instance_id) is channel:
                self._channels.pop(channel_instance_id, None)
            self._instance_configs.pop(channel_instance_id, None)
            logger.info("Channel instance stopped and removed")
            return True
        except Exception:
            logger.exception("Error stopping channel instance for removal")
            return False

    async def _start_channel(
        self,
        name: str,
        config: dict[str, Any],
        *,
        channel_instance_id: str | None = None,
    ) -> bool:
        """Instantiate and start a single channel."""
        import_path = _CHANNEL_REGISTRY.get(name)
        if not import_path:
            logger.warning("Unknown channel type")
            return False

        instance_id = normalize_channel_instance_id(
            name,
            channel_instance_id,
        )
        if instance_id in self._channels:
            logger.error("Cannot start channel while a previous instance still requires cleanup")
            return False

        try:
            from deerflow.reflection import resolve_class

            channel_cls = resolve_class(import_path, base_class=None)
        except Exception:
            logger.exception("Failed to import channel class")
            return False

        channel: Channel | None = None
        try:
            config = dict(config)
            config["channel_instance_id"] = instance_id
            config["channel_store"] = self.store
            if self._connection_repo is not None:
                config["connection_repo"] = self._connection_repo
            if self._connection_service is not None:
                config["connection_service"] = self._connection_service
            if channel_instance_id is not None and self._channel_group_binding_service is not None:
                config["channel_group_binding_service"] = self._channel_group_binding_service
            channel = channel_cls(bus=self.bus, config=config)
            self._channels[instance_id] = channel
            await channel.start()
            if not channel.is_running:
                try:
                    await channel.stop()
                except Exception as exc:
                    logger.error(
                        "Channel failed to start and cleanup is incomplete: %s",
                        type(exc).__name__,
                    )
                    return False
                if self._channels.get(instance_id) is channel:
                    self._channels.pop(instance_id, None)
                logger.error("Channel did not enter a running state after start()")
                return False
            logger.info("Channel started")
            return True
        except Exception as exc:
            if channel is not None:
                try:
                    await channel.stop()
                except Exception as cleanup_exc:
                    logger.error(
                        "Channel start failed and cleanup is incomplete: %s",
                        type(cleanup_exc).__name__,
                    )
                    return False
                if self._channels.get(instance_id) is channel:
                    self._channels.pop(instance_id, None)
            logger.error("Failed to start channel: %s", type(exc).__name__)
            return False

    def get_status(self) -> dict[str, Any]:
        """Return status information for all channels."""
        channels_status = {}
        for name in _CHANNEL_REGISTRY:
            config = self._config.get(name, {})
            enabled = isinstance(config, dict) and config.get("enabled", False)
            running = name in self._channels and self._channels[name].is_running
            channels_status[name] = {
                "enabled": enabled,
                "running": running,
            }
        return {
            "service_running": self._running,
            "channels": channels_status,
        }

    def get_channel(self, name: str) -> Channel | None:
        """Return a running channel instance by name when available."""
        return self._channels.get(name)

    def get_channel_instance(self, channel_instance_id: str) -> Channel | None:
        """Return one exact running channel instance."""

        return self._channels.get(channel_instance_id)

    def get_channel_instance_status(self, channel_instance_id: str) -> dict[str, Any] | None:
        """Return bounded runtime status for one configured channel instance."""

        entry = self._instance_configs.get(channel_instance_id)
        if entry is None:
            return None
        provider, config = entry
        channel = self._channels.get(channel_instance_id)
        return {
            "channel_instance_id": channel_instance_id,
            "provider": provider,
            "enabled": bool(config.get("enabled", False)),
            "running": channel is not None and channel.is_running,
        }

    def is_channel_enabled(self, name: str) -> bool:
        """Return whether ``channels.<name>.enabled`` is truthy in the live config.

        Tracks the runtime-authoritative ``_config`` dict, which
        :meth:`configure_channel` updates when the UI flips the
        enabled flag — so callers that read this between requests get
        the current effective setting without re-reading config.yaml.
        Used by the GitHub webhook router as a fan-out kill-switch:
        ``channels.github.enabled: false`` skips dispatch even though
        the webhook route itself remains mounted (which is governed by
        ``GITHUB_WEBHOOK_SECRET``, not this flag).
        """
        config = self._config.get(name)
        if not isinstance(config, dict):
            return False
        return bool(config.get("enabled", False))

    def get_channel_config(self, name: str) -> dict[str, Any] | None:
        """Return a shallow copy of the live ``channels.<name>`` block, or None.

        Mirrors :meth:`is_channel_enabled` in tracking the runtime-
        authoritative ``_config`` dict, so callers see the same effective
        configuration the manager sees — including any updates pushed via
        :meth:`configure_channel` from the UI. Returns ``None`` when no
        config exists for ``name`` (rather than an empty dict) so callers
        can distinguish "not configured" from "configured with defaults".
        The shallow copy keeps callers from accidentally mutating live
        config state.
        """
        config = self._config.get(name)
        if not isinstance(config, dict):
            return None
        return dict(config)


# -- singleton access -------------------------------------------------------

_channel_service: ChannelService | None = None


def get_channel_service() -> ChannelService | None:
    """Get the singleton ChannelService instance (if started)."""
    return _channel_service


async def start_channel_service(
    app_config: AppConfig | None = None,
    *,
    app: Any | None = None,
) -> ChannelService:
    """Create and start the global ChannelService from app config."""
    global _channel_service
    if _channel_service is not None:
        return _channel_service
    # Config resolution may read the operator config when no parsed AppConfig
    # was supplied; keep that synchronous work off the event loop.
    _channel_service = await asyncio.to_thread(
        ChannelService.from_app_config,
        app_config,
        gateway_app=app,
    )
    await _channel_service.start()
    return _channel_service


async def stop_channel_service() -> None:
    """Stop the global ChannelService."""
    global _channel_service
    if _channel_service is not None:
        await _channel_service.stop()
        _channel_service = None
