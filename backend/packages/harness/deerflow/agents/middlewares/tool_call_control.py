"""Harness control for repeated calls and scoped tool-call accounting.

The graph construction seam is :class:`GraphToolCallControlTopology`. It hides
whether Lead and delegated graphs share one legacy Run counter or use separate
Lead-Run and Sub-Agent-Task counters. :func:`build_tool_call_control` remains
the low-level single-middleware seam for focused callers and tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired, Protocol, TypedDict, override
from uuid import UUID

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares._bounded_dict import BoundedDict
from deerflow.agents.middlewares.read_before_write_middleware import READ_MARK_KEY
from deerflow.agents.middlewares.tool_call_metadata import (
    clone_ai_message_with_tool_call_occurrences,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.runs.execution_contracts import RunSemanticStopRecorder

TOOL_CALL_CONTROL_STATE_KEY = "tool_call_control"
TOOL_CALL_CONTROL_RECEIPT_KEY = "deerflow_tool_call_control_receipt"
TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY = "__deerflow_tool_call_control_invocation_id"
TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY = "deerflow_tool_call_control_loop_replacement"
_STATE_VERSION = 2
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_LOOP_HARD_STOP_NOTICE = "[REPEATED TOOL CALL LIMIT] The same tool-call set reached the safety limit. A tool-free finalization turn will now summarize the evidence already collected."
_LOOP_FINALIZATION_PROMPT = (
    "The repeated-call safety guard stopped one tool-call batch because: "
    "{reason} Produce the user-facing answer now using only results already "
    "present in the conversation. Do not call tools. Clearly identify any "
    "incomplete work and the direct cause of every observed failure."
)

logger = logging.getLogger(__name__)

ToolCallControlRole = Literal["lead", "subagent"]
ToolCallControlWorkloadProfile = Literal["interactive", "research"]
ToolCallControlAccountingMode = Literal[
    "shared_run",
    "lead_run_subagent_task",
]
ToolCallBudgetScope = Literal["run", "lead", "subagent_task"]
ToolCallBudgetDisposition = Literal[
    "truncate_tool_calls",
    "exhaust_run",
    "exhaust_subject",
]
ToolCallControlReasonCode = Literal[
    "repeated_call_warning",
    "repeated_call_limit",
    "tool_budget_exhausted",
]
RepeatedCallReasonCode = Literal[
    "repeated_call_warning",
    "repeated_call_limit",
]
ToolCallBudgetReasonCode = Literal["tool_budget_exhausted",]
ToolCallControlStopReason = Literal["tool_budget_capped", "loop_capped"]


class ToolCallControlStateInvalid(RuntimeError):
    """Fail-closed signal for malformed or cross-scope control state."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"tool_call_control_state_invalid: {detail}")


