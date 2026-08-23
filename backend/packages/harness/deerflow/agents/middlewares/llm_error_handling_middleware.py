"""LLM error handling middleware with retry/backoff and user-facing fallbacks."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Any, override

from httpx import HTTPStatusError
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

from deerflow.config.app_config import AppConfig
from deerflow.error_codes import (
    CURRENT_UPLOAD_FAILURE_DETAIL,
    PublicRunError,
)
from deerflow.public_error_codes import (
    llm_error_code_for_reason,
    normalize_llm_error_reason,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.recovered_llm_failures import (
    RecoveredLLMCaller,
    RecoveredLLMFailure,
    RecoveredLLMFailureSubtype,
    RunRecoveredLLMFailureRecorder,
    build_recovered_llm_failures_receipt,
    read_recovered_llm_failures,
)

logger = logging.getLogger(__name__)

_RETRIABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_BUSY_PATTERNS = (
    "server busy",
    "temporarily unavailable",
    "try again later",
    "please retry",
    "please try again",
    "overloaded",
    "high demand",
    "rate limit",
    "负载较高",
    "服务繁忙",
    "稍后重试",
    "请稍后重试",
)
_QUOTA_PATTERNS = (
    "insufficient_quota",
    "quota",
    "billing",
    "credit",
    "payment",
    "余额不足",
    "超出限额",
    "额度不足",
    "欠费",
)
_AUTH_PATTERNS = (
    "autherror",
    "auth_error",
    "authentication",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "permission_denied",
    "forbidden",
    "access denied",
    "access_denied",
    "无权",
    "未授权",
)
_PROVIDER_MODULE_PREFIXES = (
    "anthropic",
    "cohere",
    "google.api_core",
    "groq",
    "mistralai",
    "openai",
    "together",
)
_PROVIDER_AUTH_EXCEPTION_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "Unauthenticated",
        "UnauthorizedError",
    }
)
_TRANSIENT_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "InternalServerError",
        "NetworkError",
        "PoolTimeout",
        "ProxyError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "StreamChunkTimeoutError",
        "TimeoutException",
        "TransportError",
        "WriteError",
        "WriteTimeout",
    }
)
_TRANSPORT_EXCEPTION_NAMES = _TRANSIENT_EXCEPTION_NAMES - {
    "InternalServerError",
    "StreamChunkTimeoutError",
}
_TIMEOUT_EXCEPTION_NAMES = frozenset(
    {
        "APITimeoutError",
        "ConnectTimeout",
        "PoolTimeout",
        "ReadTimeout",
        "StreamChunkTimeoutError",
        "TimeoutException",
        "WriteTimeout",
    }
)
_CONNECTION_EXCEPTION_NAMES = _TRANSPORT_EXCEPTION_NAMES - _TIMEOUT_EXCEPTION_NAMES
_SAFE_CLASSIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")

# Per-exception retry budget overrides.
#
# Some transient errors are retriable in principle but expensive to retry at
# the default budget. StreamChunkTimeoutError in particular fires after the
# upstream provider has already stalled for `stream_chunk_timeout` seconds
# (typically 120-240s); a full 3-attempt loop can therefore stack 6-12 minutes
# of dead air before surfacing the failure to the user. We keep exactly one
# retry (cheap reconnect that catches genuine transient TCP blips) and then
# fail fast — the same buffered payload is overwhelmingly likely to fail
# again at the upstream provider for the same reason.
#
# Keys are exception class *names* (not classes) so we don't introduce
# import-time coupling on optional dependencies like langchain-openai. The
# value is the absolute max attempt count, NOT additional retries — so a
# value of 2 means "1 first attempt + 1 retry" (the CR-requested
# "keep one retry" behavior).
_RETRY_BUDGET_OVERRIDES: dict[str, int] = {
    "StreamChunkTimeoutError": 2,
}

# Exception class names that indicate the upstream stream-chunk watchdog
# fired because the model stalled mid-flight. These deserve a more specific
# user-facing message than the generic "temporarily unavailable" copy,
# because the typical root cause is a long tool-call serialization stalling
# the upstream stream — and the most actionable advice we can give the user
# is "ask for a shorter / split output" rather than "wait and retry".
# Generic connection drops (httpx RemoteProtocolError / ReadError) are
# intentionally excluded: they routinely fire on transient network blips
# with normal payloads, where the "split the work" guidance is misleading.
_STREAM_DROP_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "StreamChunkTimeoutError",
    }
)


class LLMErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """Retry transient LLM errors and surface graceful assistant messages."""

    retry_max_attempts: int = 3
    retry_base_delay_ms: int = 1000
    retry_cap_delay_ms: int = 8000

    def __init__(
        self,
        *,
        app_config: AppConfig,
        retry_jitter_source: Callable[[], float] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self.circuit_failure_threshold = app_config.circuit_breaker.failure_threshold
        self.circuit_recovery_timeout_sec = app_config.circuit_breaker.recovery_timeout_sec

        # Circuit Breaker state
        self._circuit_lock = threading.Lock()
        self._circuit_failure_count = 0
        self._circuit_open_until = 0.0
        self._circuit_state = "closed"
        self._circuit_probe_in_flight = False
        self._retry_jitter_source = retry_jitter_source or random.random

    def _max_attempts_for(self, exc: BaseException) -> int:
        """Return the effective max attempt count for this exception.

        Falls back to `self.retry_max_attempts` unless the exception class name
        appears in the per-exception override table.
        """
        override = _RETRY_BUDGET_OVERRIDES.get(type(exc).__name__)
        if override is None:
            return self.retry_max_attempts

        return min(override, self.retry_max_attempts)

    def _check_circuit(self) -> bool:
        """Returns True if circuit is OPEN (fast fail), False otherwise."""
        with self._circuit_lock:
            now = time.time()

            if self._circuit_state == "open":
                if now < self._circuit_open_until:
                    return True
                self._circuit_state = "half_open"
                self._circuit_probe_in_flight = False

            if self._circuit_state == "half_open":
                if self._circuit_probe_in_flight:
                    return True
                self._circuit_probe_in_flight = True
                return False

            return False

    def _record_success(self) -> None:
        with self._circuit_lock:
            if self._circuit_state != "closed" or self._circuit_failure_count > 0:
                logger.info("Circuit breaker reset (Closed). LLM service recovered.")
            self._circuit_failure_count = 0
            self._circuit_open_until = 0.0
            self._circuit_state = "closed"
            self._circuit_probe_in_flight = False

    def _record_failure(self) -> None:
        with self._circuit_lock:
            if self._circuit_state == "half_open":
                self._circuit_open_until = time.time() + self.circuit_recovery_timeout_sec
                self._circuit_state = "open"
                self._circuit_probe_in_flight = False
                logger.error(
                    "Circuit breaker probe failed (Open). Will probe again after %ds.",
                    self.circuit_recovery_timeout_sec,
                )
                return

            self._circuit_failure_count += 1
            if self._circuit_failure_count >= self.circuit_failure_threshold:
                self._circuit_open_until = time.time() + self.circuit_recovery_timeout_sec
                if self._circuit_state != "open":
                    self._circuit_state = "open"
                    self._circuit_probe_in_flight = False
                    logger.error(
                        "Circuit breaker tripped (Open). Threshold reached (%d). Will probe after %ds.",
                        self.circuit_failure_threshold,
                        self.circuit_recovery_timeout_sec,
                    )

    def _classify_error(self, exc: BaseException) -> tuple[bool, str]:
        status_code = _extract_status_code(exc)
        exception_names = _exception_type_names(exc)

        # Middleware that runs before the provider call can fail locally. Its
        # bounded error text must never be interpreted as a provider response
        # merely because it contains words such as "unauthorized".
        if _is_current_upload_error(exc):
            return False, "current_upload"

        # Proxy authentication is an egress transport failure, not evidence
        # that the model-owned API Key itself is invalid. Handle it before any
        # provider authentication classification.
        if status_code == 407 or exception_names & _TRANSPORT_EXCEPTION_NAMES:
            return True, "transient"

        provider_detail = " ".join(_extract_provider_error_values(exc)).lower()
        if _matches_any(provider_detail, _QUOTA_PATTERNS):
            return False, "quota"
        # A provider client can surface the raw httpx status exception instead
        # of a provider-SDK exception. Authenticate only
        # from that concrete structured response; arbitrary local exceptions
        # with status-like attributes or auth-looking text remain generic.
        if isinstance(exc, HTTPStatusError) and exc.response.status_code in {401, 403}:
            return False, "auth"
        if _is_provider_auth_exception(exc) or (_is_provider_exception(exc) and _matches_any(provider_detail, _AUTH_PATTERNS)):
            return False, "auth"

        if exception_names & _TRANSIENT_EXCEPTION_NAMES:
            return True, "transient"
        # Upstream sometimes returns ``200 OK`` with an empty
        # ``generations`` list (observed against Volces "coding" /
        # ark.cn-beijing.volces.com). ``langchain_core.language_models.
        # chat_models.ainvoke`` then crashes with
        # ``IndexError: list index out of range`` at
        # ``llm_result.generations[0][0].message``. That isn't really a
        # client bug — it's a transient upstream-payload glitch — so we
        # route it through the same retry/backoff path as other transient
        # provider failures rather than failing the whole run.
        if isinstance(exc, IndexError):
            return True, "transient"
        if status_code in _RETRIABLE_STATUS_CODES:
            return True, "transient"
        if _is_provider_exception(exc) and _matches_any(provider_detail, _BUSY_PATTERNS):
            return True, "busy"

        return False, "generic"

    def _build_retry_delay_ms(self, attempt: int, exc: BaseException) -> int:
        retry_after = _extract_retry_after_ms(exc)
        if retry_after is not None:
            return retry_after
        backoff = self.retry_base_delay_ms * (2 ** max(0, attempt - 1))
        bounded_backoff = min(backoff, self.retry_cap_delay_ms)
        lower_bound = bounded_backoff // 2
        sample = self._retry_jitter_source()
        if not isinstance(sample, (int, float)) or not 0 <= sample <= 1:
            sample = 0.5
        return lower_bound + int((bounded_backoff - lower_bound) * sample)

    def _build_retry_message(self, attempt: int, wait_ms: int, reason: str) -> str:
        seconds = max(1, round(wait_ms / 1000))
        reason_text = "provider is busy" if reason == "busy" else "provider request failed temporarily"
        return f"LLM request retry {attempt}/{self.retry_max_attempts}: {reason_text}. Retrying in {seconds}s."

    def _build_circuit_breaker_message(self) -> str:
        return "The configured LLM provider is currently unavailable due to continuous failures. Circuit breaker is engaged to protect the system. Please wait a moment before trying again."

    def _build_error_fallback_message(
        self,
        content: str,
        *,
        reason: str,
    ) -> AIMessage:
        safe_reason = normalize_llm_error_reason(reason)
        error_code = llm_error_code_for_reason(safe_reason)
        return AIMessage(
            content=content,
            additional_kwargs={
                "deerflow_error_fallback": True,
                "error_code": error_code,
                "error_type": error_code,
                "error_reason": safe_reason,
                "error_detail": error_code,
            },
        )

    def _build_user_message(self, exc: BaseException, reason: str) -> str:
        if _is_current_upload_error(exc):
            return "The current image attachment could not be securely read or validated. Please retry this Run; if the problem continues, remove and attach the image again."
        if reason == "quota":
            return "The configured LLM provider rejected the request because the account is out of quota, billing is unavailable, or usage is restricted. Please fix the provider account and try again."
        if reason == "auth":
            return "The configured LLM provider rejected the request because authentication or access is invalid. Please check the provider credentials and try again."
        if reason in {"busy", "transient"}:
            # Stream-drop failures (chunk-gap timeout, peer-closed connection,
            # raw read error) almost always point at a single oversized
            # tool-call payload — the model spent so long serializing JSON
            # arguments that the upstream provider buffered and the stream
            # gap exceeded `stream_chunk_timeout`. Surfacing this distinct
            # cause lets the user split or shorten their next request
            # instead of helplessly retrying the same prompt.
            if type(exc).__name__ in _STREAM_DROP_EXCEPTIONS:
                return (
                    "The model's streaming response was interrupted before it could "
                    "finish. This usually happens when a single response or tool call "
                    "is very large — please ask the assistant to split the work into "
                    "smaller steps, or shorten the requested output, and try again."
                )
            if _is_transport_or_proxy_error(exc):
                return "The configured LLM provider could not be reached because the model transport or proxy connection failed. Please check the Worker network configuration and try again."
            return "The configured LLM provider is temporarily unavailable after multiple retries. Please wait a moment and continue the conversation."
        return "The configured LLM provider could not complete the request. Please retry, or contact an administrator if the problem continues."

    def _build_user_fallback_message(self, exc: BaseException, reason: str) -> AIMessage:
        return self._build_error_fallback_message(
            self._build_user_message(exc, reason),
            reason=reason,
        )

    def _log_terminal_failure(
        self,
        *,
        attempt: int,
        exc: BaseException,
        reason: str,
    ) -> None:
        provider_error_code, provider_error_type = _extract_safe_provider_classifiers(exc)
        status_code = _extract_status_code(exc)
        exception_class = _safe_classifier(type(exc).__name__) or "Exception"
        logger.warning(
            "LLM call failed after %d attempt(s): error_code=%s exception_class=%s status_code=%s provider_error_code=%s provider_error_type=%s",
            attempt,
            llm_error_code_for_reason(reason),
            exception_class,
            status_code if status_code is not None else "none",
            provider_error_code or "none",
            provider_error_type or "none",
        )

    def _emit_retry_event(self, attempt: int, wait_ms: int, reason: str) -> None:
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            writer(
                {
                    "type": "llm_retry",
                    "attempt": attempt,
                    "max_attempts": self.retry_max_attempts,
                    "wait_ms": wait_ms,
                    "reason": reason,
                    "message": self._build_retry_message(attempt, wait_ms, reason),
                }
            )
        except Exception:
            logger.debug("Failed to emit llm_retry event")

    @staticmethod
    def _record_recovered_failures(
        request: ModelRequest,
        failures: Sequence[RecoveredLLMFailure],
    ) -> tuple[RecoveredLLMFailure, ...]:
        """Return the authoritative Run aggregate when a recorder is present."""

        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        if not isinstance(context, Mapping):
            return tuple(failures)
        recorder = context.get(
            RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER,
        )
        if not isinstance(recorder, RunRecoveredLLMFailureRecorder):
            return tuple(failures)
        try:
            snapshot = (
                recorder.record(
                    build_recovered_llm_failures_receipt(failures),
                )
                if failures
                else recorder.snapshot()
            )
            parsed = read_recovered_llm_failures(
                build_recovered_llm_failures_receipt(snapshot),
            )
        except Exception:
            logger.debug(
                "Failed to record safe recovered LLM failure receipt",
            )
            return tuple(failures)
        return parsed if len(parsed) == len(snapshot) else tuple(failures)

    @staticmethod
    def _build_recovered_failure(
        request: ModelRequest,
        exc: BaseException,
        *,
        attempt: int,
        max_attempts: int,
        error_code: str,
        reason: str,
    ) -> RecoveredLLMFailure:
        context = getattr(getattr(request, "runtime", None), "context", None)
        caller: RecoveredLLMCaller = "subagent" if isinstance(context, Mapping) and context.get(RuntimeContextKeys.IS_SUBAGENT) is True else "lead_agent"
        failure_subtype, status_code = _safe_recovered_failure_subtype(
            exc,
            reason=reason,
        )
        return {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_code": error_code,
            "reason": normalize_llm_error_reason(reason),
            "caller": caller,
            "failure_subtype": failure_subtype,
            "status_code": status_code,
            "disposition": "recovered",
        }

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if self._check_circuit():
            return self._build_error_fallback_message(
                self._build_circuit_breaker_message(),
                reason="circuit_open",
            )

        attempt = 1
        recovered_failures: list[RecoveredLLMFailure] = []
        while True:
            try:
                response = handler(request)
                self._record_success()
                self._record_recovered_failures(
                    request,
                    recovered_failures,
                )
                return response
            except (GraphBubbleUp, PublicRunError):
                # Preserve LangGraph control-flow signals (interrupt/pause/resume).
                with self._circuit_lock:
                    if self._circuit_state == "half_open":
                        self._circuit_probe_in_flight = False
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)
                max_attempts = self._max_attempts_for(exc)
                if retriable and attempt < max_attempts:
                    wait_ms = self._build_retry_delay_ms(attempt, exc)
                    error_code = llm_error_code_for_reason(reason)
                    logger.warning(
                        "Transient LLM error on attempt %d/%d; retrying in %dms: error_code=%s",
                        attempt,
                        self.retry_max_attempts,
                        wait_ms,
                        error_code,
                    )
                    self._emit_retry_event(attempt, wait_ms, reason)
                    recovered_failures.append(
                        self._build_recovered_failure(
                            request,
                            exc,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            error_code=error_code,
                            reason=reason,
                        )
                    )
                    time.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                self._log_terminal_failure(
                    attempt=attempt,
                    exc=exc,
                    reason=reason,
                )
                if retriable:
                    self._record_failure()
                return self._build_user_fallback_message(exc, reason)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self._check_circuit():
            return self._build_error_fallback_message(
                self._build_circuit_breaker_message(),
                reason="circuit_open",
            )

        attempt = 1
        recovered_failures: list[RecoveredLLMFailure] = []
        while True:
            try:
                response = await handler(request)
                self._record_success()
                self._record_recovered_failures(
                    request,
                    recovered_failures,
                )
                return response
            except (GraphBubbleUp, PublicRunError):
                # Preserve LangGraph control-flow signals (interrupt/pause/resume).
                with self._circuit_lock:
                    if self._circuit_state == "half_open":
                        self._circuit_probe_in_flight = False
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)
                max_attempts = self._max_attempts_for(exc)
                if retriable and attempt < max_attempts:
                    wait_ms = self._build_retry_delay_ms(attempt, exc)
                    error_code = llm_error_code_for_reason(reason)
                    logger.warning(
                        "Transient LLM error on attempt %d/%d; retrying in %dms: error_code=%s",
                        attempt,
                        self.retry_max_attempts,
                        wait_ms,
                        error_code,
                    )
                    self._emit_retry_event(attempt, wait_ms, reason)
                    recovered_failures.append(
                        self._build_recovered_failure(
                            request,
                            exc,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            error_code=error_code,
                            reason=reason,
                        )
                    )
                    await asyncio.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                self._log_terminal_failure(
                    attempt=attempt,
                    exc=exc,
                    reason=reason,
                )
                if retriable:
                    self._record_failure()
                return self._build_user_fallback_message(exc, reason)


def _matches_any(detail: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in detail for pattern in patterns)


def _exception_type_names(exc: BaseException) -> frozenset[str]:
    return frozenset(cls.__name__ for cls in type(exc).__mro__)


def _is_provider_exception(exc: BaseException) -> bool:
    return any(cls.__module__ == prefix or cls.__module__.startswith(f"{prefix}.") for cls in type(exc).__mro__ for prefix in _PROVIDER_MODULE_PREFIXES)


def _is_provider_auth_exception(exc: BaseException) -> bool:
    return _is_provider_exception(exc) and bool(_exception_type_names(exc) & _PROVIDER_AUTH_EXCEPTION_NAMES)


def _is_current_upload_error(exc: BaseException) -> bool:
    return type(exc) is RuntimeError and str(exc).strip() == CURRENT_UPLOAD_FAILURE_DETAIL


def _is_transport_or_proxy_error(exc: BaseException) -> bool:
    return _extract_status_code(exc) == 407 or bool(_exception_type_names(exc) & _TRANSPORT_EXCEPTION_NAMES)


def _safe_classifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_CLASSIFIER_RE.fullmatch(candidate) is not None else None


def _classifier_from_mapping(payload: dict[Any, Any], key: str) -> str | None:
    value = _safe_classifier(payload.get(key))
    if value is not None:
        return value
    nested = payload.get("error")
    if isinstance(nested, dict):
        return _classifier_from_mapping(nested, key)
    return None


def _extract_safe_provider_classifiers(exc: BaseException) -> tuple[str | None, str | None]:
    """Extract log-safe provider code/type tokens, never body or message text."""

    if not _is_provider_exception(exc):
        return None, None

    code = _safe_classifier(getattr(exc, "code", None)) or _safe_classifier(getattr(exc, "error_code", None))
    error_type = _safe_classifier(getattr(exc, "type", None))
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        code = code or _classifier_from_mapping(body, "code")
        error_type = error_type or _classifier_from_mapping(body, "type")
    return code, error_type


def _error_values_from_mapping(payload: dict[Any, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("code", "type", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    nested = payload.get("error")
    if isinstance(nested, dict):
        values.extend(_error_values_from_mapping(nested))
    elif isinstance(nested, str) and nested.strip():
        values.append(nested.strip())
    return tuple(values)


def _extract_provider_error_values(exc: BaseException) -> tuple[str, ...]:
    """Return bounded provider-owned classifiers without trusting local text.

    Only exception objects from a supported provider SDK contribute their
    message/body. This prevents a local ``RuntimeError`` from impersonating a
    provider authentication, quota, or busy response through word choice.
    """

    if not _is_provider_exception(exc):
        return ()

    values: list[str] = []
    for attr in ("code", "error_code", "type"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        values.extend(_error_values_from_mapping(body))
    elif isinstance(body, str) and body.strip():
        values.append(body.strip())

    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        values.append(message.strip())
    detail = str(exc).strip()
    if detail:
        values.append(detail)
    return tuple(values)


def _extract_status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _safe_recovered_failure_subtype(
    exc: BaseException,
    *,
    reason: str,
) -> tuple[RecoveredLLMFailureSubtype, int | None]:
    """Return a closed diagnostic class without retaining exception content."""

    status_code = _extract_status_code(exc)
    if type(status_code) is int and 100 <= status_code <= 599:
        return "http_status", status_code
    exception_names = _exception_type_names(exc)
    if exception_names & _TIMEOUT_EXCEPTION_NAMES:
        return "timeout", None
    if exception_names & _CONNECTION_EXCEPTION_NAMES:
        return "connection", None
    if isinstance(exc, IndexError):
        return "empty_response", None
    if reason == "busy":
        return "provider_busy", None
    return "unknown", None


def _extract_retry_after_ms(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    raw = None
    header_name = ""
    for key in ("retry-after-ms", "Retry-After-Ms", "retry-after", "Retry-After"):
        header_name = key
        if hasattr(headers, "get"):
            raw = headers.get(key)
        if raw:
            break
    if not raw:
        return None

    try:
        multiplier = 1 if "ms" in header_name.lower() else 1000
        return max(0, int(float(raw) * multiplier))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            delta = target.timestamp() - time.time()
            return max(0, int(delta * 1000))
        except (TypeError, ValueError, OverflowError):
            return None


def _extract_error_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return exc.__class__.__name__
