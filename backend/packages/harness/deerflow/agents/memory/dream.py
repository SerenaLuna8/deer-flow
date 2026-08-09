"""Restricted Dream prompt, document contract, and ephemeral tool loop."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from importlib import resources
from typing import Protocol

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

MAX_MEMORY_DOCUMENT_CHARS = 16_000
MAX_DREAM_HISTORY_ITEMS = 20
MAX_DREAM_HISTORY_CHARS = 1_000
DREAM_PROMPT_VERSION = "dream-prompt-v4"
DEFAULT_DREAM_TIMEOUT_SECONDS = 120.0
DEFAULT_DREAM_MAX_ROUNDS = 8

DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES = (
    "用户偏好与协作方式",
    "项目背景",
    "长期约束与架构决策",
    "当前仍有效的目标",
)
MEMORY_DOCUMENT_SECTIONS = tuple(f"# {title}" for title in DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES)
EMPTY_MEMORY_DOCUMENT = "\n\n".join(MEMORY_DOCUMENT_SECTIONS)
MAX_MEMORY_DOCUMENT_SECTIONS = 8
MIN_MEMORY_DOCUMENT_SECTIONS = 2
MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS = 80
_DREAM_SECTIONS_PLACEHOLDER = "{{MEMORY_DOCUMENT_SECTIONS}}"

_CJK_RANGES = (
    ("\u3400", "\u4dbf"),
    ("\u4e00", "\u9fff"),
    ("\u3040", "\u30ff"),
    ("\uac00", "\ud7a3"),
)
_FORBIDDEN_HISTORY_MARKER = re.compile(
    r"\[H:\d+\]|\[(?:skip|correction|permanent|durable|ephemeral)\]",
    re.IGNORECASE,
)


def _load_prompt_template() -> str:
    return resources.files("deerflow.agents.memory").joinpath("prompts", "dream.md").read_text(encoding="utf-8").strip()


DREAM_PROMPT_TEMPLATE = _load_prompt_template()
if DREAM_PROMPT_TEMPLATE.count(_DREAM_SECTIONS_PLACEHOLDER) != 1:
    raise RuntimeError("Dream prompt sections placeholder contract is invalid")
logger = logging.getLogger(__name__)


class MemoryDocumentInvalid(ValueError):
    """The complete Dream document does not satisfy the fixed contract."""


class MemoryDocumentOverBudget(MemoryDocumentInvalid):
    """The complete Dream document exceeds its frozen token budget."""

    def __init__(
        self,
        *,
        estimated_tokens: int,
        limit_tokens: int,
        target_tokens: int,
        actual_characters: int,
        target_characters: int,
    ) -> None:
        self.estimated_tokens = estimated_tokens
        self.limit_tokens = limit_tokens
        self.target_tokens = target_tokens
        self.actual_characters = actual_characters
        self.target_characters = target_characters
        self.overage_tokens = estimated_tokens - limit_tokens
        self.reduction_tokens = estimated_tokens - target_tokens
        self.reduction_characters = actual_characters - target_characters
        character_guidance = f" Document chars {actual_characters}, target-character-limit {target_characters}"
        if self.reduction_characters > 0:
            character_guidance += f"; remove at least {self.reduction_characters} characters before retrying"
        super().__init__(
            "Memory document exceeds the token budget "
            f"(estimated {estimated_tokens}, limit {limit_tokens}, "
            f"overage {self.overage_tokens}). Target <= {target_tokens} estimated "
            "tokens (90% of limit); remove at least "
            f"{self.reduction_tokens} estimated tokens before retrying."
            f"{character_guidance}"
        )


class MemoryDreamError(RuntimeError):
    """Stable Dream execution failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_memory_document_sections(sections: object) -> tuple[str, ...]:
    """Validate one immutable ordered list of plain section titles."""

    if not isinstance(sections, (tuple, list)) or not MIN_MEMORY_DOCUMENT_SECTIONS <= len(sections) <= MAX_MEMORY_DOCUMENT_SECTIONS:
        raise MemoryDocumentInvalid("Memory document sections are invalid")
    validated: list[str] = []
    for title in sections:
        if (
            not isinstance(title, str)
            or title != title.strip()
            or not title
            or len(title) > MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS
            or title.startswith("#")
            or len(title.splitlines()) != 1
            or any((category := unicodedata.category(character)).startswith("C") or category in {"Zl", "Zp"} for character in title)
            or _FORBIDDEN_HISTORY_MARKER.search(title) is not None
        ):
            raise MemoryDocumentInvalid("Memory document sections are invalid")
        validated.append(title)
    if len(validated) != len(set(validated)):
        raise MemoryDocumentInvalid("Memory document sections are invalid")
    return tuple(validated)