class ToolCallControlLoopFinalizationFailed(RuntimeError):
    """Fail-closed signal when the required tool-free final turn is invalid."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"tool_call_control_loop_finalization_failed: {detail}")


@dataclass(frozen=True, slots=True)
class FixedToolCallControlScope:
    """One explicit execution scope shared across its Graph Turns."""

    scope_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope_id, str) or not self.scope_id.strip():
            raise ValueError("scope_id must be a non-empty string")

    def resolve(self, runtime: Runtime) -> str:
        del runtime
        return self.scope_id


@dataclass(frozen=True, slots=True)
class PerInvocationToolCallControlScope:
    """Resolve one SDK/Embedded invocation ID from an explicit context key."""

    def resolve(self, runtime: Runtime) -> str:
        context = getattr(runtime, "context", None)
        scope_id = context.get(TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY) if isinstance(context, Mapping) else None
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ToolCallControlStateInvalid("explicit invocation scope missing")
        return scope_id


ToolCallControlScope = FixedToolCallControlScope | PerInvocationToolCallControlScope


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _normalize_tool_call_args(raw_args: object) -> tuple[dict, str | None]:
    if isinstance(raw_args, dict):
        return raw_args, None
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, raw_args
        if isinstance(parsed, dict):
            return parsed, None
        return {}, json.dumps(parsed, sort_keys=True, default=str)
    if raw_args is None:
        return {}, None
    return {}, json.dumps(raw_args, sort_keys=True, default=str)


def _stable_tool_key(
    name: str,
    args: dict,
    fallback_key: str | None,
    *,
    read_mark: str | None = None,
) -> str:
    if name == "read_file" and fallback_key is None:
        path = args.get("path") or ""
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        try:
            start = int(start_line) if start_line is not None else 1
        except (TypeError, ValueError):
            start = 1
        try:
            end = int(end_line) if end_line is not None else start
        except (TypeError, ValueError):
            end = start
        start, end = sorted((start, end))
        return f"{path}:{(max(start, 1) - 1) // 200}-{(max(end, 1) - 1) // 200}:{read_mark or 'unmarked'}"
    if name in {"write_file", "str_replace", "upsert_candidate_file"}:
        if fallback_key is not None:
            return fallback_key
        return json.dumps(args, sort_keys=True, default=str)
    if fallback_key is not None:
        return fallback_key
    return json.dumps(args, sort_keys=True, default=str)


def _current_read_marks(messages: list[object]) -> dict[str, str]:
    marks: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, ToolMessage) or message.status == "error" or message.name != "read_file":
            continue
        mark = (message.additional_kwargs or {}).get(READ_MARK_KEY)
        if not isinstance(mark, Mapping):
            continue
        path = mark.get("path")
        content_hash = mark.get("hash")
        if isinstance(path, str) and path and isinstance(content_hash, str) and _SHA256_HEX.fullmatch(content_hash) is not None:
            marks[posixpath.normpath(path)] = content_hash
    return marks


def _repeated_call_fingerprint(
    tool_calls: list[dict],
    *,
    read_marks: Mapping[str, str] | None = None,
) -> str:
    normalized: list[str] = []
    for call in tool_calls:
        name = call.get("name", "")
        args, fallback_key = _normalize_tool_call_args(call.get("args", {}))
        path = args.get("path") if fallback_key is None else None
        normalized_path = posixpath.normpath(path) if isinstance(path, str) and path else None
        key = _stable_tool_key(
            name,
            args,
            fallback_key,
            read_mark=(read_marks.get(normalized_path) if read_marks is not None and normalized_path is not None else None),
        )
        normalized.append(f"{name}:{key}")
    normalized.sort()
    payload = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolCallLimit:
    """Resolved warning and hard limits for repeated-call detection."""

    warn_threshold: int
    hard_limit: int

    def __post_init__(self) -> None:
        warn = _positive_int(self.warn_threshold, field="warn_threshold")
        hard = _positive_int(self.hard_limit, field="hard_limit")
        if hard < warn:
            raise ValueError("hard_limit must be greater than or equal to warn_threshold")


@dataclass(frozen=True, slots=True)
class RepeatedCallPolicy:
    """Resolved identical-call-set loop policy."""

    warn_threshold: int
    hard_limit: int
    window_size: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a boolean")
        warn = _positive_int(self.warn_threshold, field="warn_threshold")
        hard = _positive_int(self.hard_limit, field="hard_limit")
        _positive_int(self.window_size, field="window_size")
        if hard < warn:
            raise ValueError("hard_limit must be greater than or equal to warn_threshold")


@dataclass(frozen=True, slots=True)
class ResolvedToolCallControlPolicy:
    """One immutable policy for one ToolCallControl accounting subject."""

    repeated_calls: RepeatedCallPolicy
    internal_tool_call_limit: int = 200

    def __post_init__(self) -> None:
        if not isinstance(self.repeated_calls, RepeatedCallPolicy):
            raise TypeError("repeated_calls must be a RepeatedCallPolicy")
        _positive_int(
            self.internal_tool_call_limit,
            field="internal_tool_call_limit",
        )


@dataclass(frozen=True, slots=True)
class ResolvedGraphToolCallControlProfile:
    """Resolved Lead/Sub-Agent policies and their accounting topology."""

    workload_profile: ToolCallControlWorkloadProfile
    accounting_mode: ToolCallControlAccountingMode
    lead: ResolvedToolCallControlPolicy
    subagent: ResolvedToolCallControlPolicy

    def __post_init__(self) -> None:
        if self.workload_profile not in {"interactive", "research"}:
            raise ValueError(
                "workload_profile must be 'interactive' or 'research'",
            )
        if self.accounting_mode not in {
            "shared_run",
            "lead_run_subagent_task",
        }:
            raise ValueError(
                "accounting_mode must be 'shared_run' or 'lead_run_subagent_task'",
            )
        if not isinstance(self.lead, ResolvedToolCallControlPolicy):
            raise TypeError("lead must be a ResolvedToolCallControlPolicy")
        if not isinstance(self.subagent, ResolvedToolCallControlPolicy):
            raise TypeError("subagent must be a ResolvedToolCallControlPolicy")
        if self.accounting_mode == "shared_run" and self.lead != self.subagent:
            raise ValueError(
                "shared_run requires identical Lead and Sub-Agent policies",
            )


def default_graph_tool_call_control_profile(
    workload_profile: ToolCallControlWorkloadProfile = "interactive",
    *,
    repeated_calls_enabled: bool = True,
) -> ResolvedGraphToolCallControlProfile:
    """Return caller-owned SDK/Embedded defaults for new invocations.

    This helper does not select a Private Run workload and does not read
    ``AppConfig``.  Server-admitted Runs must pass their already materialized
    :class:`ResolvedGraphToolCallControlProfile` instead.
    """

    if workload_profile not in {"interactive", "research"}:
        raise ValueError("workload_profile must be 'interactive' or 'research'")
    repeated_calls = RepeatedCallPolicy(
        enabled=repeated_calls_enabled,
        warn_threshold=3,
        hard_limit=20,
        window_size=20,
    )

    return ResolvedGraphToolCallControlProfile(
        workload_profile=workload_profile,
        accounting_mode="lead_run_subagent_task",
        lead=ResolvedToolCallControlPolicy(
            repeated_calls=repeated_calls,
            internal_tool_call_limit=200,
        ),
        subagent=ResolvedToolCallControlPolicy(
            repeated_calls=repeated_calls,
            internal_tool_call_limit=50,
        ),
    )


class ToolCallControlObserver(Protocol):
    """Owner-loop adapter for safe control observations."""

    def observe(self, observation: ToolCallControlObservation) -> None: ...


@dataclass(frozen=True, slots=True)
class RunToolCallLimitReservation:
    """One atomic prefix decision from a scoped tool-call counter."""

    count_before: int
    proposed: int
    admitted: int
    rejected: int
    count_after: int
    admitted_indices: tuple[int, ...]
    first_exhaustion: bool


@dataclass(slots=True)
class _RunToolCallLimitFacts:
    count: int
    reservations: dict[str, tuple[int, RunToolCallLimitReservation]]


class RunToolCallLimitAuthority:
    """Thread-safe counters keyed by an exact top-level Run/invocation scope."""

    def __init__(self, *, hard_limit: int, max_scopes: int = 1000) -> None:
        self.hard_limit = _positive_int(hard_limit, field="hard_limit")
        self._lock = threading.Lock()
        self._scopes: BoundedDict[str, _RunToolCallLimitFacts] = BoundedDict(_positive_int(max_scopes, field="max_scopes"))

    def reserve_batch(
        self,
        *,
        scope_id: str,
        proposal_receipt: str,
        proposed: int,
        baseline: int,
    ) -> RunToolCallLimitReservation:
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("scope_id must be a non-empty string")
        if not isinstance(proposal_receipt, str) or not proposal_receipt:
            raise ValueError("proposal_receipt must be a non-empty string")
        if not isinstance(proposed, int) or isinstance(proposed, bool) or proposed < 0:
            raise ValueError("proposed must be a non-negative integer")
        if not isinstance(baseline, int) or isinstance(baseline, bool) or not 0 <= baseline <= self.hard_limit:
            raise ValueError("baseline must be within the hard limit")
        with self._lock:
            facts = self._scopes.get(scope_id)
            if facts is None:
                facts = _RunToolCallLimitFacts(count=baseline, reservations={})
                self._scopes[scope_id] = facts
            elif baseline > facts.count:
                facts.count = baseline
            replay = facts.reservations.get(proposal_receipt)
            if replay is not None:
                replay_proposed, reservation = replay
                if replay_proposed != proposed:
                    raise ToolCallControlStateInvalid(
                        "proposal receipt changed its batch size",
                    )
                return reservation
            before = facts.count
            admitted = min(proposed, self.hard_limit - before)
            after = before + admitted
            reservation = RunToolCallLimitReservation(
                count_before=before,
                proposed=proposed,
                admitted=admitted,
                rejected=proposed - admitted,
                count_after=after,
                admitted_indices=tuple(range(admitted)),
                first_exhaustion=(before < self.hard_limit <= after),
            )
            facts.count = after
            facts.reservations[proposal_receipt] = (proposed, reservation)
            return reservation

    def count(self, *, scope_id: str, baseline: int = 0) -> int:
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("scope_id must be a non-empty string")
        if not isinstance(baseline, int) or isinstance(baseline, bool) or not 0 <= baseline <= self.hard_limit:
            raise ValueError("baseline must be within the hard limit")
        with self._lock:
            facts = self._scopes.get(scope_id)
            if facts is None:
                self._scopes[scope_id] = _RunToolCallLimitFacts(
                    count=baseline,
                    reservations={},
                )
                return baseline
            if baseline > facts.count:
                facts.count = baseline
            return facts.count


@dataclass(frozen=True, slots=True)
class ToolCallControlBinding:
    """Exact caller-owned scope for one middleware instance."""

    role: ToolCallControlRole
    scope: ToolCallControlScope
    workload_profile: ToolCallControlWorkloadProfile = "interactive"
    observer: ToolCallControlObserver | None = None
    limit_authority: RunToolCallLimitAuthority | None = None
    limit_scope: ToolCallControlScope | None = None
    budget_scope: ToolCallBudgetScope = "run"

    def __post_init__(self) -> None:
        if self.role not in {"lead", "subagent"}:
            raise ValueError("role must be 'lead' or 'subagent'")
        if not isinstance(
            self.scope,
            (FixedToolCallControlScope, PerInvocationToolCallControlScope),
        ):
            raise TypeError("scope must be an explicit tool-call control strategy")
        if self.workload_profile not in {"interactive", "research"}:
            raise ValueError("workload_profile must be 'interactive' or 'research'")
        if self.limit_authority is not None and not isinstance(
            self.limit_authority,
            RunToolCallLimitAuthority,
        ):
            raise TypeError("limit_authority must be RunToolCallLimitAuthority")
        if self.limit_scope is not None and not isinstance(
            self.limit_scope,
            (FixedToolCallControlScope, PerInvocationToolCallControlScope),
        ):
            raise TypeError("limit_scope must be an explicit scope strategy")
        if self.budget_scope not in {"run", "lead", "subagent_task"}:
            raise ValueError(
                "budget_scope must be 'run', 'lead', or 'subagent_task'",
            )
        if self.budget_scope == "lead" and self.role != "lead":
            raise ValueError("lead budget scope requires the Lead role")
        if self.budget_scope == "subagent_task" and self.role != "subagent":
            raise ValueError(
                "subagent_task budget scope requires the Sub-Agent role",
            )


@dataclass(frozen=True, slots=True)
class RepeatedCallObservation:
    """Argument-free repeated-call fact emitted at a loop threshold."""

    reason_code: RepeatedCallReasonCode
    role: ToolCallControlRole
    scope_id: str
    workload_profile: str
    count_before: int
    proposed: int
    admitted: int
    rejected: int
    count_after: int
    warn_threshold: int
    hard_limit: int
    disposition: str
    observation_id: str


@dataclass(frozen=True, slots=True)
class ToolCallBudgetObservation:
    """Argument-free fact emitted when one accounting limit is reached."""

    reason_code: ToolCallBudgetReasonCode
    role: ToolCallControlRole
    scope_id: str
    budget_scope: ToolCallBudgetScope
    workload_profile: str
    count_before: int
    proposed: int
    admitted: int
    rejected: int
    count_after: int
    hard_limit: int
    disposition: ToolCallBudgetDisposition
    observation_id: str


ToolCallControlObservation = RepeatedCallObservation | ToolCallBudgetObservation


class _ControlFacts(TypedDict):
    version: int
    scope_id: str
    contract_fingerprint: str
    admitted_count: int
    limit_exhausted: bool
    pending_notices: list[str]
    proposal_receipts: dict[str, list[int]]
    proposal_signatures: dict[str, str]
    controlled_proposal_signatures: dict[str, str]
    recent_fingerprints: list[str]
    warned_fingerprints: list[str]
    loop_finalization: dict[str, str] | None


class ToolCallControlState(AgentState):
    tool_call_control: NotRequired[Annotated[_ControlFacts | None, PrivateStateAttr]]


class ToolCallControl(AgentMiddleware[ToolCallControlState]):
    """Apply one resolved tool-call policy to complete model proposal batches."""

    state_schema = ToolCallControlState

    def __init__(
        self,
        policy: ResolvedToolCallControlPolicy,
        binding: ToolCallControlBinding,
    ) -> None:
        super().__init__()
        self._policy = policy
        self._binding = binding
        self._limit_authority = binding.limit_authority or RunToolCallLimitAuthority(
            hard_limit=policy.internal_tool_call_limit,
        )
        if self._limit_authority.hard_limit != policy.internal_tool_call_limit:
            raise ValueError("limit authority and policy hard limits must match")
        self._limit_scope = binding.limit_scope or binding.scope
        self._stop_reason_lock = threading.Lock()
        self._stop_reasons: BoundedDict[str, ToolCallControlStopReason] = BoundedDict(1000)
        scope_contract: dict[str, str] = {
            "strategy": type(binding.scope).__name__,
        }
        contract = {
            "role": binding.role,
            "scope": scope_contract,
            "workload_profile": binding.workload_profile,
            "repeated_calls": {
                "enabled": policy.repeated_calls.enabled,
                "warn_threshold": policy.repeated_calls.warn_threshold,
                "hard_limit": policy.repeated_calls.hard_limit,
                "window_size": policy.repeated_calls.window_size,
            },
            "internal_tool_call_limit": policy.internal_tool_call_limit,
        }
        # Runtime Policy v2-v5 checkpoints were fingerprinted before
        # ``budget_scope`` existed.  Preserve their exact shared-Run contract
        # bytes; only the new subject-accounting bindings extend the contract.
        if binding.budget_scope != "run":
            contract["budget_scope"] = binding.budget_scope
        self._contract_fingerprint = hashlib.sha256(
            json.dumps(
                contract,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _facts(
        self,
        state: AgentState,
        *,
        scope_id: str,
        allow_prior_contract: bool = False,
    ) -> _ControlFacts:
        raw = state.get(TOOL_CALL_CONTROL_STATE_KEY)
        if raw is None:
            return {
                "version": _STATE_VERSION,
                "scope_id": scope_id,
                "contract_fingerprint": self._contract_fingerprint,
                "admitted_count": 0,
                "limit_exhausted": False,
                "pending_notices": [],
                "proposal_receipts": {},
                "proposal_signatures": {},
                "controlled_proposal_signatures": {},
                "recent_fingerprints": [],
                "warned_fingerprints": [],
                "loop_finalization": None,
            }
        if not isinstance(raw, Mapping):
            raise ToolCallControlStateInvalid("state is not a mapping")
        if raw.get("version") != _STATE_VERSION:
            raise ToolCallControlStateInvalid("unsupported state version")
        if raw.get("scope_id") != scope_id:
            raise ToolCallControlStateInvalid("execution scope mismatch")
        raw_contract_fingerprint = raw.get("contract_fingerprint")
        if allow_prior_contract:
            if not isinstance(raw_contract_fingerprint, str) or _SHA256_HEX.fullmatch(raw_contract_fingerprint) is None:
                raise ToolCallControlStateInvalid("malformed contract fingerprint")
        elif raw_contract_fingerprint != self._contract_fingerprint:
            raise ToolCallControlStateInvalid("policy or binding mismatch")
        admitted_count = raw.get("admitted_count")
        limit_exhausted = raw.get("limit_exhausted")
        pending = raw.get("pending_notices")
        receipts = raw.get("proposal_receipts")
        signatures = raw.get("proposal_signatures")
        controlled_signatures = raw.get("controlled_proposal_signatures")
        fingerprints = raw.get("recent_fingerprints")
        warned_fingerprints = raw.get("warned_fingerprints")
        finalization = raw.get("loop_finalization")
        if (
            not isinstance(admitted_count, int)
            or isinstance(admitted_count, bool)
            or admitted_count < 0
            or admitted_count > self._policy.internal_tool_call_limit
            or not isinstance(limit_exhausted, bool)
            or not isinstance(pending, list)
            or not isinstance(receipts, Mapping)
            or not isinstance(signatures, Mapping)
            or not isinstance(controlled_signatures, Mapping)
            or not isinstance(fingerprints, list)
            or not isinstance(warned_fingerprints, list)
        ):
            raise ToolCallControlStateInvalid("malformed budget facts")
        if limit_exhausted is not (admitted_count >= self._policy.internal_tool_call_limit):
            raise ToolCallControlStateInvalid("limit exhaustion does not match count")
        if any(not isinstance(notice, str) or not notice for notice in pending):
            raise ToolCallControlStateInvalid("malformed pending notices")
        normalized_receipts: dict[str, list[int]] = {}
        for receipt, indices in receipts.items():
            if not isinstance(receipt, str) or len(receipt) != 64 or not isinstance(indices, list):
                raise ToolCallControlStateInvalid("malformed proposal receipt")
            if any(not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in indices):
                raise ToolCallControlStateInvalid("malformed proposal receipt")
            normalized_receipts[receipt] = list(dict.fromkeys(indices))
        normalized_signatures: dict[str, str] = {}
        for receipt, signature in signatures.items():
            if not isinstance(receipt, str) or not isinstance(signature, str) or len(signature) != 64:
                raise ToolCallControlStateInvalid("malformed proposal signature")
            normalized_signatures[receipt] = signature
        normalized_controlled_signatures: dict[str, str] = {}
        for receipt, signature in controlled_signatures.items():
            if not isinstance(receipt, str) or not isinstance(signature, str) or len(signature) != 64:
                raise ToolCallControlStateInvalid(
                    "malformed controlled proposal signature",
                )
            normalized_controlled_signatures[receipt] = signature
        if normalized_signatures.keys() != normalized_receipts.keys() or normalized_controlled_signatures.keys() != normalized_receipts.keys():
            raise ToolCallControlStateInvalid("proposal receipt/signature mismatch")
        if any(not isinstance(value, str) or not value for value in fingerprints) or any(not isinstance(value, str) or not value for value in warned_fingerprints):
            raise ToolCallControlStateInvalid("malformed repeated-call facts")
        normalized_finalization: dict[str, str] | None = None
        if finalization is not None:
            if not isinstance(finalization, Mapping) or finalization.get("phase") != "pending" or not isinstance(finalization.get("receipt"), str) or not isinstance(finalization.get("reason"), str):
                raise ToolCallControlStateInvalid("malformed loop finalization")
            normalized_finalization = {
                "phase": "pending",
                "receipt": finalization["receipt"],
                "reason": finalization["reason"],
            }
        return {
            "version": _STATE_VERSION,
            "scope_id": scope_id,
            "contract_fingerprint": raw_contract_fingerprint,
            "admitted_count": admitted_count,
            "limit_exhausted": limit_exhausted,
            "pending_notices": list(dict.fromkeys(pending)),
            "proposal_receipts": normalized_receipts,
            "proposal_signatures": normalized_signatures,
            "controlled_proposal_signatures": normalized_controlled_signatures,
            "recent_fingerprints": list(fingerprints),
            "warned_fingerprints": list(dict.fromkeys(warned_fingerprints)),
            "loop_finalization": normalized_finalization,
        }

    def _proposal_signature(
        self,
        message: AIMessage,
        *,
        scope_id: str,
    ) -> str:
        additional = dict(message.additional_kwargs or {})
        additional.pop(TOOL_CALL_CONTROL_RECEIPT_KEY, None)
        payload = {
            "scope_id": scope_id,
            "message_id": message.id,
            "tool_calls": list(message.tool_calls or []),
            "invalid_tool_calls": list(message.invalid_tool_calls or []),
            "raw_tool_calls": additional.get("tool_calls"),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _proposal_receipt(
        signature: str,
        *,
        message_index: int,
        collision_ordinal: int = 0,
    ) -> str:
        payload = f"{signature}:{message_index}"
        if collision_ordinal:
            payload = f"{payload}:{collision_ordinal}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _stamp_receipt(message: AIMessage, receipt: str) -> AIMessage:
        additional = dict(message.additional_kwargs or {})
        additional[TOOL_CALL_CONTROL_RECEIPT_KEY] = receipt
        return message.model_copy(update={"additional_kwargs": additional})

    def _record_loop_capped(
        self,
        runtime: Runtime,
        message: AIMessage,
        *,
        scope_id: str,
    ) -> None:
        context = getattr(runtime, "context", None)
        recorder = context.get(RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER) if isinstance(context, Mapping) else None
        message_id = message.id
        suppressed_id = message_id if isinstance(message_id, str) and message_id.strip() else None
        if self._binding.role == "lead" and isinstance(recorder, RunSemanticStopRecorder):
            recorder.record(
                "loop_capped",
                suppressed_ai_message_id=suppressed_id,
            )
            return
        self._record_stop_reason(scope_id, "loop_capped")

    def _record_stop_reason(
        self,
        scope_id: str,
        reason: ToolCallControlStopReason,
    ) -> None:
        """Keep the strongest additive Sub-Agent control receipt."""

        with self._stop_reason_lock:
            previous = self._stop_reasons.get(scope_id)
            if previous == "loop_capped":
                return
            if previous is None or reason == "loop_capped":
                self._stop_reasons[scope_id] = reason

    def consume_stop_reason(
        self,
        scope_id: str | None,
    ) -> ToolCallControlStopReason | None:
        """Consume the graph-local control receipt once."""

        if scope_id is None:
            return None
        with self._stop_reason_lock:
            return self._stop_reasons.pop(scope_id, None)

    @staticmethod
    def _has_visible_text(content: object) -> bool:
        if isinstance(content, str):
            return bool(content.strip())
        if not isinstance(content, list):
            return False
        return any(
            (isinstance(block, str) and bool(block.strip())) or (isinstance(block, Mapping) and str(block.get("type", "")).lower() in {"text", "output_text"} and isinstance(block.get("text"), str) and bool(block["text"].strip()))
            for block in content
        )

    @staticmethod
    def _loop_replacement(message: AIMessage, receipt: str) -> AIMessage:
        content = message.content
        if isinstance(content, list):
            content = [
                *content,
                {
                    "type": "text",
                    "text": f"\n\n{_LOOP_HARD_STOP_NOTICE}",
                },
            ]
        elif isinstance(content, str) and content:
            content = f"{content}\n\n{_LOOP_HARD_STOP_NOTICE}"
        else:
            content = _LOOP_HARD_STOP_NOTICE
        additional = dict(message.additional_kwargs or {})
        additional.pop("tool_calls", None)
        additional.pop("function_call", None)
        additional["hide_from_ui"] = True
        additional[TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY] = True
        additional[TOOL_CALL_CONTROL_RECEIPT_KEY] = receipt
        metadata = dict(message.response_metadata or {})
        if metadata.get("finish_reason") == "tool_calls":
            metadata["finish_reason"] = "stop"
        return message.model_copy(
            update={
                "content": content,
                "tool_calls": [],
                "invalid_tool_calls": [],
                "additional_kwargs": additional,
                "response_metadata": metadata,
            }
        )

    def _observation(
        self,
        *,
        reason_code: ToolCallControlReasonCode,
        count_before: int,
        proposed: int,
        admitted: int,
        rejected: int,
        count_after: int,
        limit: ToolCallLimit,
        disposition: str,
        proposal_receipt: str,
        scope_id: str,
    ) -> ToolCallControlObservation:
        identity = "|".join(
            (
                scope_id,
                proposal_receipt,
                reason_code,
                str(count_before),
                str(proposed),
                str(admitted),
                str(rejected),
                str(count_after),
            )
        )
        common = {
            "role": self._binding.role,
            "scope_id": scope_id,
            "workload_profile": self._binding.workload_profile,
            "count_before": count_before,
            "proposed": proposed,
            "admitted": admitted,
            "rejected": rejected,
            "count_after": count_after,
            "hard_limit": limit.hard_limit,
            "disposition": disposition,
            "observation_id": hashlib.sha256(identity.encode()).hexdigest(),
        }
        if reason_code in {"repeated_call_warning", "repeated_call_limit"}:
            return RepeatedCallObservation(
                reason_code=reason_code,
                warn_threshold=limit.warn_threshold,
                **common,
            )
        return ToolCallBudgetObservation(
            reason_code=reason_code,
            budget_scope=self._binding.budget_scope,
            **common,
        )

    def _budget_limit_notice(self, admitted_count: int) -> str:
        if self._binding.budget_scope == "lead":
            return f"[LEAD TOOL CALL LIMIT] The Lead Agent internal tool-call limit for this Run is exhausted at {admitted_count} admitted calls. Finish with the evidence already collected."
        if self._binding.budget_scope == "subagent_task":
            return f"[SUB-AGENT TASK TOOL CALL LIMIT] The internal tool-call limit for this Sub-Agent Task is exhausted at {admitted_count} admitted calls. Finish with the evidence already collected."
        return f"[RUN TOOL CALL LIMIT] The shared internal tool-call limit for this Run is exhausted at {admitted_count} admitted calls. Finish with the evidence already collected."

    def _budget_disposition(
        self,
        *,
        rejected: int,
    ) -> ToolCallBudgetDisposition:
        if rejected:
            return "truncate_tool_calls"
        if self._binding.budget_scope == "run":
            return "exhaust_run"
        return "exhaust_subject"

    def _observe(self, observation: ToolCallControlObservation) -> None:
        observer = self._binding.observer
        if observer is None:
            return
        try:
            observer.observe(observation)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Tool-call control observer failed",
                extra={
                    "reason_code": observation.reason_code,
                    "role": observation.role,
                    "tool_name": getattr(observation, "tool_name", None),
                },
                exc_info=True,
            )

    def _apply(
        self,
        state: ToolCallControlState,
        runtime: Runtime,
    ) -> dict | None:
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        scope_id = self._binding.scope.resolve(runtime)
        message = messages[-1]
        tool_calls = list(message.tool_calls or [])
        raw_facts = state.get(TOOL_CALL_CONTROL_STATE_KEY)
        if raw_facts is None and not tool_calls:
            return None
        facts = self._facts(state, scope_id=scope_id)
        limit_scope_id = self._limit_scope.resolve(runtime)
        budget_count = self._limit_authority.count(
            scope_id=limit_scope_id,
            baseline=facts["admitted_count"],
        )
        facts["admitted_count"] = budget_count
        facts["limit_exhausted"] = budget_count >= self._policy.internal_tool_call_limit
        signature = self._proposal_signature(
            message,
            scope_id=scope_id,
        )
        stamped_receipt = (message.additional_kwargs or {}).get(
            TOOL_CALL_CONTROL_RECEIPT_KEY,
        )
        receipt: str | None = None
        if isinstance(stamped_receipt, str) and stamped_receipt in facts["proposal_receipts"]:
            original_signature = facts["proposal_signatures"][stamped_receipt]
            controlled_signature = facts["controlled_proposal_signatures"][stamped_receipt]
            replay_indices = facts["proposal_receipts"][stamped_receipt]
            if signature == controlled_signature:
                update: dict = {
                    TOOL_CALL_CONTROL_STATE_KEY: facts,
                    "messages": [
                        self._stamp_receipt(message, stamped_receipt),
                    ],
                }
                if not replay_indices:
                    update["jump_to"] = "model"
                return update
            if signature != original_signature:
                raise ToolCallControlStateInvalid(
                    "stamped proposal signature mismatch",
                )
            receipt = stamped_receipt

        finalization = facts["loop_finalization"]
        if finalization is not None:
            if message.tool_calls or message.invalid_tool_calls:
                self._record_loop_capped(
                    runtime,
                    message,
                    scope_id=scope_id,
                )
                raise ToolCallControlLoopFinalizationFailed(
                    "model attempted another tool call",
                )
            if not self._has_visible_text(message.content):
                raise ToolCallControlLoopFinalizationFailed(
                    "model returned no visible answer",
                )
            facts["loop_finalization"] = None
            facts["pending_notices"] = []
            return {
                TOOL_CALL_CONTROL_STATE_KEY: facts,
                "jump_to": "end",
            }

        if not tool_calls:
            if not facts["pending_notices"]:
                return None
            facts["pending_notices"] = []
            return {TOOL_CALL_CONTROL_STATE_KEY: facts}

        if receipt is None:
            collision_ordinal = 0
            receipt = self._proposal_receipt(
                signature,
                message_index=len(messages) - 1,
            )
            while receipt in facts["proposal_receipts"]:
                collision_ordinal += 1
                receipt = self._proposal_receipt(
                    signature,
                    message_index=len(messages) - 1,
                    collision_ordinal=collision_ordinal,
                )
        replay_indices = facts["proposal_receipts"].get(receipt)
        if replay_indices is not None:
            if any(index >= len(tool_calls) for index in replay_indices):
                raise ToolCallControlStateInvalid(
                    "proposal receipt does not match batch",
                )
            replay_message = clone_ai_message_with_tool_call_occurrences(
                message,
                replay_indices,
            )
            update: dict = {
                TOOL_CALL_CONTROL_STATE_KEY: facts,
                "messages": [self._stamp_receipt(replay_message, receipt)],
            }
            if not replay_indices:
                update["jump_to"] = "model"
            return update

        repeated_notices: list[str] = []
        repeated_observations: list[ToolCallControlObservation] = []
        history = list(facts["recent_fingerprints"])
        warned_fingerprints = set(facts["warned_fingerprints"])
        repeated = self._policy.repeated_calls
        if repeated.enabled:
            fingerprint = _repeated_call_fingerprint(
                tool_calls,
                read_marks=_current_read_marks(list(messages)),
            )
            history.append(fingerprint)
            history = history[-repeated.window_size :]
            warned_fingerprints.intersection_update(history)
            repeat_count = history.count(fingerprint)
            repeated_limit = ToolCallLimit(
                warn_threshold=repeated.warn_threshold,
                hard_limit=repeated.hard_limit,
            )
            if repeat_count >= repeated.hard_limit:
                controlled_message = self._loop_replacement(message, receipt)
                controlled_signature = self._proposal_signature(
                    controlled_message,
                    scope_id=scope_id,
                )
                next_facts: _ControlFacts = {
                    **facts,
                    "pending_notices": [],
                    "proposal_receipts": {
                        **facts["proposal_receipts"],
                        receipt: [],
                    },
                    "proposal_signatures": {
                        **facts["proposal_signatures"],
                        receipt: signature,
                    },
                    "controlled_proposal_signatures": {
                        **facts["controlled_proposal_signatures"],
                        receipt: controlled_signature,
                    },
                    "recent_fingerprints": history,
                    "warned_fingerprints": sorted(warned_fingerprints),
                    "loop_finalization": {
                        "phase": "pending",
                        "receipt": receipt,
                        "reason": _LOOP_HARD_STOP_NOTICE,
                    },
                }
                observation = self._observation(
                    reason_code="repeated_call_limit",
                    count_before=repeat_count - 1,
                    proposed=1,
                    admitted=0,
                    rejected=1,
                    count_after=repeat_count,
                    limit=repeated_limit,
                    disposition="tool_free_finalization",
                    proposal_receipt=receipt,
                    scope_id=scope_id,
                )
                self._record_loop_capped(
                    runtime,
                    message,
                    scope_id=scope_id,
                )
                self._observe(observation)
                return {
                    TOOL_CALL_CONTROL_STATE_KEY: next_facts,
                    "messages": [controlled_message],
                    "jump_to": "model",
                }
            if repeat_count >= repeated.warn_threshold and fingerprint not in warned_fingerprints:
                warned_fingerprints.add(fingerprint)
                repeated_notices.append(f"[REPEATED TOOL CALL ADVISORY] The same tool-call set has appeared {repeat_count} times. Change strategy or arguments, or finish with the evidence already collected.")
                repeated_observations.append(
                    self._observation(
                        reason_code="repeated_call_warning",
                        count_before=repeat_count - 1,
                        proposed=1,
                        admitted=1,
                        rejected=0,
                        count_after=repeat_count,
                        limit=repeated_limit,
                        disposition="advisory",
                        proposal_receipt=receipt,
                        scope_id=scope_id,
                    )
                )

        pending_notices: list[str] = list(repeated_notices)
        observations: list[ToolCallControlObservation] = list(repeated_observations)
        reservation = self._limit_authority.reserve_batch(
            scope_id=limit_scope_id,
            proposal_receipt=receipt,
            proposed=len(tool_calls),
            baseline=facts["admitted_count"],
        )
        kept_indices = list(reservation.admitted_indices)
        if reservation.first_exhaustion or reservation.rejected:
            pending_notices.append(
                self._budget_limit_notice(reservation.count_after),
            )
            if self._binding.role == "subagent":
                self._record_stop_reason(scope_id, "tool_budget_capped")
            observations.append(
                self._observation(
                    reason_code="tool_budget_exhausted",
                    count_before=reservation.count_before,
                    proposed=reservation.proposed,
                    admitted=reservation.admitted,
                    rejected=reservation.rejected,
                    count_after=reservation.count_after,
                    limit=ToolCallLimit(
                        warn_threshold=self._policy.internal_tool_call_limit,
                        hard_limit=self._policy.internal_tool_call_limit,
                    ),
                    disposition=self._budget_disposition(
                        rejected=reservation.rejected,
                    ),
                    proposal_receipt=receipt,
                    scope_id=scope_id,
                )
            )

        controlled_message = clone_ai_message_with_tool_call_occurrences(
            message,
            kept_indices,
        )
        controlled_signature = self._proposal_signature(
            controlled_message,
            scope_id=scope_id,
        )
        next_facts: _ControlFacts = {
            "version": _STATE_VERSION,
            "scope_id": scope_id,
            "contract_fingerprint": self._contract_fingerprint,
            "admitted_count": reservation.count_after,
            "limit_exhausted": (reservation.count_after >= self._policy.internal_tool_call_limit),
            "pending_notices": list(dict.fromkeys(pending_notices)),
            "proposal_receipts": {
                **facts["proposal_receipts"],
                receipt: kept_indices,
            },
            "proposal_signatures": {
                **facts["proposal_signatures"],
                receipt: signature,
            },
            "controlled_proposal_signatures": {
                **facts["controlled_proposal_signatures"],
                receipt: controlled_signature,
            },
            "recent_fingerprints": history,
            "warned_fingerprints": sorted(warned_fingerprints),
            "loop_finalization": None,
        }
        update: dict = {TOOL_CALL_CONTROL_STATE_KEY: next_facts}
        update["messages"] = [self._stamp_receipt(controlled_message, receipt)]
        if reservation.rejected and not kept_indices:
            update["jump_to"] = "model"
        for observation in observations:
            self._observe(observation)
        return update

    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        raw = (request.state or {}).get(TOOL_CALL_CONTROL_STATE_KEY)
        if raw is None:
            return request
        scope_id = self._binding.scope.resolve(request.runtime)
        facts = self._facts(request.state or {}, scope_id=scope_id)
        limit_scope_id = self._limit_scope.resolve(request.runtime)
        budget_count = self._limit_authority.count(
            scope_id=limit_scope_id,
            baseline=facts["admitted_count"],
        )
        finalization = facts["loop_finalization"]
        if finalization is not None:
            return request.override(
                messages=[
                    *request.messages,
                    HumanMessage(
                        content=_LOOP_FINALIZATION_PROMPT.format(reason=finalization["reason"]),
                        name="tool_call_control_loop_finalization",
                        additional_kwargs={"hide_from_ui": True},
                    ),
                ],
                tools=[],
                tool_choice=None,
                response_format=None,
            )
        tools = [] if budget_count >= self._policy.internal_tool_call_limit else list(request.tools)
        messages = list(request.messages)
        if budget_count >= self._policy.internal_tool_call_limit and not facts["limit_exhausted"]:
            messages.append(
                HumanMessage(
                    content=self._budget_limit_notice(budget_count),
                    name="tool_call_control_advisory",
                )
            )
        if facts["pending_notices"]:
            messages.append(
                HumanMessage(
                    content="\n\n".join(facts["pending_notices"]),
                    name="tool_call_control_advisory",
                )
            )
        if tools == list(request.tools) and messages == list(request.messages):
            return request
        return request.override(messages=messages, tools=tools)

    @override
    def before_agent(
        self,
        state: ToolCallControlState,
        runtime: Runtime,
    ) -> dict | None:
        scope_id = self._binding.scope.resolve(runtime)
        raw = state.get(TOOL_CALL_CONTROL_STATE_KEY)
        if isinstance(raw, Mapping):
            prior_scope_id = raw.get("scope_id")
            if isinstance(prior_scope_id, str) and prior_scope_id and prior_scope_id != scope_id:
                # A cached Thread/graph may retain a previous Run or invocation's
                # private channel. Validate its version and structure before
                # replacing it, but do not compare it to the new Run's policy:
                # workload policy may legitimately change with the scope. The
                # same-scope path below still requires the exact fingerprint.
                self._facts(
                    state,
                    scope_id=prior_scope_id,
                    allow_prior_contract=True,
                )
                return {
                    TOOL_CALL_CONTROL_STATE_KEY: self._facts(
                        {},
                        scope_id=scope_id,
                    )
                }
        facts = self._facts(state, scope_id=scope_id)
        if raw is None:
            return {TOOL_CALL_CONTROL_STATE_KEY: facts}
        return None

    @override
    async def abefore_agent(
        self,
        state: ToolCallControlState,
        runtime: Runtime,
    ) -> dict | None:
        return self.before_agent(state, runtime)

    @hook_config(can_jump_to=["model", "end"])
    @override
    def after_model(
        self,
        state: ToolCallControlState,
        runtime: Runtime,
    ) -> dict | None:
        return self._apply(state, runtime)

    @hook_config(can_jump_to=["model", "end"])
    @override
    async def aafter_model(
        self,
        state: ToolCallControlState,
        runtime: Runtime,
    ) -> dict | None:
        return self._apply(state, runtime)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._augment_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._augment_request(request))


def build_tool_call_control(
    policy: ResolvedToolCallControlPolicy,
    binding: ToolCallControlBinding,
) -> AgentMiddleware:
    """Build the sole Agent Graph adapter for one resolved control policy."""

    if not isinstance(policy, ResolvedToolCallControlPolicy):
        raise TypeError("policy must be a ResolvedToolCallControlPolicy")
    if not isinstance(binding, ToolCallControlBinding):
        raise TypeError("binding must be a ToolCallControlBinding")
    return ToolCallControl(policy, binding)


class GraphToolCallControlTopology:
    """Resolve Lead and Sub-Agent Task accounting behind one small interface.

    Callers supply only the frozen graph profile and the top-level Run or SDK
    invocation scope.  This Module owns authority construction and chooses
    whether a delegated Task reuses the parent authority/scope or receives an
    execution-ID-scoped counter.  Lead and delegated graph builders therefore
    cannot accidentally implement different accounting topology rules.
    """

    __slots__ = (
        "_lead_authority",
        "_lead_scope",
        "_profile",
        "_subagent_authority",
    )

    def __init__(
        self,
        *,
        profile: ResolvedGraphToolCallControlProfile,
        lead_scope: ToolCallControlScope,
    ) -> None:
        if type(profile) is not ResolvedGraphToolCallControlProfile:
            raise TypeError(
                "profile must be a ResolvedGraphToolCallControlProfile",
            )
        if not isinstance(
            lead_scope,
            (FixedToolCallControlScope, PerInvocationToolCallControlScope),
        ):
            raise TypeError("lead_scope must be an explicit scope strategy")
        self._profile = profile
        self._lead_scope = lead_scope
        self._lead_authority = RunToolCallLimitAuthority(
            hard_limit=profile.lead.internal_tool_call_limit,
        )
        self._subagent_authority = (
            self._lead_authority
            if profile.accounting_mode == "shared_run"
            else RunToolCallLimitAuthority(
                hard_limit=profile.subagent.internal_tool_call_limit,
            )
        )

    @property
    def profile(self) -> ResolvedGraphToolCallControlProfile:
        """Return the immutable policy profile used by both graph roles."""

        return self._profile

    def build_lead(
        self,
        *,
        observer: ToolCallControlObserver | None = None,
    ) -> AgentMiddleware:
        """Build the sole control middleware for the Lead graph."""

        budget_scope: ToolCallBudgetScope = "run" if self._profile.accounting_mode == "shared_run" else "lead"
        return build_tool_call_control(
            self._profile.lead,
            ToolCallControlBinding(
                role="lead",
                scope=self._lead_scope,
                workload_profile=self._profile.workload_profile,
                observer=observer,
                limit_authority=self._lead_authority,
                limit_scope=self._lead_scope,
                budget_scope=budget_scope,
            ),
        )

    def bind_parent_scope(
        self,
        runtime: Runtime,
    ) -> GraphToolCallControlTopology:
        """Freeze the parent scope while retaining the same authorities.

        A delegated graph runs with a projected child context, so a dynamic
        SDK/Embedded invocation key must be resolved at the graph-local task
        Adapter seam.  The returned topology keeps both authority objects;
        legacy shared accounting therefore observes Lead reservations while
        the new mode continues to isolate each Task by execution ID.
        """

        resolved_scope = FixedToolCallControlScope(
            self._lead_scope.resolve(runtime),
        )
        bound = object.__new__(GraphToolCallControlTopology)
        bound._profile = self._profile
        bound._lead_scope = resolved_scope
        bound._lead_authority = self._lead_authority
        bound._subagent_authority = self._subagent_authority
        return bound

    def build_subagent_task(
        self,
        execution_id: UUID,
        *,
        observer: ToolCallControlObserver | None = None,
    ) -> AgentMiddleware:
        """Build one Task control keyed by its lifecycle-owned execution ID."""

        if not isinstance(execution_id, UUID):
            raise TypeError(
                "execution_id must be the lifecycle-owned UUID",
            )
        execution_scope = FixedToolCallControlScope(str(execution_id))
        shared_run = self._profile.accounting_mode == "shared_run"
        return build_tool_call_control(
            self._profile.subagent,
            ToolCallControlBinding(
                role="subagent",
                scope=execution_scope,
                workload_profile=self._profile.workload_profile,
                observer=observer,
                limit_authority=self._subagent_authority,
                limit_scope=(self._lead_scope if shared_run else execution_scope),
                budget_scope=("run" if shared_run else "subagent_task"),
            ),
        )


__all__ = [
    "TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY",
    "TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY",
    "TOOL_CALL_CONTROL_RECEIPT_KEY",
    "TOOL_CALL_CONTROL_STATE_KEY",
    "FixedToolCallControlScope",
    "GraphToolCallControlTopology",
    "PerInvocationToolCallControlScope",
    "RepeatedCallObservation",
    "RepeatedCallReasonCode",
    "RepeatedCallPolicy",
    "ResolvedToolCallControlPolicy",
    "ResolvedGraphToolCallControlProfile",
    "RunToolCallLimitAuthority",
    "RunToolCallLimitReservation",
    "ToolCallControlBinding",
    "ToolCallControlLoopFinalizationFailed",
    "ToolCallControlObservation",
    "ToolCallControlObserver",
    "ToolCallControlReasonCode",
    "ToolCallControlRole",
    "ToolCallControlScope",
    "ToolCallControlStateInvalid",
    "ToolCallControlStopReason",
    "ToolCallControlWorkloadProfile",
    "ToolCallBudgetObservation",
    "ToolCallBudgetDisposition",
    "ToolCallBudgetReasonCode",
    "ToolCallBudgetScope",
    "ToolCallControlAccountingMode",
    "ToolCallLimit",
    "build_tool_call_control",
    "default_graph_tool_call_control_profile",
]
