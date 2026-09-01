"""Provider request-context measurement from frozen profiles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import BaseMessage, HumanMessage

from deerflow.agents.middlewares.dangling_tool_call_middleware import (
    project_dangling_tool_call_messages,
)
from deerflow.agents.middlewares.durable_context_middleware import (
    render_durable_context_messages,
)
from deerflow.agents.middlewares.input_sanitization_middleware import (
    project_input_sanitization_messages,
)
from deerflow.agents.middlewares.provider_request_cost_adapter import (
    PROVIDER_NON_ASCII_SAFETY_SUPPLEMENT_TOKENS_PER_BYTE,
    provider_visible_messages_payload,
)
from deerflow.agents.middlewares.provider_request_profile import (
    _MAX_CURRENT_UPLOAD_IMAGE_ALLOWANCE,
    _SERIALIZATION_FRAMING_UTF8_BYTES,
    _VISION_CONTEXT_HEADER_UTF8_BYTES,
    _VISION_CONTEXT_PER_IMAGE_UTF8_BYTES,
    PROVIDER_REQUEST_ERROR_CONTRACT,
    PROVIDER_REQUEST_ESTIMATOR_REVISION,
    ProviderRequestContextMeasurement,
    ProviderRequestProfile,
    ProviderRequestUsageUnsupported,
    _canonical_json,
    _canonicalize_tool_schema_facts,
    _component,
    _material_bytes,
    _non_ascii_utf8_bytes,
    _project_visual_blocks,
    _tool_schema_list_utf8_bytes,
    contains_visual_material,
)


def _state_ephemeral_material(state: Mapping[str, object]) -> tuple[BaseMessage, ...]:
    ledger = state.get("delegations")
    skills = state.get("skill_context")
    return render_durable_context_messages(
        state.get("summary_text") if isinstance(state.get("summary_text"), str) else None,
        list(ledger) if isinstance(ledger, list) else [],
        list(skills) if isinstance(skills, list) else [],
    )


def _todo_ephemeral_material(state: Mapping[str, object]) -> tuple[BaseMessage, ...]:
    todos = state.get("todos")
    if not isinstance(todos, list) or not todos:
        return ()
    # Lazy import avoids a module cycle: TodoMiddleware's state schema imports
    # ThreadState, while this module is also used by ThreadState serialization.
    from deerflow.agents.middlewares.todo_middleware import (
        render_todo_request_reserve,
    )

    return (
        HumanMessage(
            content=render_todo_request_reserve(todos),
            name="todo_request_reserve",
        ),
    )


def _slash_request_ephemeral_material(
    messages: Sequence[BaseMessage],
) -> tuple[BaseMessage, ...]:
    last_human = next(
        (message for message in reversed(messages) if isinstance(message, HumanMessage)),
        None,
    )
    if last_human is None or not isinstance(last_human.content, str):
        return ()
    if not last_human.content.lstrip().startswith("/"):
        return ()
    # Slash activation duplicates the remaining user request inside its hidden
    # wrapper. The largest installed Skill wrapper itself is frozen into the
    # profile's bounded overlay byte count at assembly.
    return (HumanMessage(content=last_human.content, name="slash_request_reserve"),)


def measure_profile_snapshot_context(
    snapshot: Mapping[str, object],
    state: Mapping[str, object] | None,
) -> ProviderRequestContextMeasurement:
    """Measure one checkpoint from the persisted immutable profile snapshot."""

    if snapshot.get("version") != 1 or snapshot.get("estimator_revision") != PROVIDER_REQUEST_ESTIMATOR_REVISION or snapshot.get("error_contract") != PROVIDER_REQUEST_ERROR_CONTRACT:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile revision is unavailable")
    if snapshot.get("supported") is not True:
        reason = snapshot.get("unsupported_reason")
        raise ProviderRequestUsageUnsupported(reason if isinstance(reason, str) else "provider request profile is unsupported")
    provider_adapter = snapshot.get("provider_adapter")
    if not isinstance(provider_adapter, str) or not provider_adapter:
        raise ProviderRequestUsageUnsupported(
            "provider_request_usage_unsupported: provider adapter is unavailable",
        )
    integer_fields = (
        "static_system_utf8_bytes",
        "full_tool_schema_utf8_bytes",
        "full_tool_count",
        "bounded_overlay_utf8_bytes",
        "bounded_overlay_message_count",
        "provider_fixed_overhead_tokens",
        "provider_per_message_overhead_tokens",
        "provider_per_tool_overhead_tokens",
    )
    if any(not isinstance(snapshot.get(field), int) or snapshot[field] < 0 for field in integer_fields):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile components are invalid")
    raw_tool_facts = snapshot.get("full_tool_schema_facts")
    try:
        tool_facts = _canonicalize_tool_schema_facts(raw_tool_facts if isinstance(raw_tool_facts, (list, tuple)) else ())
    except ValueError:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile tool facts are invalid") from None
    if len(tool_facts) != snapshot["full_tool_count"] or _tool_schema_list_utf8_bytes(tool_facts) != snapshot["full_tool_schema_utf8_bytes"]:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile tool facts do not match components")
    allowance_ratio = snapshot.get("error_allowance_ratio")
    if not isinstance(allowance_ratio, (int, float)) or not 0 <= allowance_ratio <= 1:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile allowance is invalid")
    current = state or {}
    raw_messages = current.get("messages")
    messages = list(raw_messages) if isinstance(raw_messages, list) else []
    if any(not isinstance(message, BaseMessage) for message in messages):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: checkpoint messages are invalid")
    raw_visual_declared = snapshot.get("visual_max_tokens_per_image")
    visual_declared = raw_visual_declared if isinstance(raw_visual_declared, int) and not isinstance(raw_visual_declared, bool) and raw_visual_declared > 0 else None
    if visual_declared is None and contains_visual_material(messages):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")
    viewed_images = current.get("viewed_images")
    viewed_image_count = len(viewed_images) if isinstance(viewed_images, Mapping) else 0
    if snapshot.get("supports_vision") is True and viewed_image_count and visual_declared is None:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")

    durable_messages = _state_ephemeral_material(current)
    todo_messages = _todo_ephemeral_material(current)
    slash_messages = _slash_request_ephemeral_material(messages)
    # InputSanitization is a temporary outer request transform, so checkpoint
    # state retains the original HumanMessages. Project that exact transform
    # for profile capacity/drift accounting while leaving Slash Skill and other
    # state-derived overlays anchored to the original state above.
    provider_messages = project_dangling_tool_call_messages(
        project_input_sanitization_messages(messages),
    )
    provider_message_payloads = list(
        provider_visible_messages_payload(
            provider_messages,
            provider_adapter=provider_adapter,
        )
    )
    state_visual_count = 0
    if visual_declared is not None:
        provider_message_payloads, state_visual_count = _project_visual_blocks(
            provider_message_payloads,
        )
    # A vision-declared profile reserves the ephemeral image-context message
    # that ViewImageMiddleware injects: every retained viewed_images entry plus
    # the bounded current-upload allowance, each at the declared per-image cost,
    # plus the bounded label text around the visual blocks.
    vision_active = snapshot.get("supports_vision") is True and visual_declared is not None
    vision_image_allowance = (viewed_image_count + _MAX_CURRENT_UPLOAD_IMAGE_ALLOWANCE) if vision_active else 0
    vision_overhead_tokens = (vision_image_allowance + state_visual_count) * (visual_declared or 0)
    vision_context_bytes = (_VISION_CONTEXT_HEADER_UTF8_BYTES + vision_image_allowance * _VISION_CONTEXT_PER_IMAGE_UTF8_BYTES) if vision_active else 0
    vision_message_count = 1 if vision_active else 0
    non_ascii_supplement_per_byte = PROVIDER_NON_ASCII_SAFETY_SUPPLEMENT_TOKENS_PER_BYTE.get(provider_adapter, 0.0)
    compressible_material = _canonical_json(provider_message_payloads)
    compressible_bytes = len(compressible_material)
    fixed_bytes = int(snapshot["static_system_utf8_bytes"]) + int(snapshot["full_tool_schema_utf8_bytes"]) + _SERIALIZATION_FRAMING_UTF8_BYTES
    rendered_materials = [
        _material_bytes(
            message,
            provider_adapter=provider_adapter,
        )
        for message in (*durable_messages, *todo_messages, *slash_messages)
    ]
    ephemeral_bytes = int(snapshot["bounded_overlay_utf8_bytes"]) + vision_context_bytes + sum(len(material) for material in rendered_materials)
    # Declared overlay and vision label allowances have no retained text, so
    # they count fully toward the non-ASCII supplement (conservative).
    ephemeral_non_ascii_bytes = int(snapshot["bounded_overlay_utf8_bytes"]) + vision_context_bytes + sum(_non_ascii_utf8_bytes(material) for material in rendered_materials)
    compressible = _component(
        material_bytes=compressible_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(len(provider_message_payloads) * int(snapshot["provider_per_message_overhead_tokens"])),
        non_ascii_bytes=_non_ascii_utf8_bytes(compressible_material),
        non_ascii_supplement_per_byte=non_ascii_supplement_per_byte,
    )
    fixed = _component(
        material_bytes=fixed_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(int(snapshot["provider_fixed_overhead_tokens"]) + int(snapshot["full_tool_count"]) * int(snapshot["provider_per_tool_overhead_tokens"]) + int(snapshot["provider_per_message_overhead_tokens"])),
    )
    ephemeral = _component(
        material_bytes=ephemeral_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(
            ((len(durable_messages) + len(todo_messages) + len(slash_messages) + vision_message_count + int(snapshot["bounded_overlay_message_count"])) * int(snapshot["provider_per_message_overhead_tokens"])) + vision_overhead_tokens
        ),
        non_ascii_bytes=ephemeral_non_ascii_bytes,
        non_ascii_supplement_per_byte=non_ascii_supplement_per_byte,
    )
    components = {
        "compressible": compressible,
        "fixed": fixed,
        "ephemeral": ephemeral,
    }
    return ProviderRequestContextMeasurement(
        estimated_tokens=sum(item.estimated_tokens for item in components.values()),
        error_allowance_tokens=sum(item.error_allowance_tokens for item in components.values()),
        safety_bound_tokens=sum(item.safety_bound_tokens for item in components.values()),
        material_utf8_bytes=compressible_bytes + fixed_bytes + ephemeral_bytes,
        message_count=(len(provider_message_payloads) + len(durable_messages) + len(todo_messages) + len(slash_messages) + vision_message_count + int(snapshot["bounded_overlay_message_count"]) + 1),
        full_tool_count=int(snapshot["full_tool_count"]),
        components=components,
    )


def measure_profile_context(
    profile: ProviderRequestProfile,
    state: Mapping[str, object] | None,
) -> ProviderRequestContextMeasurement:
    """Measure the auto-trigger input from one in-memory frozen profile."""

    return measure_profile_snapshot_context(profile.snapshot(), state)