def _render_section_headings(sections: object, *, separator: str) -> str:
    return separator.join(f"# {title}" for title in validate_memory_document_sections(sections))


def render_empty_memory_document(sections: object) -> str:
    """Render the empty complete document for one frozen section contract."""

    return _render_section_headings(sections, separator="\n\n")


def render_dream_prompt(sections: object) -> str:
    """Render exactly one frozen section list into the versioned prompt."""

    return DREAM_PROMPT_TEMPLATE.replace(
        _DREAM_SECTIONS_PLACEHOLDER,
        _render_section_headings(sections, separator="\n"),
    )


DREAM_PROMPT = render_dream_prompt(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES)


def estimate_memory_tokens(content: str) -> int:
    """Return a deterministic, offline and conservative token estimate."""

    if not isinstance(content, str):
        raise TypeError("Memory document must be text")
    cjk = sum(1 for character in content if any(start <= character <= end for start, end in _CJK_RANGES))
    non_cjk = len(content) - cjk
    return cjk + ((non_cjk + 3) // 4)


def _validate_memory_document_structure(
    content: str,
    *,
    sections: object = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
) -> str:
    if not isinstance(content, str):
        raise MemoryDocumentInvalid("Memory document must be text")
    if not content or len(content) > MAX_MEMORY_DOCUMENT_CHARS:
        raise MemoryDocumentInvalid("Memory document exceeds the character contract")
    headings = tuple(f"# {title}" for title in validate_memory_document_sections(sections))
    top_level = tuple(line for line in content.splitlines() if line.startswith("# "))
    if top_level != headings:
        raise MemoryDocumentInvalid("Memory document sections are invalid")
    if _FORBIDDEN_HISTORY_MARKER.search(content) is not None:
        raise MemoryDocumentInvalid("Memory document contains a history marker")
    return content


def validate_memory_document(
    content: str,
    max_tokens: int,
    *,
    sections: object = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
) -> str:
    """Validate a complete Memory document without normalizing or truncating it."""

    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("Memory token budget must be positive")
    _validate_memory_document_structure(content, sections=sections)
    estimated_tokens = estimate_memory_tokens(content)
    if estimated_tokens > max_tokens:
        target_tokens = _target_token_limit(max_tokens)
        target_characters = _target_character_limit(max_tokens)
        raise MemoryDocumentOverBudget(
            estimated_tokens=estimated_tokens,
            limit_tokens=max_tokens,
            target_tokens=target_tokens,
            actual_characters=len(content),
            target_characters=target_characters,
        )
    return content


def _target_token_limit(max_tokens: int) -> int:
    return (max_tokens * 9) // 10


def _target_character_limit(max_tokens: int) -> int:
    return min(MAX_MEMORY_DOCUMENT_CHARS, _target_token_limit(max_tokens))


def _revision_instruction(
    *,
    target_tokens: int,
    target_characters: int,
    same_rejected_draft: bool,
) -> str:
    if same_rejected_draft:
        prefix = "This is the same rejected draft; it was not saved. Rewrite the complete memory document"
    else:
        prefix = "The rejected draft was not saved. Rewrite the complete memory document"
    return (
        f"{prefix} before calling replace_memory_document again. Do not "
        "resubmit the same content. Keep the rewrite at or below "
        f"target-token-limit {target_tokens} estimated tokens and "
        f"target-character-limit {target_characters} characters by removing "
        "lower-priority, stale, superseded, or duplicate facts."
    )


def _fresh_regeneration_instruction(
    *,
    over_budget_count: int,
    target_tokens: int,
    target_characters: int,
) -> str:
    return (
        f"Fresh-regeneration phase entered after {over_budget_count} consecutive "
        "over-budget drafts. None of the rejected drafts were saved, and they are "
        "not included in this context. Start again from the exact frozen dream "
        "input above. Call read_memory_document before editing, then call "
        "replace_memory_document with one complete document at or below "
        f"target-token-limit {target_tokens} estimated tokens and "
        f"target-character-limit {target_characters} characters. Do not reuse or "
        "reconstruct a prior rejected draft; prune lower-priority, stale, "
        "superseded, or duplicate facts."
    )


@dataclass(frozen=True, slots=True)
class DreamHistoryInput:
    sequence: int
    tagged_text: str
    origin: str = "snip"

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1 or not isinstance(self.tagged_text, str) or not self.tagged_text or len(self.tagged_text) > MAX_DREAM_HISTORY_CHARS:
            raise ValueError("Dream history input is invalid")
        if self.origin not in {"snip", "tool"}:
            raise ValueError("Dream history origin is invalid")


@dataclass(frozen=True, slots=True)
class MemoryDreamInput:
    document: str
    document_version: int
    history: tuple[DreamHistoryInput, ...]
    max_tokens: int
    sections: tuple[str, ...] = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES
    # Only the server-decided `budget_rewrite` trigger may freeze an empty
    # batch; every other Dream must consume 1..20 history entries.
    budget_rewrite: bool = False

    def __post_init__(self) -> None:
        if type(self.document_version) is not int or self.document_version < 0:
            raise ValueError("Dream document version is invalid")
        if type(self.budget_rewrite) is not bool:
            raise ValueError("Dream budget flag is invalid")
        if self.budget_rewrite:
            if self.history:
                raise ValueError("Dream budget rewrite must not carry history")
        elif not 1 <= len(self.history) <= MAX_DREAM_HISTORY_ITEMS:
            raise ValueError("Dream history batch is invalid")
        if any(current.sequence >= following.sequence for current, following in zip(self.history, self.history[1:], strict=False)):
            raise ValueError("Dream history must be strictly ordered")
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("Memory token budget must be positive")
        validated_sections = validate_memory_document_sections(self.sections)
        object.__setattr__(self, "sections", validated_sections)
        _validate_memory_document_structure(self.document, sections=validated_sections)


@dataclass(frozen=True, slots=True)
class MemoryDreamResult:
    content: str
    replaced: bool


def render_dream_input(value: MemoryDreamInput) -> str:
    """Render exact frozen data blocks; history text is never truncated."""

    if type(value) is not MemoryDreamInput:
        raise TypeError("MemoryDreamInput is required")
    if value.history:
        history = "\n\n".join((f"[H:{item.sequence}] (origin=tool)\n{item.tagged_text}" if item.origin == "tool" else f"[H:{item.sequence}]\n{item.tagged_text}") for item in value.history)
    else:
        history = "No new history entries. This is a budget rewrite: rewrite the current document to fit the target limits without inventing new facts."
    return "\n".join(
        (
            "<dream-input>",
            f"<document-version>{value.document_version}</document-version>",
            f"<character-limit>{MAX_MEMORY_DOCUMENT_CHARS}</character-limit>",
            f"<token-limit>{value.max_tokens}</token-limit>",
            f"<target-token-limit>{_target_token_limit(value.max_tokens)}</target-token-limit>",
            f"<target-character-limit>{_target_character_limit(value.max_tokens)}</target-character-limit>",
            "<document-sections>",
            _render_section_headings(value.sections, separator="\n"),
            "</document-sections>",
            "<current-memory>",
            value.document,
            "</current-memory>",
            "<conversation-history>",
            history,
            "</conversation-history>",
            "</dream-input>",
        )
    )


@dataclass(slots=True)
class _MemoryDraft:
    original: str
    max_tokens: int
    content: str
    sections: tuple[str, ...] = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES
    read: bool = False
    replaced: bool = False
    replacement_rejected: bool = False

    def __post_init__(self) -> None:
        self.sections = validate_memory_document_sections(self.sections)


def build_dream_tools(draft: _MemoryDraft) -> tuple[StructuredTool, StructuredTool]:
    """Build the only two tools an ephemeral Dream session may access."""

    if type(draft) is not _MemoryDraft:
        raise TypeError("Dream draft is required")

    async def read_memory_document() -> str:
        """Read the exact frozen private Memory document."""

        if draft.read:
            if draft.replacement_rejected:
                return "memory document already read; do not call read_memory_document again. Revise using the rejection feedback and call replace_memory_document with a complete valid document."
            return "memory document already read; do not call read_memory_document again. Call replace_memory_document with a complete valid document, or finish without tools only if no change is needed."
        draft.read = True
        return draft.content

    async def replace_memory_document(content: str) -> str:
        """Replace the complete in-memory draft after reading it."""

        if not draft.read:
            raise MemoryDocumentInvalid("Memory document must be read before replacement")
        draft.content = validate_memory_document(
            content,
            draft.max_tokens,
            sections=draft.sections,
        )
        draft.replaced = True
        return "memory draft replaced"

    return (
        StructuredTool.from_function(
            coroutine=read_memory_document,
            name="read_memory_document",
            description="Read the exact frozen private Memory document.",
        ),
        StructuredTool.from_function(
            coroutine=replace_memory_document,
            name="replace_memory_document",
            description=(f"Replace the complete in-memory private Memory draft. The complete content must be at most {_target_character_limit(draft.max_tokens)} characters."),
        ),
    )


class _BoundDreamModel(Protocol):
    async def ainvoke(self, messages: list[object]) -> AIMessage: ...


class DreamModel(Protocol):
    def bind_tools(self, tools: list[BaseTool]) -> _BoundDreamModel: ...


class MemoryDreamRunner:
    """Run a bounded ephemeral model loop with no ambient Agent capabilities."""

    def __init__(
        self,
        model: DreamModel,
        *,
        timeout_seconds: float = DEFAULT_DREAM_TIMEOUT_SECONDS,
        max_rounds: int = DEFAULT_DREAM_MAX_ROUNDS,
    ) -> None:
        if not callable(getattr(model, "bind_tools", None)) or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0 or type(max_rounds) is not int or not 1 <= max_rounds <= 32:
            raise ValueError("Dream runner configuration is invalid")
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._max_rounds = max_rounds

    async def run(self, value: MemoryDreamInput) -> MemoryDreamResult:
        if type(value) is not MemoryDreamInput:
            raise TypeError("MemoryDreamInput is required")
        draft = _MemoryDraft(
            original=value.document,
            content=value.document,
            max_tokens=value.max_tokens,
            sections=value.sections,
        )
        tools = build_dream_tools(draft)
        by_name = {tool.name: tool for tool in tools}
        if set(by_name) != {"read_memory_document", "replace_memory_document"}:
            raise RuntimeError("Dream tool contract is invalid")
        bound = self._model.bind_tools(list(tools))
        frozen_input = render_dream_input(value)
        messages: list[object] = [
            SystemMessage(content=render_dream_prompt(value.sections)),
            HumanMessage(content=frozen_input),
        ]
        rejected_content_digests: set[bytes] = set()
        consecutive_over_budget = 0
        fresh_regeneration_used = False
        fresh_regeneration_requires_replace = False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                for round_number in range(1, self._max_rounds + 1):
                    response = await bound.ainvoke(messages)
                    if not isinstance(response, AIMessage):
                        raise MemoryDreamError("MEMORY_DREAM_MODEL_INVALID")
                    messages.append(response)
                    safe_tool_names = tuple(name if name in by_name else "<invalid>" for call in response.tool_calls for name in (call.get("name") if isinstance(call, dict) else None,))
                    logger.info(
                        "Dream round %d/%d tool_names=%s",
                        round_number,
                        self._max_rounds,
                        ",".join(safe_tool_names) or "none",
                    )
                    if not response.tool_calls:
                        if (draft.replacement_rejected or fresh_regeneration_requires_replace) and not draft.replaced:
                            messages.append(
                                HumanMessage(
                                    content=_revision_instruction(
                                        target_tokens=_target_token_limit(value.max_tokens),
                                        target_characters=_target_character_limit(value.max_tokens),
                                        same_rejected_draft=False,
                                    )
                                )
                            )
                            continue
                        if not draft.read:
                            raise MemoryDreamError("MEMORY_DREAM_READ_REQUIRED")
                        validate_memory_document(
                            draft.content,
                            value.max_tokens,
                            sections=value.sections,
                        )
                        return MemoryDreamResult(
                            content=draft.content,
                            replaced=draft.replaced,
                        )
                    accepted_replacement = False
                    revision_instructions: list[HumanMessage] = []
                    fresh_regeneration_count: int | None = None
                    for call in response.tool_calls:
                        name = call.get("name")
                        tool = by_name.get(name) if isinstance(name, str) else None
                        call_id = call.get("id")
                        arguments = call.get("args")
                        if tool is None or not isinstance(call_id, str) or not call_id or not isinstance(arguments, dict):
                            raise MemoryDreamError("MEMORY_DREAM_TOOL_INVALID")
                        try:
                            result = await tool.ainvoke(arguments)
                        except asyncio.CancelledError:
                            raise
                        except MemoryDocumentInvalid as error:
                            if isinstance(error, MemoryDocumentOverBudget):
                                consecutive_over_budget += 1
                                if consecutive_over_budget >= 2 and not fresh_regeneration_used and fresh_regeneration_count is None:
                                    fresh_regeneration_count = consecutive_over_budget
                            else:
                                consecutive_over_budget = 0
                            draft.replacement_rejected = True
                            rejected_content = arguments.get("content")
                            same_rejected_draft = False
                            if isinstance(rejected_content, str):
                                rejected_digest = hashlib.sha256(rejected_content.encode("utf-8")).digest()
                                same_rejected_draft = rejected_digest in rejected_content_digests
                                rejected_content_digests.add(rejected_digest)
                            logger.info(
                                "Dream round %d/%d rejected %s: %s; same_rejected_draft=%s",
                                round_number,
                                self._max_rounds,
                                name,
                                error,
                                same_rejected_draft,
                            )
                            feedback_prefix = "memory draft rejected: "
                            if same_rejected_draft:
                                feedback_prefix += "same rejected draft; it was not saved. "
                            messages.append(
                                ToolMessage(
                                    content=(f"{feedback_prefix}{error}. Submit a valid complete document by removing lower-priority, stale, superseded, or duplicate facts, then call replace_memory_document again."),
                                    tool_call_id=call_id,
                                    name=name,
                                )
                            )
                            revision_instructions.append(
                                HumanMessage(
                                    content=_revision_instruction(
                                        target_tokens=_target_token_limit(value.max_tokens),
                                        target_characters=_target_character_limit(value.max_tokens),
                                        same_rejected_draft=same_rejected_draft,
                                    )
                                )
                            )
                            continue
                        except Exception:
                            raise MemoryDreamError("MEMORY_DREAM_TOOL_FAILED") from None
                        messages.append(
                            ToolMessage(
                                content=str(result),
                                tool_call_id=call_id,
                                name=name,
                            )
                        )
                        accepted_replacement = accepted_replacement or name == "replace_memory_document"
                    if accepted_replacement:
                        return MemoryDreamResult(
                            content=draft.content,
                            replaced=True,
                        )
                    if fresh_regeneration_count is not None:
                        fresh_regeneration_used = True
                        fresh_regeneration_requires_replace = True
                        consecutive_over_budget = 0
                        draft.content = draft.original
                        draft.read = False
                        draft.replaced = False
                        draft.replacement_rejected = False
                        messages = [
                            SystemMessage(content=render_dream_prompt(value.sections)),
                            HumanMessage(content=frozen_input),
                            HumanMessage(
                                content=_fresh_regeneration_instruction(
                                    over_budget_count=fresh_regeneration_count,
                                    target_tokens=_target_token_limit(value.max_tokens),
                                    target_characters=_target_character_limit(value.max_tokens),
                                )
                            ),
                        ]
                        logger.info(
                            "Dream entering fresh-regeneration phase: over_budget_count=%d target_tokens=%d target_characters=%d",
                            fresh_regeneration_count,
                            _target_token_limit(value.max_tokens),
                            _target_character_limit(value.max_tokens),
                        )
                        continue
                    messages.extend(revision_instructions)
        except TimeoutError:
            raise MemoryDreamError("MEMORY_DREAM_TIMEOUT") from None
        raise MemoryDreamError("MEMORY_DREAM_ROUND_LIMIT")


__all__ = [
    "DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES",
    "DEFAULT_DREAM_MAX_ROUNDS",
    "DEFAULT_DREAM_TIMEOUT_SECONDS",
    "DREAM_PROMPT",
    "DREAM_PROMPT_TEMPLATE",
    "DREAM_PROMPT_VERSION",
    "DreamHistoryInput",
    "EMPTY_MEMORY_DOCUMENT",
    "MAX_DREAM_HISTORY_CHARS",
    "MAX_DREAM_HISTORY_ITEMS",
    "MAX_MEMORY_DOCUMENT_CHARS",
    "MEMORY_DOCUMENT_SECTIONS",
    "MemoryDocumentInvalid",
    "MemoryDocumentOverBudget",
    "MemoryDreamError",
    "MemoryDreamInput",
    "MemoryDreamResult",
    "MemoryDreamRunner",
    "build_dream_tools",
    "estimate_memory_tokens",
    "render_dream_input",
    "render_dream_prompt",
    "render_empty_memory_document",
    "validate_memory_document",
    "validate_memory_document_sections",
]
