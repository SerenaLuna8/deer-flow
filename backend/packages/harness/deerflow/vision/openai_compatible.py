"""Narrow OpenAI-compatible transport for the versioned Vision Bridge.

This is intentionally not the generic chat-model adapter.  It accepts one
fixed HTTPS endpoint profile, one image, one prompt and one JSON Schema so a
model catalog entry cannot widen the request with arbitrary headers or bodies.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import math
import secrets
import time
import weakref
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Event, Lock
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from deerflow.config.model_config import ModelConfig
from deerflow.vision.contracts import (
    VisionEvidence,
    VisionInvocationResult,
    VisionUsageReceipt,
)
from deerflow.vision.prompt import VisionMode, render_vision_prompt_v1

MAX_VISION_REQUEST_BYTES: Final = 16 * 1024 * 1024
MAX_VISION_RESPONSE_BYTES: Final = 256 * 1024
MAX_VISION_OUTPUT_TOKENS: Final = 4_096
_MAX_RETRY_AFTER_SECONDS: Final = 1.0
_GLOBAL_MODEL_CONCURRENCY: Final = 4
_GLOBAL_MODEL_MAX_WAITERS: Final = 16
_RETRYABLE_STATUS_CODES: Final = frozenset({408, 429, 500, 502, 503, 504})

_gate_lock = Lock()
_loop_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _BoundedModelGate]] = weakref.WeakKeyDictionary()


class _BoundedModelGate:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(_GLOBAL_MODEL_CONCURRENCY)
        self._waiters = 0

    async def acquire(self) -> None:
        waiting = self._semaphore.locked()
        if waiting:
            if self._waiters >= _GLOBAL_MODEL_MAX_WAITERS:
                raise OpenAICompatibleVisionError("VISION_BUSY")
            self._waiters += 1
        try:
            await self._semaphore.acquire()
        finally:
            if waiting:
                self._waiters -= 1

    def release(self) -> None:
        self._semaphore.release()


class OpenAICompatibleVisionError(RuntimeError):
    """Stable, content-free provider error with dispatch evidence."""

    def __init__(
        self,
        code: str,
        *,
        request_dispatched: bool = False,
        call_count: int = 0,
    ) -> None:
        self.code = code
        self.request_dispatched = request_dispatched
        self.call_count = call_count
        super().__init__(code)


def _model_gate(key: str) -> _BoundedModelGate:
    loop = asyncio.get_running_loop()
    with _gate_lock:
        gates = _loop_gates.setdefault(loop, {})
        gate = gates.get(key)
        if gate is None:
            gate = _BoundedModelGate()
            gates[key] = gate
        return gate


def _validated_endpoint(
    base_url: object,
    resource: str = "chat/completions",
) -> str:
    if type(base_url) is not str:
        raise OpenAICompatibleVisionError("VISION_CONFIGURATION_ERROR")
    value = base_url.strip()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(character.isspace() for character in value)
        ):
            raise ValueError
        _ = parsed.port
    except (TypeError, ValueError):
        raise OpenAICompatibleVisionError("VISION_CONFIGURATION_ERROR") from None
    return f"{value.rstrip('/')}/{resource}"


def _api_key(model_config: ModelConfig) -> SecretStr:
    value = getattr(model_config, "api_key", None)
    if not isinstance(value, SecretStr) or not value.get_secret_value():
        raise OpenAICompatibleVisionError("VISION_CONFIGURATION_ERROR")
    return value


def _request_payload(
    *,
    model: str,
    image_bytes: bytes,
    mime_type: str,
    mode: VisionMode,
) -> bytes:
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": render_vision_prompt_v1(mode),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Analyze the attached image.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{encoded_image}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": MAX_VISION_OUTPUT_TOKENS,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "vision_evidence_v1",
                "strict": True,
                "schema": _strict_response_schema(),
            },
        },
    }
    body = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_VISION_REQUEST_BYTES:
        raise OpenAICompatibleVisionError("IMAGE_TOO_LARGE")
    return body


def _responses_request_payload(
    *,
    model: str,
    image_bytes: bytes,
    mime_type: str,
    mode: VisionMode,
) -> bytes:
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "instructions": render_vision_prompt_v1(mode),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Analyze the attached image.",
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime_type};base64,{encoded_image}",
                        "detail": "high",
                    },
                ],
            }
        ],
        "max_output_tokens": MAX_VISION_OUTPUT_TOKENS,
        "stream": False,
        # The bridge never requests provider-side persistence.  Provider data
        # handling still remains subject to the configured third party's terms.
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "vision_evidence_v1",
                "strict": True,
                "schema": _strict_response_schema(),
            }
        },
    }
    body = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_VISION_REQUEST_BYTES:
        raise OpenAICompatibleVisionError("IMAGE_TOO_LARGE")
    return body


def _strict_response_schema() -> dict[str, object]:
    """Project the canonical Pydantic schema to the strict OpenAI subset."""

    schema = copy.deepcopy(VisionEvidence.model_json_schema())

    def normalize(node: object) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                normalize(value)
        elif isinstance(node, list):
            for value in node:
                normalize(value)

    normalize(schema)
    return schema


async def _bounded_response_bytes(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_VISION_RESPONSE_BYTES:
                raise OpenAICompatibleVisionError(
                    "VISION_RESPONSE_TOO_LARGE",
                    request_dispatched=True,
                )
        except ValueError:
            raise OpenAICompatibleVisionError(
                "VISION_SCHEMA_MISMATCH",
                request_dispatched=True,
            ) from None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_VISION_RESPONSE_BYTES:
            raise OpenAICompatibleVisionError(
                "VISION_RESPONSE_TOO_LARGE",
                request_dispatched=True,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _status_error(status_code: int) -> str:
    if status_code in {401, 403}:
        return "VISION_AUTH_FAILED"
    if status_code == 429:
        return "VISION_RATE_LIMITED"
    if status_code == 413:
        return "IMAGE_TOO_LARGE"
    if status_code in {400, 404, 405, 409, 415, 422}:
        return "VISION_CONFIGURATION_ERROR"
    return "VISION_UNAVAILABLE"


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw is not None:
            try:
                value = float(raw)
                if math.isfinite(value) and value >= 0:
                    return min(value, _MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(raw)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    value = max(
                        0.0,
                        (retry_at - datetime.now(UTC)).total_seconds(),
                    )
                    return min(value, _MAX_RETRY_AFTER_SECONDS)
                except (TypeError, ValueError, OverflowError):
                    pass
    jitter = secrets.randbelow(101) / 1_000
    return min(0.15 * (attempt + 1) + jitter, _MAX_RETRY_AFTER_SECONDS)


def _parse_usage(value: object) -> tuple[int | None, int | None, bool]:
    if not isinstance(value, Mapping):
        return None, None, True
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0:
        return None, None, True
    return prompt, completion, False


def _parse_responses_usage(
    value: object,
) -> tuple[int | None, int | None, bool]:
    if not isinstance(value, Mapping):
        return None, None, True
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
        return None, None, True
    return input_tokens, output_tokens, False


def _parse_response(body: bytes) -> VisionInvocationResult:
    try:
        payload = json.loads(body)
        if not isinstance(payload, Mapping):
            raise ValueError
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ValueError
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter":
            raise OpenAICompatibleVisionError(
                "VISION_CONTENT_BLOCKED",
                request_dispatched=True,
            )
        if finish_reason != "stop":
            raise ValueError
        message = choice.get("message")
        if not isinstance(message, Mapping) or type(message.get("content")) is not str:
            raise ValueError
        evidence_payload = json.loads(message["content"])
        evidence = VisionEvidence.model_validate(evidence_payload)
        input_tokens, output_tokens, usage_unknown = _parse_usage(
            payload.get("usage"),
        )
        # Apply the canonical byte budget after validation too.  This catches a
        # syntactically valid object whose expanded Unicode representation is
        # larger than the ToolMessage contract.
        evidence.canonical_json()
        return VisionInvocationResult(
            evidence=evidence,
            usage_receipt=VisionUsageReceipt(
                call_count=1,
                request_dispatched=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_unknown=usage_unknown,
            ),
        )
    except OpenAICompatibleVisionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError):
        raise OpenAICompatibleVisionError(
            "VISION_SCHEMA_MISMATCH",
            request_dispatched=True,
        ) from None


def _parse_responses_response(body: bytes) -> VisionInvocationResult:
    try:
        payload = json.loads(body)
        if not isinstance(payload, Mapping):
            raise ValueError
        status = payload.get("status")
        if status != "completed":
            incomplete = payload.get("incomplete_details")
            error = payload.get("error")
            reason = incomplete.get("reason") if isinstance(incomplete, Mapping) else None
            error_code = error.get("code") if isinstance(error, Mapping) else None
            if reason == "content_filter" or error_code == "content_filter":
                raise OpenAICompatibleVisionError(
                    "VISION_CONTENT_BLOCKED",
                    request_dispatched=True,
                )
            raise ValueError
        if payload.get("error") is not None or payload.get("incomplete_details") is not None:
            raise ValueError
        output = payload.get("output")
        if not isinstance(output, list):
            raise ValueError
        messages: list[Mapping[str, object]] = []
        for item in output:
            if not isinstance(item, Mapping):
                raise ValueError
            item_type = item.get("type")
            if item_type == "reasoning":
                continue
            if item_type != "message":
                raise ValueError
            messages.append(item)
        if len(messages) != 1:
            raise ValueError
        message = messages[0]
        if message.get("role") != "assistant" or message.get("status") != "completed":
            raise ValueError
        content = message.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError
        part = content[0]
        if not isinstance(part, Mapping):
            raise ValueError
        if part.get("type") in {"refusal", "output_refusal"}:
            raise OpenAICompatibleVisionError(
                "VISION_CONTENT_BLOCKED",
                request_dispatched=True,
            )
        if part.get("type") != "output_text" or type(part.get("text")) is not str:
            raise ValueError
        evidence_payload = json.loads(part["text"])
        evidence = VisionEvidence.model_validate(evidence_payload)
        input_tokens, output_tokens, usage_unknown = _parse_responses_usage(
            payload.get("usage"),
        )
        evidence.canonical_json()
        return VisionInvocationResult(
            evidence=evidence,
            usage_receipt=VisionUsageReceipt(
                call_count=1,
                request_dispatched=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_unknown=usage_unknown,
            ),
        )
    except OpenAICompatibleVisionError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise OpenAICompatibleVisionError(
            "VISION_SCHEMA_MISMATCH",
            request_dispatched=True,
        ) from None


class OpenAICompatibleVisionEvidenceClient:
    """One exact, bounded ``/chat/completions`` Vision Bridge profile."""

    requires_external_dispatch: Final = True
    _endpoint_resource = "chat/completions"
    _request_builder = staticmethod(_request_payload)
    _response_parser = staticmethod(_parse_response)

    def __init__(
        self,
        model_config: ModelConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        transient_gate_key: str | None = None,
    ) -> None:
        self._endpoint = _validated_endpoint(
            getattr(model_config, "base_url", None),
            self._endpoint_resource,
        )
        self._api_key = _api_key(model_config)
        self._model = model_config.model
        if type(self._model) is not str or not self._model:
            raise OpenAICompatibleVisionError("VISION_CONFIGURATION_ERROR")
        version_id = model_config._system_model_config_version_id
        if version_id is None and (type(transient_gate_key) is not str or not transient_gate_key):
            raise OpenAICompatibleVisionError("VISION_CONFIGURATION_ERROR")
        self._gate_key = str(version_id) if version_id is not None else transient_gate_key
        self._run_gate = asyncio.Semaphore(1)
        self._transport = transport

    async def analyze(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        mode: VisionMode,
        deadline_monotonic: float,
        abort_signal: Event,
    ) -> VisionInvocationResult:
        if abort_signal.is_set():
            raise OpenAICompatibleVisionError("VISION_DEADLINE_EXCEEDED")
        body = self._request_builder(
            model=self._model,
            image_bytes=image_bytes,
            mime_type=mime_type,
            mode=mode,
        )
        global_gate = _model_gate(self._gate_key)
        acquired_run = False
        acquired_global = False
        dispatched = False
        dispatched_calls = 0
        try:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise OpenAICompatibleVisionError("VISION_DEADLINE_EXCEEDED")
            async with asyncio.timeout(remaining):
                await self._run_gate.acquire()
                acquired_run = True
                await global_gate.acquire()
                acquired_global = True

                async with httpx.AsyncClient(
                    transport=self._transport,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    for attempt in range(2):
                        if abort_signal.is_set():
                            raise OpenAICompatibleVisionError(
                                "VISION_DEADLINE_EXCEEDED",
                                request_dispatched=dispatched,
                            )
                        remaining = deadline_monotonic - time.monotonic()
                        if remaining <= 0:
                            raise OpenAICompatibleVisionError(
                                "VISION_DEADLINE_EXCEEDED",
                                request_dispatched=dispatched,
                            )
                        response: httpx.Response | None = None
                        try:
                            attempt_budget = remaining if attempt > 0 else max(0.1, remaining / 2)
                            async with asyncio.timeout(attempt_budget):
                                dispatched_calls += 1
                                dispatched = True
                                async with client.stream(
                                    "POST",
                                    self._endpoint,
                                    headers={
                                        "authorization": f"Bearer {self._api_key.get_secret_value()}",
                                        "content-type": "application/json",
                                        "accept": "application/json",
                                    },
                                    content=body,
                                    timeout=httpx.Timeout(attempt_budget),
                                ) as response:
                                    response_body = await _bounded_response_bytes(response)
                        except (httpx.TransportError, TimeoutError):
                            if attempt == 0:
                                delay = _retry_delay(None, attempt)
                                if time.monotonic() + delay >= deadline_monotonic:
                                    break
                                await asyncio.sleep(delay)
                                continue
                            raise OpenAICompatibleVisionError(
                                "VISION_UNAVAILABLE",
                                request_dispatched=dispatched,
                            ) from None
                        if response.status_code in _RETRYABLE_STATUS_CODES and attempt == 0:
                            delay = _retry_delay(response, attempt)
                            if time.monotonic() + delay >= deadline_monotonic:
                                break
                            await asyncio.sleep(delay)
                            continue
                        if response.status_code < 200 or response.status_code >= 300:
                            raise OpenAICompatibleVisionError(
                                _status_error(response.status_code),
                                request_dispatched=True,
                            )
                        content_type = response.headers.get("content-type", "")
                        if content_type.split(";", 1)[0].strip().lower() != "application/json":
                            raise OpenAICompatibleVisionError(
                                "VISION_SCHEMA_MISMATCH",
                                request_dispatched=True,
                            )
                        result = self._response_parser(response_body)
                        return replace(
                            result,
                            usage_receipt=replace(
                                result.usage_receipt,
                                call_count=dispatched_calls,
                            ),
                        )
                raise OpenAICompatibleVisionError(
                    "VISION_DEADLINE_EXCEEDED" if time.monotonic() >= deadline_monotonic else "VISION_UNAVAILABLE",
                    request_dispatched=dispatched,
                )
        except OpenAICompatibleVisionError as error:
            if dispatched_calls > 0 and error.call_count != dispatched_calls:
                raise OpenAICompatibleVisionError(
                    error.code,
                    request_dispatched=True,
                    call_count=dispatched_calls,
                ) from None
            raise
        except TimeoutError:
            raise OpenAICompatibleVisionError(
                "VISION_DEADLINE_EXCEEDED",
                request_dispatched=dispatched,
                call_count=dispatched_calls,
            ) from None
        finally:
            if acquired_global:
                global_gate.release()
            if acquired_run:
                self._run_gate.release()


class OpenAIResponsesVisionEvidenceClient(
    OpenAICompatibleVisionEvidenceClient,
):
    """Bounded multimodal executor for a selected model's Responses protocol."""

    _endpoint_resource = "responses"
    _request_builder = staticmethod(_responses_request_payload)
    _response_parser = staticmethod(_parse_responses_response)


__all__ = [
    "MAX_VISION_REQUEST_BYTES",
    "MAX_VISION_RESPONSE_BYTES",
    "OpenAICompatibleVisionError",
    "OpenAICompatibleVisionEvidenceClient",
    "OpenAIResponsesVisionEvidenceClient",
]
