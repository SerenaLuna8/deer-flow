"""Lead-Agent ``knowledge_search`` tool bound to one Project's KnowledgeModule.

This is a Worker-side adapter: it depends only on the Knowledge Package
interface (:class:`KnowledgeModule`) and the Harness trusted-extension seam.
The Package never imports the Harness and the Harness never imports the
Package — this module is the only place the two meet for Agent Runs.

The model-visible signature stays ``knowledge_search(query, top_k=None)``;
omitting ``top_k`` defers to each base's configured default. The Project
identity is bound at factory-creation time from the Worker-issued Run
context, never from model arguments. Successful calls return a ``Command``
whose ToolMessage carries the model-readable ``items`` JSON as content and the
complete Knowledge Citations under
``additional_kwargs["knowledge_citations"]`` for host persistence.
"""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from actweave_knowledge import (
    KnowledgeCitation,
    KnowledgeError,
    KnowledgeMetadataFilter,
    KnowledgeModule,
    KnowledgeSearchRequest,
)
from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

KNOWLEDGE_SEARCH_TOOL_NAME = "knowledge_search"
KNOWLEDGE_CITATIONS_KEY = "knowledge_citations"


def _parsed_metadata_filters(raw: list[dict[str, Any]] | None) -> tuple[KnowledgeMetadataFilter, ...] | None:
    """Shape model-provided filter dicts into package DTOs.

    The tool schema already guarantees a list of dicts; name/operator/value
    rules live in the package validator, so incomplete model output surfaces
    as a KNOWLEDGE_INVALID_REQUEST error ToolMessage rather than a crash.
    """

    if raw is None:
        return None
    return tuple(
        KnowledgeMetadataFilter(
            name=item.get("name"),  # type: ignore[arg-type]  # validated by the package
            operator=item.get("operator"),  # type: ignore[arg-type]
            value=item.get("value"),  # type: ignore[arg-type]
        )
        for item in raw
    )


def _citation_payload(citation: KnowledgeCitation) -> dict[str, Any]:
    return {
        "knowledge_base_id": str(citation.knowledge_base_id),
        "knowledge_base_name": citation.knowledge_base_name,
        "document_id": str(citation.document_id),
        "document_name": citation.document_name,
        "segment_id": str(citation.segment_id),
        "segment_position": citation.segment_position,
        "snippet": citation.snippet,
        "score": citation.score,
        "source_position": dict(citation.source_position),
    }


def create_knowledge_search_tool(module: KnowledgeModule, project_id: UUID):
    """Build the ``knowledge_search`` tool for exactly one Project."""

    @tool(KNOWLEDGE_SEARCH_TOOL_NAME, parse_docstring=True)
    async def knowledge_search(
        query: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        top_k: int | None = None,
        metadata_filters: list[dict[str, Any]] | None = None,
    ) -> Command:
        """Search this project's knowledge bases and return the best matching passages.

        Use this when the user's question may be answered by documents the
        project has uploaded (manuals, reports, policies, notes). Quote or
        paraphrase the returned snippets and rely on their scores to judge
        relevance; do not fabricate passages that were not returned.

        Args:
            query: The question or key phrase to search for, in natural language.
            top_k: How many passages to return (1-20). Omit to use each
                knowledge base's configured default.
            metadata_filters: Optional document-metadata conditions, ANDed
                together, each shaped {"name": str, "operator": "eq" |
                "contains" | "gte" | "lte", "value": string or number}.
                Only use field names the project has defined; time fields
                compare as epoch seconds.
        """

        try:
            result = await module.search(
                KnowledgeSearchRequest(
                    project_id=project_id,
                    query=query,
                    top_k=top_k,
                    source="agent",
                    metadata_filters=_parsed_metadata_filters(metadata_filters),
                )
            )
        except KnowledgeError as error:
            # The model may keep answering after a failed search, but an error
            # must never turn into a fabricated citation: error ToolMessages
            # carry no knowledge_citations payload.
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            f"Error: {error.code}: {error.message}",
                            tool_call_id=tool_call_id,
                            status="error",
                        )
                    ]
                },
            )
        citations = [_citation_payload(citation) for citation in result.citations]
        items = [
            {
                "knowledge_base_name": payload["knowledge_base_name"],
                "document_name": payload["document_name"],
                "snippet": payload["snippet"],
                "score": payload["score"],
                "source_position": payload["source_position"],
            }
            for payload in citations
        ]
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        json.dumps({"items": items}, ensure_ascii=False),
                        tool_call_id=tool_call_id,
                        additional_kwargs={KNOWLEDGE_CITATIONS_KEY: citations},
                    )
                ]
            },
        )

    return knowledge_search


def create_knowledge_lead_agent_factory(
    *,
    module: KnowledgeModule,
    project_id: UUID,
    base_factory: Any | None = None,
):
    """Wrap the lead-agent factory so private chat Runs carry ``knowledge_search``.

    The wrapper preserves the complete keyword-only signature of
    ``_make_lead_agent_with_private_runtime`` (the Worker verifies parameter
    presence via ``inspect.signature``) and only pins ``trusted_extension``.
    Skill Builder Runs never reach this wrapper: the executor installs it for
    ordinary chat Runs only.
    """

    from deerflow.agents.lead_agent.agent import (
        TrustedLeadAgentExtension,
        _make_lead_agent_with_private_runtime,
        make_lead_agent,
    )

    extension = TrustedLeadAgentExtension(
        extra_tools=(create_knowledge_search_tool(module, project_id),),
    )
    base = base_factory if base_factory is not None else make_lead_agent
    # The wrapper below pins the canonical private-runtime path. Silently
    # discarding a custom one would be an invisible behavior change, so refuse.
    base_private = getattr(base, "private_runtime_factory", None)
    if base_private is not None and base_private is not _make_lead_agent_with_private_runtime:
        raise ValueError("knowledge lead-agent factory only wraps the canonical private-runtime factory; got a custom private_runtime_factory")

    def knowledge_lead_agent_factory(config):  # noqa: ANN001, ANN202 - mirrors make_lead_agent
        return base(config)

    def private_runtime_factory(
        *,
        config,  # noqa: ANN001
        private_runtime,  # noqa: ANN001
        app_config=None,  # noqa: ANN001
        trusted_extension=None,  # noqa: ANN001
        tool_call_control_profile=None,  # noqa: ANN001
        tool_call_control_scope_id=None,  # noqa: ANN001
        tool_call_control_observer=None,  # noqa: ANN001
        context_evidence_observer=None,  # noqa: ANN001
        resolved_max_concurrent_subagents=None,  # noqa: ANN001
        resolved_max_total_subagents=None,  # noqa: ANN001
    ):
        if trusted_extension is not None:
            raise ValueError("knowledge lead-agent factory owns the trusted extension; caller must not pass one")
        return _make_lead_agent_with_private_runtime(
            config=config,
            private_runtime=private_runtime,
            app_config=app_config,
            trusted_extension=extension,
            tool_call_control_profile=tool_call_control_profile,
            tool_call_control_scope_id=tool_call_control_scope_id,
            tool_call_control_observer=tool_call_control_observer,
            context_evidence_observer=context_evidence_observer,
            resolved_max_concurrent_subagents=resolved_max_concurrent_subagents,
            resolved_max_total_subagents=resolved_max_total_subagents,
        )

    knowledge_lead_agent_factory.private_runtime_factory = private_runtime_factory
    return knowledge_lead_agent_factory
