"""Feishu/Lark channel — connects to Feishu via WebSocket (no public IP needed)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Literal

from app.channel_group_bindings.errors import ProjectChannelGroupBindingError
from app.channels.base import Channel
from app.channels.commands import (
    is_known_channel_command,
    is_removed_channel_command,
    parse_group_bind_command,
)
from app.channels.connection_identity import (
    attach_connection_identity,
    attach_resolved_connection_identity,
)
from app.channels.instance_identity import persisted_channel_instance_id
from app.channels.message_bus import (
    PENDING_CLARIFICATION_METADATA_KEY,
    RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY,
    InboundMessage,
    InboundMessageType,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)
from app.private_work.errors import PrivateWorkError
from app.project_channels.providers import is_allowed_channel_public_value
from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.sandbox.sandbox_provider import get_sandbox_provider

logger = logging.getLogger(__name__)
PENDING_CLARIFICATION_TTL_SECONDS = 30 * 60
FEISHU_INBOUND_BATCH_WINDOW_SECONDS = 0.75
FEISHU_WS_START_TIMEOUT_SECONDS = 10.0
FEISHU_GROUP_NAME_TIMEOUT_SECONDS = 5.0
_FEISHU_WS_CONNECT_LOCK = threading.Lock()


def _is_feishu_command(text: str) -> bool:
    return is_known_channel_command(text)


class FeishuChannel(Channel):
    """Feishu/Lark IM channel using the ``lark-oapi`` WebSocket client.

    Configuration keys (in ``config.yaml`` under ``channels.feishu``):
        - ``app_id``: Feishu app ID.
        - ``app_secret``: Feishu app secret.
        - ``verification_token``: (optional) Event verification token.

    The channel uses WebSocket long-connection mode so no public IP is required.

    Message flow:
        1. User sends a message → bot adds "OK" emoji reaction
        2. Bot replies with a card: "Working on it......"
        3. Agent processes the message and returns a result
        4. Bot updates the card with the result
        5. Bot adds "DONE" emoji reaction to the original message
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="feishu", bus=bus, config=config)
        self._thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_client = None
        self._ws_stop_event = threading.Event()
        self._ws_startup_event = threading.Event()
        self._ws_start_succeeded = False
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._api_client = None
        self._CreateMessageReactionRequest = None
        self._CreateMessageReactionRequestBody = None
        self._Emoji = None
        self._PatchMessageRequest = None
        self._PatchMessageRequestBody = None
        self._background_tasks: set[asyncio.Task] = set()
        self._running_card_ids: dict[str, str] = {}
        self._running_card_tasks: dict[str, asyncio.Task] = {}
        self._pending_clarifications: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._pending_inbound_batches: dict[tuple[str, str], dict[str, Any]] = {}
        self._CreateFileRequest = None
        self._CreateFileRequestBody = None
        self._CreateImageRequest = None
        self._CreateImageRequestBody = None
        self._GetMessageResourceRequest = None
        self._thread_lock = threading.Lock()

    @staticmethod
    def _non_empty_str(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _pending_key(chat_id: str, user_id: str) -> tuple[str, str]:
        return (chat_id, user_id)

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def is_running(self) -> bool:
        if not self._running:
            return False
        return self._thread is not None and self._thread.is_alive()

    def _build_event_handler(self, lark):
        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_im_message_message_read_v1(self._on_ignored_message_event)
            .register_p2_im_message_reaction_created_v1(self._on_ignored_message_event)
            .register_p2_im_message_reaction_deleted_v1(self._on_ignored_message_event)
            .register_p2_im_message_recalled_v1(self._on_ignored_message_event)
            .build()
        )

    async def start(self) -> None:
        if self._running:
            return
        if self._thread is not None:
            await self.stop()

        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import (
                CreateFileRequest,
                CreateFileRequestBody,
                CreateImageRequest,
                CreateImageRequestBody,
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
                CreateMessageRequest,
                CreateMessageRequestBody,
                Emoji,
                GetMessageResourceRequest,
                PatchMessageRequest,
                PatchMessageRequestBody,
                ReplyMessageRequest,
                ReplyMessageRequestBody,
            )
        except ImportError:
            logger.error("lark-oapi is not installed. Install it with: uv add lark-oapi")
            return

        self._lark = lark
        self._CreateMessageRequest = CreateMessageRequest
        self._CreateMessageRequestBody = CreateMessageRequestBody
        self._ReplyMessageRequest = ReplyMessageRequest
        self._ReplyMessageRequestBody = ReplyMessageRequestBody
        self._CreateMessageReactionRequest = CreateMessageReactionRequest
        self._CreateMessageReactionRequestBody = CreateMessageReactionRequestBody
        self._Emoji = Emoji
        self._PatchMessageRequest = PatchMessageRequest
        self._PatchMessageRequestBody = PatchMessageRequestBody
        self._CreateFileRequest = CreateFileRequest
        self._CreateFileRequestBody = CreateFileRequestBody
        self._CreateImageRequest = CreateImageRequest
        self._CreateImageRequestBody = CreateImageRequestBody
        self._GetMessageResourceRequest = GetMessageResourceRequest

        app_id = self.config.get("app_id", "")
        app_secret = self.config.get("app_secret", "")
        domain = self.config.get("domain", "https://open.feishu.cn")

        if not app_id or not app_secret:
            logger.error("Feishu channel requires app_id and app_secret")
            return
        if not is_allowed_channel_public_value("feishu", "domain", domain):
            logger.error("Feishu channel domain must use an official HTTPS endpoint")
            return
        assert isinstance(domain, str)
        domain = domain.strip()

        self._api_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).build()
        logger.info("[Feishu] using domain: %s", domain)
        self._main_loop = asyncio.get_event_loop()

        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Both ws.Client construction and start() must happen in a dedicated
        # thread with its own event loop.  lark-oapi caches the running loop
        # at construction time and later calls loop.run_until_complete(),
        # which conflicts with an already-running uvloop.
        self._thread = threading.Thread(
            target=self._run_ws,
            args=(app_id, app_secret, domain),
            daemon=True,
        )
        self._ws_stop_event.clear()
        self._ws_startup_event.clear()
        self._ws_start_succeeded = False
        self._thread.start()
        startup_signalled = await asyncio.to_thread(
            self._ws_startup_event.wait,
            FEISHU_WS_START_TIMEOUT_SECONDS,
        )
        if startup_signalled and self._ws_start_succeeded and self._running and self._thread.is_alive():
            logger.info("Feishu channel started")
            return

        if startup_signalled:
            logger.error("Feishu WebSocket startup failed")
        else:
            # lark-oapi performs its endpoint request with requests.post()
            # without an SDK timeout. Bound our async startup wait, fence the
            # late thread with the stop flag, and retain it if join cannot yet
            # prove termination so ChannelService never starts a replacement.
            logger.error("Feishu WebSocket startup timed out")
        self._ws_start_succeeded = False
        try:
            await self.stop()
        finally:
            self._api_client = None

    def _run_ws(self, app_id: str, app_secret: str, domain: str) -> None:
        """Construct and run the lark WS client in a thread with a fresh event loop.

        The lark-oapi SDK captures a module-level event loop at import time
        (``lark_oapi.ws.client.loop``).  When uvicorn uses uvloop, that
        captured loop is the *main* thread's uvloop — which is already
        running, so ``loop.run_until_complete()`` inside ``Client.start()``
        raises ``RuntimeError``.

        We work around this by creating a plain asyncio event loop per
        instance and isolating the SDK's module-global scheduling point while
        each connection is established. Receive callbacks then use their
        running instance loop directly, which permits multiple Feishu apps in
        one Gateway process.
        """
        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        asyncio.set_event_loop(loop)
        try:
            import lark_oapi as lark
            import lark_oapi.ws.client as _ws_client_mod

            owner = self

            class _InstanceSafeWsClient(lark.ws.Client):
                async def _connect(self) -> None:
                    # lark-oapi 1.x uses a module-global event loop only to
                    # schedule its receive task. Serialize that short section
                    # and restore the prior value so sibling project instances
                    # never schedule work onto one another's loops.
                    with _FEISHU_WS_CONNECT_LOCK:
                        previous_loop = _ws_client_mod.loop
                        _ws_client_mod.loop = loop
                        try:
                            await super()._connect()
                        finally:
                            _ws_client_mod.loop = previous_loop

                async def _receive_message_loop(self) -> None:
                    try:
                        while owner._running and not owner._ws_stop_event.is_set():
                            if self._conn is None:
                                raise _ws_client_mod.ConnectionClosedException("connection is closed")
                            message = await self._conn.recv()
                            asyncio.get_running_loop().create_task(self._handle_message(message))
                    except Exception:
                        await self._disconnect()
                        if owner._running and not owner._ws_stop_event.is_set():
                            await self._reconnect()

            event_handler = self._build_event_handler(lark)
            ws_client = _InstanceSafeWsClient(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=event_handler,
                # The SDK's INFO connection log includes the WebSocket URL,
                # whose query string contains short-lived access material.
                log_level=lark.LogLevel.WARNING,
                domain=domain,
            )
            self._ws_client = ws_client

            async def run_instance() -> None:
                await ws_client._connect()
                if not self._running or self._ws_stop_event.is_set():
                    await ws_client._disconnect()
                    return
                self._ws_start_succeeded = True
                self._ws_startup_event.set()
                ping_task = asyncio.create_task(ws_client._ping_loop())
                try:
                    while self._running and not self._ws_stop_event.is_set():
                        await asyncio.sleep(0.25)
                finally:
                    ping_task.cancel()
                    await asyncio.gather(ping_task, return_exceptions=True)
                    await ws_client._disconnect()

            loop.run_until_complete(run_instance())
        except Exception as exc:
            self._ws_start_succeeded = False
            if self._running:
                logger.error(
                    "Feishu WebSocket startup or receive loop failed: %s",
                    type(exc).__name__,
                )
            self._running = False
        finally:
            self._ws_startup_event.set()
            self._ws_client = None
            self._ws_loop = None
            loop.close()

    def _on_ignored_message_event(self, event) -> None:
        logger.debug("[Feishu] ignoring non-content message event: %s", type(event).__name__)

    async def stop(self) -> None:
        self._running = False
        self._ws_start_succeeded = False
        self._ws_stop_event.set()
        self._ws_startup_event.set()
        self.bus.unsubscribe_outbound(self._on_outbound)
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        for task in list(self._running_card_tasks.values()):
            task.cancel()
        self._running_card_tasks.clear()
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
            if thread.is_alive():
                raise RuntimeError("Feishu websocket thread did not stop")
            if self._thread is thread:
                self._thread = None
        self._api_client = None
        logger.info("Feishu channel stopped")

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        if not self._api_client:
            logger.warning("[Feishu] send called but no api_client available")
            return

        logger.info(
            "[Feishu] sending reply: threaded=%s, text_len=%d",
            bool(msg.thread_ts),
            len(msg.text),
        )

        await self._send_with_retry(
            lambda: self._send_card_message(msg),
            max_retries=_max_retries,
            log_prefix="[Feishu]",
        )

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        if not self._api_client:
            return False

        # Check size limits (image: 10MB, file: 30MB)
        if attachment.is_image and attachment.size > 10 * 1024 * 1024:
            logger.warning("[Feishu] image too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False
        if not attachment.is_image and attachment.size > 30 * 1024 * 1024:
            logger.warning("[Feishu] file too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False

        try:
            if attachment.is_image:
                file_key = await self._upload_image(attachment.actual_path)
                msg_type = "image"
                content = json.dumps({"image_key": file_key})
            else:
                file_key = await self._upload_file(attachment.actual_path, attachment.filename)
                msg_type = "file"
                content = json.dumps({"file_key": file_key})

            if msg.thread_ts:
                request = self._ReplyMessageRequest.builder().message_id(msg.thread_ts).request_body(self._ReplyMessageRequestBody.builder().msg_type(msg_type).content(content).build()).build()
                response = await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            else:
                request = self._CreateMessageRequest.builder().receive_id_type("chat_id").request_body(self._CreateMessageRequestBody.builder().receive_id(msg.chat_id).msg_type(msg_type).content(content).build()).build()
                response = await asyncio.to_thread(self._api_client.im.v1.message.create, request)
            if not response.success():
                raise RuntimeError(f"Feishu file send failed: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}")

            logger.info("[Feishu] file sent: %s (type=%s)", attachment.filename, msg_type)
            return True
        except Exception:
            logger.exception("[Feishu] failed to upload/send file: %s", attachment.filename)
            return False

    async def _upload_image(self, path) -> str:
        """Upload an image to Feishu and return the image_key."""
        with open(str(path), "rb") as f:
            request = self._CreateImageRequest.builder().request_body(self._CreateImageRequestBody.builder().image_type("message").image(f).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.image.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu image upload failed: code={response.code}, msg={response.msg}")
        return response.data.image_key

    async def _upload_file(self, path, filename: str) -> str:
        """Upload a file to Feishu and return the file_key."""
        suffix = path.suffix.lower() if hasattr(path, "suffix") else ""
        if suffix in (".xls", ".xlsx", ".csv"):
            file_type = "xls"
        elif suffix in (".ppt", ".pptx"):
            file_type = "ppt"
        elif suffix == ".pdf":
            file_type = "pdf"
        elif suffix in (".doc", ".docx"):
            file_type = "doc"
        else:
            file_type = "stream"

        with open(str(path), "rb") as f:
            request = self._CreateFileRequest.builder().request_body(self._CreateFileRequestBody.builder().file_type(file_type).file_name(filename).file(f).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.file.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu file upload failed: code={response.code}, msg={response.msg}")
        return response.data.file_key

    async def receive_file(self, msg: InboundMessage, thread_id: str, *, user_id: str | None = None) -> InboundMessage:
        """Download a Feishu file into the thread uploads directory.

        Returns the sandbox virtual path when the image is persisted successfully.
        """
        if not msg.thread_ts:
            logger.warning(
                "[Feishu] received file message without a reply coordinate",
            )
            return msg
        files = msg.files
        if not files:
            logger.warning("[Feishu] received file message with no files")
            return msg
        text = msg.text
        for file in files:
            if file.get("image_key"):
                virtual_path = await self._receive_single_file(msg.thread_ts, file["image_key"], "image", thread_id, user_id=user_id)
                text = text.replace("[image]", virtual_path, 1)
            elif file.get("file_key"):
                virtual_path = await self._receive_single_file(msg.thread_ts, file["file_key"], "file", thread_id, user_id=user_id)
                text = text.replace("[file]", virtual_path, 1)
        msg.text = text
        return msg

    async def _receive_single_file(
        self,
        message_id: str,
        file_key: str,
        type: Literal["image", "file"],
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> str:
        request = self._GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(type).build()

        def inner():
            return self._api_client.im.v1.message_resource.get(request)

        try:
            response = await asyncio.to_thread(inner)
        except Exception:
            logger.exception("[Feishu] resource get request failed: type=%s", type)
            return f"Failed to obtain the [{type}]"

        if not response.success():
            logger.warning(
                "[Feishu] resource get failed: type=%s, code=%s, msg=%s",
                type,
                response.code,
                response.msg,
            )
            return f"Failed to obtain the [{type}]"

        image_stream = getattr(response, "file", None)
        if image_stream is None:
            logger.warning(
                "[Feishu] resource get returned no file stream: type=%s",
                type,
            )
            return f"Failed to obtain the [{type}]"

        try:
            content: bytes = await asyncio.to_thread(image_stream.read)
        except Exception:
            logger.exception(
                "[Feishu] failed to read resource stream: type=%s",
                type,
            )
            return f"Failed to obtain the [{type}]"

        if not content:
            logger.warning("[Feishu] empty resource content: type=%s", type)
            return f"Failed to obtain the [{type}]"

        paths = get_paths()
        effective_user_id = user_id or get_effective_user_id()
        paths.ensure_thread_dirs(thread_id, user_id=effective_user_id)
        uploads_dir = paths.sandbox_uploads_dir(thread_id, user_id=effective_user_id).resolve()

        ext = "png" if type == "image" else "bin"
        raw_filename = getattr(response, "file_name", "") or f"feishu_{file_key[-12:]}.{ext}"

        # Sanitize filename: preserve extension, replace path chars in name part
        if "." in raw_filename:
            name_part, ext = raw_filename.rsplit(".", 1)
            name_part = re.sub(r"[./\\]", "_", name_part)
            filename = f"{name_part}.{ext}"
        else:
            filename = re.sub(r"[./\\]", "_", raw_filename)
        resolved_target = uploads_dir / filename

        def down_load():
            # use thread_lock to avoid filename conflicts when writing
            with self._thread_lock:
                resolved_target.write_bytes(content)

        try:
            await asyncio.to_thread(down_load)
        except Exception:
            logger.exception("[Feishu] failed to persist downloaded resource: %s, type=%s", resolved_target, type)
            return f"Failed to obtain the [{type}]"

        virtual_path = f"{VIRTUAL_PATH_PREFIX}/uploads/{resolved_target.name}"

        try:
            sandbox_provider = get_sandbox_provider()
            sandbox_id = sandbox_provider.acquire(thread_id, user_id=effective_user_id)
            if sandbox_id != "local":
                sandbox = sandbox_provider.get(sandbox_id)
                if sandbox is None:
                    logger.warning("[Feishu] sandbox not found for thread_id=%s", thread_id)
                    return f"Failed to obtain the [{type}]"
                sandbox.update_file(virtual_path, content)
        except Exception:
            logger.exception("[Feishu] failed to sync resource into non-local sandbox: %s", virtual_path)
            return f"Failed to obtain the [{type}]"

        logger.info("[Feishu] downloaded resource mapped: path=%s", virtual_path)
        return virtual_path

    # -- message formatting ------------------------------------------------

    @staticmethod
    def _build_card_content(text: str) -> str:
        """Build a Feishu interactive card with markdown content.

        Feishu's interactive card format natively renders markdown, including
        headers, bold/italic, code blocks, lists, and links.
        """
        card = {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "elements": [{"tag": "markdown", "content": text}],
        }
        return json.dumps(card)

    # -- reaction helpers --------------------------------------------------

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """Add an emoji reaction to a message."""
        if not self._api_client or not self._CreateMessageReactionRequest:
            return
        try:
            request = self._CreateMessageReactionRequest.builder().message_id(message_id).request_body(self._CreateMessageReactionRequestBody.builder().reaction_type(self._Emoji.builder().emoji_type(emoji_type).build()).build()).build()
            response = await asyncio.to_thread(
                self._api_client.im.v1.message_reaction.create,
                request,
            )
            if not response.success():
                logger.warning(
                    "[Feishu] reaction failed: emoji=%s, code=%s, msg=%s",
                    emoji_type,
                    response.code,
                    response.msg,
                )
                return
            logger.info("[Feishu] reaction added: emoji=%s", emoji_type)
        except Exception:
            logger.exception("[Feishu] failed to add reaction: emoji=%s", emoji_type)

    async def _reply_card(self, message_id: str, text: str) -> str | None:
        """Reply with an interactive card and return the created card message ID."""
        if not self._api_client:
            return None

        content = self._build_card_content(text)
        request = self._ReplyMessageRequest.builder().message_id(message_id).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).build()).build()
        response = await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
        if not response.success():
            raise RuntimeError(f"Feishu card reply failed: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}")
        response_data = getattr(response, "data", None)
        return getattr(response_data, "message_id", None)

    async def _send_connection_confirmation(self, *, message_id: str, chat_id: str, text: str) -> None:
        """Confirm a connection in-channel, falling back when reply delivery fails."""
        try:
            reply_message_id = await self._reply_card(message_id, text)
            if reply_message_id:
                logger.info("[Feishu] connection confirmation replied successfully")
                return
            logger.warning(
                "[Feishu] connection confirmation reply returned no message id; falling back to a new chat card",
            )
        except Exception:
            logger.exception(
                "[Feishu] connection confirmation reply failed; falling back to a new chat card",
            )

        try:
            await self._create_card(chat_id, text)
            logger.info("[Feishu] connection confirmation sent as a new chat card")
        except Exception:
            logger.exception("[Feishu] connection confirmation fallback failed")
            raise

    async def _create_card(self, chat_id: str, text: str) -> None:
        """Create a new card message in the target chat."""
        if not self._api_client:
            return

        content = self._build_card_content(text)
        request = self._CreateMessageRequest.builder().receive_id_type("chat_id").request_body(self._CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(content).build()).build()
        response = await asyncio.to_thread(
            self._api_client.im.v1.message.create,
            request,
        )
        if not response.success():
            raise RuntimeError(f"Feishu card creation failed: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}")

    async def _update_card(self, message_id: str, text: str) -> None:
        """Patch an existing card message in place."""
        if not self._api_client or not self._PatchMessageRequest:
            return

        content = self._build_card_content(text)
        request = self._PatchMessageRequest.builder().message_id(message_id).request_body(self._PatchMessageRequestBody.builder().content(content).build()).build()
        response = await asyncio.to_thread(
            self._api_client.im.v1.message.patch,
            request,
        )
        if not response.success():
            raise RuntimeError(f"Feishu card update failed: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}")

    def _track_background_task(self, task: asyncio.Task, *, name: str, msg_id: str) -> None:
        """Keep a strong reference to fire-and-forget tasks and surface errors."""
        self._background_tasks.add(task)
        task.add_done_callback(lambda done_task, task_name=name, mid=msg_id: self._finalize_background_task(done_task, task_name, mid))

    def _finalize_background_task(self, task: asyncio.Task, name: str, msg_id: str) -> None:
        self._background_tasks.discard(task)
        self._log_task_error(task, name, msg_id)

    async def _create_running_card(self, source_message_id: str, text: str) -> str | None:
        """Create the running card and cache its message ID when available."""
        running_card_id = await self._reply_card(source_message_id, text)
        if running_card_id:
            self._running_card_ids[source_message_id] = running_card_id
            logger.info("[Feishu] running card created")
        else:
            logger.warning(
                "[Feishu] running card creation returned no message id; subsequent updates will fall back to new replies",
            )
        return running_card_id

    def _ensure_running_card_started(self, source_message_id: str, text: str = "thinking...") -> asyncio.Task | None:
        """Start running-card creation once per source message."""
        running_card_id = self._running_card_ids.get(source_message_id)
        if running_card_id:
            return None

        running_card_task = self._running_card_tasks.get(source_message_id)
        if running_card_task:
            return running_card_task

        running_card_task = asyncio.create_task(self._create_running_card(source_message_id, text))
        self._running_card_tasks[source_message_id] = running_card_task
        running_card_task.add_done_callback(lambda done_task, mid=source_message_id: self._finalize_running_card_task(mid, done_task))
        return running_card_task

    def _finalize_running_card_task(self, source_message_id: str, task: asyncio.Task) -> None:
        if self._running_card_tasks.get(source_message_id) is task:
            self._running_card_tasks.pop(source_message_id, None)
        self._log_task_error(task, "create_running_card", source_message_id)

    async def _ensure_running_card(self, source_message_id: str, text: str = "thinking...") -> str | None:
        """Ensure the in-thread running card exists and track its message ID."""
        running_card_id = self._running_card_ids.get(source_message_id)
        if running_card_id:
            return running_card_id

        running_card_task = self._ensure_running_card_started(source_message_id, text)
        if running_card_task is None:
            return self._running_card_ids.get(source_message_id)
        return await running_card_task

    async def _send_running_reply(self, message_id: str) -> None:
        """Reply to a message in-thread with a running card."""
        try:
            await self._ensure_running_card(message_id)
        except Exception:
            logger.exception("[Feishu] failed to send running reply")

    async def _send_card_message(self, msg: OutboundMessage) -> None:
        """Send or update the Feishu card tied to the current request."""
        source_message_id = msg.thread_ts
        if source_message_id:
            running_card_id = self._running_card_ids.get(source_message_id)
            awaited_running_card_task = False

            if not running_card_id:
                running_card_task = self._running_card_tasks.get(source_message_id)
                if running_card_task:
                    awaited_running_card_task = True
                    running_card_id = await running_card_task

            if running_card_id:
                try:
                    await self._update_card(running_card_id, msg.text)
                except Exception:
                    if not msg.is_final:
                        raise
                    logger.exception(
                        "[Feishu] failed to patch running card; falling back to final reply",
                    )
                    fallback_card_id = await self._reply_card(source_message_id, msg.text)
                    await self._remember_thread_mapping(msg, source_message_id, fallback_card_id)
                    self._remember_pending_clarification(msg, fallback_card_id)
                else:
                    await self._remember_thread_mapping(msg, source_message_id, running_card_id)
                    self._remember_pending_clarification(msg, running_card_id)
                    logger.info("[Feishu] running card updated")
            elif msg.is_final:
                final_card_id = await self._reply_card(source_message_id, msg.text)
                await self._remember_thread_mapping(msg, source_message_id, final_card_id)
                self._remember_pending_clarification(msg, final_card_id)
            elif awaited_running_card_task:
                logger.warning(
                    "[Feishu] running card task finished without a message id; skipping duplicate non-final creation",
                )
            else:
                created_card_id = await self._ensure_running_card(source_message_id, msg.text)
                await self._remember_thread_mapping(msg, source_message_id, created_card_id)

            if msg.is_final:
                self._running_card_ids.pop(source_message_id, None)
                await self._add_reaction(source_message_id, "DONE")
            return

        await self._create_card(msg.chat_id, msg.text)

    # -- internal ----------------------------------------------------------

    async def _remember_thread_mapping(self, msg: OutboundMessage, *topic_ids: str | None) -> None:
        store = self.config.get("channel_store")
        if store is None or not msg.thread_id or not msg.connection_id or msg.private_scope is None:
            return

        metadata_topic_ids = [
            msg.metadata.get("message_id"),
            msg.metadata.get("root_id"),
            msg.metadata.get("parent_id"),
            msg.metadata.get("thread_id"),
            msg.metadata.get("topic_id"),
        ]
        seen: set[str] = set()
        raw_topic_ids: list[str] = []
        for topic_id in [*topic_ids, *metadata_topic_ids]:
            topic_id = self._non_empty_str(topic_id)
            if not topic_id or topic_id in seen:
                continue
            seen.add(topic_id)
            raw_topic_ids.append(topic_id)

        conversation_id = msg.chat_id
        persisted_topic_ids: list[str] = raw_topic_ids
        if msg.resolved_conversation_id is not None:
            group_binding_service = self.config.get("channel_group_binding_service")
            if group_binding_service is None:
                logger.warning("[Feishu] guest topic alias persistence is unavailable")
                return
            try:
                aliases = group_binding_service.pseudonymize_topic_aliases(
                    provider="feishu",
                    channel_instance_id=self.channel_instance_id,
                    chat_id=msg.chat_id,
                    resolved_conversation_id=msg.resolved_conversation_id,
                    topic_ids=tuple(raw_topic_ids),
                )
            except Exception as exc:
                logger.warning(
                    "[Feishu] guest topic alias pseudonymization failed: %s",
                    type(exc).__name__,
                )
                return
            if len(aliases) != len(raw_topic_ids):
                logger.warning("[Feishu] guest topic alias pseudonymization returned an invalid result")
                return
            conversation_id = msg.resolved_conversation_id
            persisted_topic_ids = list(aliases)
            if msg.resolved_topic_id:
                persisted_topic_ids.insert(0, msg.resolved_topic_id)

        for topic_id in dict.fromkeys(persisted_topic_ids):
            try:
                await store.set_thread_id(
                    self.name,
                    conversation_id,
                    msg.thread_id,
                    topic_id=topic_id,
                    connection_id=msg.connection_id,
                    scope=msg.private_scope,
                )
            except Exception:
                logger.exception("[Feishu] failed to remember thread mapping")

    def _remember_pending_clarification(self, msg: OutboundMessage, card_message_id: str | None) -> None:
        if not msg.is_final or msg.metadata.get(PENDING_CLARIFICATION_METADATA_KEY) is not True:
            return

        user_id = self._non_empty_str(msg.metadata.get("user_id"))
        topic_id = self._non_empty_str(msg.metadata.get("topic_id"))
        source_message_id = self._non_empty_str(msg.thread_ts) or self._non_empty_str(msg.metadata.get("message_id"))
        if not (user_id and topic_id and msg.thread_id and source_message_id and card_message_id):
            return

        key = self._pending_key(msg.chat_id, user_id)
        pending = {
            "thread_id": msg.thread_id,
            "topic_id": topic_id,
            "source_message_id": source_message_id,
            "card_message_id": card_message_id,
            "created_at": time.time(),
        }
        with self._thread_lock:
            # Plain-message clarification continuity is a short-lived in-memory
            # hint; explicit Feishu replies are still covered by persisted
            # message-id mappings.
            self._pending_clarifications.setdefault(key, []).append(pending)
        logger.info(
            "[Feishu] pending clarification remembered: thread_id=%s",
            msg.thread_id,
        )

    def _consume_pending_clarification(self, chat_id: str, user_id: str) -> dict[str, Any] | None:
        key = self._pending_key(chat_id, user_id)
        with self._thread_lock:
            pending_items = self._pending_clarifications.get(key)
            if not pending_items:
                return None

            now = time.time()
            while pending_items:
                pending = pending_items.pop(0)
                created_at = pending.get("created_at")
                if isinstance(created_at, (int, float)) and now - created_at <= PENDING_CLARIFICATION_TTL_SECONDS:
                    if pending_items:
                        self._pending_clarifications[key] = pending_items
                    else:
                        self._pending_clarifications.pop(key, None)
                    return pending
                logger.info("[Feishu] pending clarification expired")

            self._pending_clarifications.pop(key, None)
            return None

    async def _resolve_persisted_topic_id(
        self,
        inbound: InboundMessage,
    ) -> tuple[str, bool]:
        store = self.config.get("channel_store")
        connection_id = self._non_empty_str(inbound.connection_id)
        scope = inbound.private_scope
        candidates = [
            inbound.metadata.get("root_id"),
            inbound.metadata.get("parent_id"),
            inbound.metadata.get("thread_id"),
        ]

        raw_candidates: list[str] = []
        for candidate in candidates:
            normalized = self._non_empty_str(candidate)
            if normalized and normalized not in raw_candidates:
                raw_candidates.append(normalized)

        conversation_id = inbound.chat_id
        persisted_candidates = raw_candidates
        if inbound.resolved_conversation_id is not None:
            if not raw_candidates:
                return inbound.topic_id or "", False
            group_binding_service = self.config.get("channel_group_binding_service")
            if group_binding_service is None:
                logger.warning("[Feishu] guest topic alias lookup is unavailable")
                return inbound.topic_id or "", False
            try:
                aliases = group_binding_service.pseudonymize_topic_aliases(
                    provider="feishu",
                    channel_instance_id=self.channel_instance_id,
                    chat_id=inbound.chat_id,
                    resolved_conversation_id=inbound.resolved_conversation_id,
                    topic_ids=tuple(raw_candidates),
                )
            except Exception as exc:
                logger.warning(
                    "[Feishu] guest topic alias lookup failed: %s",
                    type(exc).__name__,
                )
                return inbound.topic_id or "", False
            if len(aliases) != len(raw_candidates):
                logger.warning("[Feishu] guest topic alias lookup returned an invalid result")
                return inbound.topic_id or "", False
            conversation_id = inbound.resolved_conversation_id
            persisted_candidates = list(aliases)

        if store is not None and connection_id and scope is not None:
            for candidate, persisted_candidate in zip(
                raw_candidates,
                persisted_candidates,
                strict=True,
            ):
                try:
                    if await store.get_thread_id(
                        self.name,
                        conversation_id,
                        topic_id=persisted_candidate,
                        connection_id=connection_id,
                        scope=scope,
                    ):
                        if inbound.resolved_conversation_id is not None:
                            inbound.resolved_topic_id = persisted_candidate
                        return candidate, True
                except Exception:
                    logger.exception(
                        "[Feishu] failed to resolve stored topic mapping",
                    )

        return inbound.topic_id or "", False

    @staticmethod
    def _is_batchable_file_inbound(
        *,
        msg_type: InboundMessageType,
        text: str,
        files: list[dict[str, Any]],
        root_id: str | None,
        parent_id: str | None,
        thread_id: str | None,
    ) -> bool:
        return msg_type == InboundMessageType.CHAT and text in {"[file]", "[image]"} and len(files) == 1 and not (root_id or parent_id or thread_id)

    def _schedule_prepare_inbound(
        self,
        msg_id: str,
        inbound: InboundMessage,
        *,
        source_message_ids: list[str] | None = None,
    ) -> None:
        if self._main_loop and self._main_loop.is_running():
            logger.info(
                "[Feishu] publishing inbound message to bus: type=%s",
                inbound.msg_type.value,
            )
            fut = asyncio.run_coroutine_threadsafe(
                self._prepare_inbound(msg_id, inbound, source_message_ids=source_message_ids),
                self._main_loop,
            )
            fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "prepare_inbound", mid))
        else:
            logger.warning("[Feishu] main loop not running, cannot publish inbound message")

    def _schedule_batch_flush(self, key: tuple[str, str], source_message_id: str) -> None:
        if self._main_loop and self._main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._flush_pending_inbound_batch_after(key, source_message_id), self._main_loop)
            fut.add_done_callback(lambda f, mid=source_message_id: self._log_future_error(f, "flush_inbound_batch", mid))
        else:
            logger.warning("[Feishu] main loop not running, cannot flush inbound batch")

    def _queue_file_inbound_batch(self, msg_id: str, inbound: InboundMessage) -> bool:
        key = self._pending_key(inbound.chat_id, inbound.user_id)
        should_schedule_flush = False
        expired_batch: tuple[str, InboundMessage, list[str]] | None = None

        with self._thread_lock:
            batch = self._pending_inbound_batches.get(key)
            now = time.time()
            if batch:
                if now - batch["created_at"] <= FEISHU_INBOUND_BATCH_WINDOW_SECONDS:
                    batched_inbound = batch["inbound"]
                    batch["message_ids"].append(msg_id)
                    batch["text_parts"].append(inbound.text)
                    batched_inbound.text = "\n\n".join(part for part in batch["text_parts"] if part)
                    batched_inbound.files.extend(inbound.files)
                    batched_inbound.metadata["batched_message_ids"] = list(batch["message_ids"])
                    logger.info(
                        "[Feishu] batched inbound file message: files=%d",
                        len(batched_inbound.files),
                    )
                    return True

                expired_batch = (batch["anchor_message_id"], batch["inbound"], list(batch["message_ids"]))

            self._pending_inbound_batches[key] = {
                "anchor_message_id": msg_id,
                "created_at": now,
                "inbound": inbound,
                "message_ids": [msg_id],
                "text_parts": [inbound.text],
            }
            inbound.metadata["batched_message_ids"] = [msg_id]
            should_schedule_flush = True

        if should_schedule_flush:
            self._schedule_batch_flush(key, msg_id)
        if expired_batch:
            anchor_message_id, expired_inbound, source_message_ids = expired_batch
            self._schedule_prepare_inbound(anchor_message_id, expired_inbound, source_message_ids=source_message_ids)
        return True

    def _pop_pending_inbound_batch(self, key: tuple[str, str], *, anchor_message_id: str | None = None) -> tuple[str, InboundMessage, list[str]] | None:
        with self._thread_lock:
            batch = self._pending_inbound_batches.get(key)
            if not batch:
                return None
            if anchor_message_id is not None and batch["anchor_message_id"] != anchor_message_id:
                return None
            self._pending_inbound_batches.pop(key, None)
            return batch["anchor_message_id"], batch["inbound"], list(batch["message_ids"])

    async def _flush_pending_inbound_batch_after(self, key: tuple[str, str], anchor_message_id: str) -> None:
        await asyncio.sleep(FEISHU_INBOUND_BATCH_WINDOW_SECONDS)
        batch = self._pop_pending_inbound_batch(key, anchor_message_id=anchor_message_id)
        if not batch:
            return
        anchor_message_id, inbound, source_message_ids = batch
        logger.info(
            "[Feishu] flushing inbound file batch: messages=%d files=%d",
            len(source_message_ids),
            len(inbound.files),
        )
        await self._prepare_inbound(anchor_message_id, inbound, source_message_ids=source_message_ids)

    @staticmethod
    def _log_task_error(task: asyncio.Task, name: str, msg_id: str) -> None:
        """Callback for background asyncio tasks to surface errors."""
        try:
            exc = task.exception()
            if exc:
                logger.error("[Feishu] %s failed: %s", name, exc)
        except asyncio.CancelledError:
            logger.info("[Feishu] %s cancelled", name)
        except Exception:
            pass

    async def _prepare_inbound(self, msg_id: str, inbound, *, source_message_ids: list[str] | None = None) -> None:
        """Kick off Feishu side effects without delaying inbound dispatch."""
        if not await self._has_instance_authority():
            return
        inbound = await self._attach_connection_identity(inbound)
        persisted_topic_id, resolved_from_stored_mapping = await self._resolve_persisted_topic_id(inbound)
        if resolved_from_stored_mapping:
            inbound.topic_id = persisted_topic_id
            inbound.metadata["topic_id"] = persisted_topic_id
        elif inbound.msg_type == InboundMessageType.CHAT and not is_removed_channel_command(inbound.text) and inbound.metadata.get(RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY) is not True:
            pending = self._consume_pending_clarification(inbound.chat_id, inbound.user_id)
            pending_topic_id = self._non_empty_str(pending.get("topic_id")) if pending else None
            if pending_topic_id:
                inbound.topic_id = pending_topic_id
                inbound.metadata["topic_id"] = pending_topic_id
                inbound.metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] = True
                self._refresh_guest_topic_alias(inbound, pending_topic_id)
        reaction_message_ids = source_message_ids or [msg_id]
        for reaction_message_id in reaction_message_ids:
            reaction_task = asyncio.create_task(self._add_reaction(reaction_message_id, "OK"))
            self._track_background_task(reaction_task, name="add_reaction", msg_id=reaction_message_id)
        self._ensure_running_card_started(msg_id)
        await self.bus.publish_inbound(inbound)

    def _refresh_guest_topic_alias(
        self,
        inbound: InboundMessage,
        topic_id: str,
    ) -> None:
        """Keep a guest message's pseudonymous topic aligned with its raw reply target."""

        resolved_conversation_id = self._non_empty_str(inbound.resolved_conversation_id)
        if resolved_conversation_id is None:
            return

        group_binding_service = self.config.get("channel_group_binding_service")
        try:
            aliases = group_binding_service.pseudonymize_topic_aliases(
                provider="feishu",
                channel_instance_id=self.channel_instance_id,
                chat_id=inbound.chat_id,
                resolved_conversation_id=resolved_conversation_id,
                topic_ids=(topic_id,),
            )
        except Exception as exc:
            logger.warning(
                "[Feishu] guest clarification topic pseudonymization failed: %s",
                type(exc).__name__,
            )
            aliases = ()

        resolved_topic_id = self._non_empty_str(aliases[0]) if isinstance(aliases, tuple) and len(aliases) == 1 else None
        if resolved_topic_id is None:
            inbound.resolved_topic_id = None
            inbound.metadata["group_binding_unavailable"] = True
            return
        inbound.resolved_topic_id = resolved_topic_id

    async def _attach_connection_identity(self, inbound: InboundMessage) -> InboundMessage:
        group_binding_service = self.config.get("channel_group_binding_service")
        chat_type = self._non_empty_str(inbound.metadata.get("chat_type"))
        if chat_type != "p2p" and group_binding_service is not None:
            inbound.workspace_id = inbound.chat_id
            try:
                resolved = await group_binding_service.resolve_or_create_guest(
                    provider="feishu",
                    channel_instance_id=self.channel_instance_id,
                    chat_id=inbound.chat_id,
                    sender_id=inbound.user_id,
                    topic_id=inbound.topic_id,
                )
            except ProjectChannelGroupBindingError as exc:
                if exc.code == "GROUP_BINDING_NOT_FOUND":
                    inbound.metadata["group_binding_required"] = True
                elif exc.code == "GROUP_BINDING_AGENT_UNAVAILABLE":
                    inbound.metadata["group_binding_agent_unavailable"] = True
                else:
                    inbound.metadata["group_binding_unavailable"] = True
                return inbound
            attach_resolved_connection_identity(inbound, resolved)
            if inbound.connection_id is None:
                inbound.metadata["group_binding_required"] = True
            return inbound
        return await attach_connection_identity(
            inbound,
            repo=self._connection_repo,
            provider="feishu",
            workspace_id=inbound.chat_id,
        )

    async def _bind_group_from_code(
        self,
        *,
        message_id: str,
        chat_id: str,
        user_id: str,
        code: str,
        chat_type: str | None,
    ) -> bool:
        if not await self._has_instance_authority():
            return True
        service = self.config.get("channel_group_binding_service")
        if service is None or not code:
            await self._send_connection_confirmation(
                message_id=message_id,
                chat_id=chat_id,
                text="Feishu group connections are unavailable.",
            )
            return True
        if chat_type == "p2p":
            await self._send_connection_confirmation(
                message_id=message_id,
                chat_id=chat_id,
                text="Send this command in the Feishu group you want to connect.",
            )
            return True
        display_name = await self._resolve_group_display_name(chat_id)
        try:
            await service.complete_challenge(
                provider="feishu",
                channel_instance_id=self.channel_instance_id,
                code=code,
                chat_id=chat_id,
                sender_id=user_id,
                display_name=display_name,
            )
        except (PrivateWorkError, ProjectChannelGroupBindingError):
            await self._send_connection_confirmation(
                message_id=message_id,
                chat_id=chat_id,
                text="Feishu group connection code is invalid or expired.",
            )
            return True
        await self._send_connection_confirmation(
            message_id=message_id,
            chat_id=chat_id,
            text="Feishu group connected to ActWeave.",
        )
        return True

    async def _resolve_group_display_name(self, chat_id: str) -> str | None:
        """Read a safe display label without making group binding depend on it."""

        if self._api_client is None:
            return None
        try:
            from lark_oapi.api.im.v1 import GetChatRequest

            request = GetChatRequest.builder().chat_id(chat_id).build()
            response = await asyncio.wait_for(
                self._api_client.im.v1.chat.aget(request),
                timeout=FEISHU_GROUP_NAME_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "[Feishu] group display name lookup failed: %s",
                type(exc).__name__,
            )
            return None
        if not response.success():
            logger.warning(
                "[Feishu] group display name is unavailable: code=%s, log_id=%s",
                response.code,
                response.get_log_id(),
            )
            return None
        data = getattr(response, "data", None)
        name = getattr(data, "name", None)
        if not isinstance(name, str):
            return None
        normalized = name.strip()
        return normalized[:120] or None

    async def _bind_connection_from_connect_code(self, *, message_id: str, chat_id: str, user_id: str, code: str) -> bool:
        if not await self._has_instance_authority():
            return True
        connection_service = self.config.get("connection_service")
        if (self._connection_repo is None and connection_service is None) or not code:
            return False

        if not user_id or not chat_id:
            await self._reply_card(message_id, "Feishu connection could not be completed from this message.")
            return True

        metadata = {"chat_id": chat_id, "message_id": message_id}
        if connection_service is not None:
            try:
                await connection_service.complete_callback(
                    "feishu",
                    code,
                    user_id,
                    chat_id,
                    channel_instance_id=self.channel_instance_id,
                    metadata=metadata,
                    status="connected",
                )
            except PrivateWorkError:
                await self._send_connection_confirmation(
                    message_id=message_id,
                    chat_id=chat_id,
                    text="Feishu connection code is invalid or expired.",
                )
                return True
        else:
            instance_id = persisted_channel_instance_id(
                "feishu",
                self.channel_instance_id,
            )
            state = await self._connection_repo.consume_oauth_state(
                provider="feishu",
                channel_instance_id=instance_id,
                state=code,
            )
            if state is None:
                await self._send_connection_confirmation(
                    message_id=message_id,
                    chat_id=chat_id,
                    text="Feishu connection code is invalid or expired.",
                )
                return True
            await self._connection_repo.upsert_connection(
                owner_user_id=state["owner_user_id"],
                provider="feishu",
                channel_instance_id=instance_id,
                external_account_id=user_id,
                workspace_id=chat_id,
                metadata=metadata,
                status="connected",
            )
        await self._send_connection_confirmation(
            message_id=message_id,
            chat_id=chat_id,
            text="Feishu connected to ActWeave.",
        )
        return True

    def _on_message(self, event) -> None:
        """Called by lark-oapi when a message is received (runs in lark thread)."""
        try:
            logger.info("[Feishu] raw event received: type=%s", type(event).__name__)
            message = event.event.message
            chat_id = message.chat_id
            msg_id = message.message_id
            sender_id = event.event.sender.sender_id.open_id

            root_id = getattr(message, "root_id", None) or None
            chat_type = getattr(message, "chat_type", None)
            parent_id = self._non_empty_str(getattr(message, "parent_id", None))
            feishu_thread_id = self._non_empty_str(getattr(message, "thread_id", None))

            # Parse message content
            content = json.loads(message.content)

            # files_list store the any-file-key in feishu messages, which can be used to download the file content later
            # In Feishu channel, image_keys are independent of file_keys.
            # The file_key includes files, videos, and audio, but does not include stickers.
            files_list = []

            if "text" in content:
                # Handle plain text messages
                text = content["text"]
            elif "file_key" in content:
                file_key = content.get("file_key")
                if isinstance(file_key, str) and file_key:
                    files_list.append({"file_key": file_key})
                    text = "[file]"
                else:
                    text = ""
            elif "image_key" in content:
                image_key = content.get("image_key")
                if isinstance(image_key, str) and image_key:
                    files_list.append({"image_key": image_key})
                    text = "[image]"
                else:
                    text = ""
            elif "content" in content and isinstance(content["content"], list):
                # Handle rich-text messages with a top-level "content" list (e.g., topic groups/posts)
                text_paragraphs: list[str] = []
                for paragraph in content["content"]:
                    if isinstance(paragraph, list):
                        paragraph_text_parts: list[str] = []
                        for element in paragraph:
                            if isinstance(element, dict):
                                # Include both normal text and @ mentions
                                if element.get("tag") in ("text", "at"):
                                    text_value = element.get("text", "")
                                    if text_value:
                                        paragraph_text_parts.append(text_value)
                                elif element.get("tag") == "img":
                                    image_key = element.get("image_key")
                                    if isinstance(image_key, str) and image_key:
                                        files_list.append({"image_key": image_key})
                                        paragraph_text_parts.append("[image]")
                                elif element.get("tag") in ("file", "media"):
                                    file_key = element.get("file_key")
                                    if isinstance(file_key, str) and file_key:
                                        files_list.append({"file_key": file_key})
                                        paragraph_text_parts.append("[file]")
                        if paragraph_text_parts:
                            # Join text segments within a paragraph with spaces to avoid "helloworld"
                            text_paragraphs.append(" ".join(paragraph_text_parts))

                # Join paragraphs with blank lines to preserve paragraph boundaries
                text = "\n\n".join(text_paragraphs)
            else:
                text = ""
            text = text.strip()

            logger.info(
                "[Feishu] parsed message: chat_type=%s, text_len=%d",
                chat_type,
                len(text or ""),
            )

            if not (text or files_list):
                logger.info("[Feishu] empty text, ignoring message")
                return

            group_bind_command = parse_group_bind_command(text)
            if group_bind_command.matched:
                if self._main_loop and self._main_loop.is_running():
                    if group_bind_command.code:
                        bind_coro = self._bind_group_from_code(
                            message_id=msg_id,
                            chat_id=chat_id,
                            user_id=sender_id,
                            code=group_bind_command.code,
                            chat_type=chat_type,
                        )
                    else:
                        bind_coro = self._send_connection_confirmation(
                            message_id=msg_id,
                            chat_id=chat_id,
                            text=("The Feishu group connection command is incomplete or malformed. Copy the full command from Project Settings and send it again."),
                        )
                    fut = asyncio.run_coroutine_threadsafe(
                        bind_coro,
                        self._main_loop,
                    )
                    fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "bind_group", mid))
                else:
                    logger.warning("[Feishu] main loop not running, cannot bind project group")
                return

            connect_code = self._pending_connect_code(text)
            if connect_code:
                if self._main_loop and self._main_loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(
                        self._bind_connection_from_connect_code(
                            message_id=msg_id,
                            chat_id=chat_id,
                            user_id=sender_id,
                            code=connect_code,
                        ),
                        self._main_loop,
                    )
                    fut.add_done_callback(lambda f, mid=msg_id: self._log_future_error(f, "bind_connection", mid))
                else:
                    logger.warning("[Feishu] main loop not running, cannot bind channel connection")
                return

            # Only treat known slash commands as commands; absolute paths and
            # other slash-prefixed text should be handled as normal chat.
            if _is_feishu_command(text):
                msg_type = InboundMessageType.COMMAND
            else:
                msg_type = InboundMessageType.CHAT

            # The initial topic is provider-owned input only. After the exact
            # connection is resolved on the main event loop, _prepare_inbound
            # checks PostgreSQL aliases and may replace it with an existing
            # scoped topic before manager dispatch.
            topic_id = root_id or msg_id
            if chat_type == "p2p":
                topic_id = None
            resolved_from_pending = False
            has_explicit_topic = bool(root_id or parent_id or feishu_thread_id)
            if msg_type == InboundMessageType.CHAT and not is_removed_channel_command(text) and not has_explicit_topic:
                pending = self._consume_pending_clarification(chat_id, sender_id)
                pending_topic_id = self._non_empty_str(pending.get("topic_id")) if pending else None
                if pending_topic_id:
                    topic_id = pending_topic_id
                    resolved_from_pending = True

            inbound = self._make_inbound(
                chat_id=chat_id,
                user_id=sender_id,
                text=text,
                msg_type=msg_type,
                thread_ts=msg_id,
                provider_delivery_id=msg_id,
                files=files_list,
                metadata={
                    "message_id": msg_id,
                    "root_id": root_id,
                    "parent_id": parent_id,
                    "thread_id": feishu_thread_id,
                    "chat_type": chat_type,
                    "topic_id": topic_id,
                    "user_id": sender_id,
                    RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY: resolved_from_pending,
                },
            )
            inbound.topic_id = topic_id

            if self._is_batchable_file_inbound(
                msg_type=msg_type,
                text=text,
                files=files_list,
                root_id=root_id,
                parent_id=parent_id,
                thread_id=feishu_thread_id,
            ):
                self._queue_file_inbound_batch(msg_id, inbound)
                return

            self._schedule_prepare_inbound(msg_id, inbound)
        except Exception:
            logger.exception("[Feishu] error processing message")
