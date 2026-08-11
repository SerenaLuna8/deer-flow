"""Pure first-batch predicate, template, route, and Loop commit semantics.

These helpers freeze compiler-visible behavior without creating a LangGraph
runtime executor.  G40-G42 may call them behind lease/checkpoint boundaries;
G12 keeps them deterministic, authority-free, and side-effect-free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from deerflow.workflows.canonical import canonical_json_value
from deerflow.workflows.contracts import PredicateAst, PredicateClause, RestrictedTemplate, ValueBinding

BindingResolver = Callable[[ValueBinding], Any]


class WorkflowSemanticError(ValueError):
    """A pure authored semantic operation could not be evaluated safely."""


class PredicateEvaluationError(WorkflowSemanticError):
    pass


class RestrictedTemplateEvaluationError(WorkflowSemanticError):
    pass


def _evaluate_clause(clause: PredicateClause, *, resolve: BindingResolver) -> bool:
    left = resolve(clause.left)
    operator = clause.operator
    if operator == "is_null":
        return left is None
    if operator == "is_not_null":
        return left is not None
    if clause.right is None:
        raise PredicateEvaluationError("predicate comparison is missing its right binding")
    right = resolve(clause.right)
    try:
        if operator == "eq":
            return left == right
        if operator == "ne":
            return left != right
        if operator == "gt":
            return bool(left > right)
        if operator == "gte":
            return bool(left >= right)
        if operator == "lt":
            return bool(left < right)
        if operator == "lte":
            return bool(left <= right)
        if operator == "contains":
            return bool(right in left)
        if operator == "starts_with":
            return isinstance(left, str) and isinstance(right, str) and left.startswith(right)
        if operator == "ends_with":
            return isinstance(left, str) and isinstance(right, str) and left.endswith(right)
    except (TypeError, ValueError) as error:
        raise PredicateEvaluationError("predicate operands are not compatible with the operator") from error
    raise PredicateEvaluationError("predicate operator is not installed")


def evaluate_predicate(
    predicate: PredicateAst,
    *,
    resolve: BindingResolver,
) -> bool:
    """Evaluate the closed Predicate AST with ordered short-circuit semantics."""

    if not predicate.items:
        raise PredicateEvaluationError("predicate group must contain at least one item")
    if predicate.op == "and":
        for item in predicate.items:
            result = evaluate_predicate(item, resolve=resolve) if isinstance(item, PredicateAst) else _evaluate_clause(item, resolve=resolve)
            if not result:
                return False
        return True
    for item in predicate.items:
        result = evaluate_predicate(item, resolve=resolve) if isinstance(item, PredicateAst) else _evaluate_clause(item, resolve=resolve)
        if result:
            return True
    return False


def route_condition(
    branches: Sequence[tuple[str, PredicateAst]],
    *,
    else_output_port_id: str,
    resolve: BindingResolver,
) -> str:
    """Return the first matching branch, otherwise the explicit fallback."""

    for output_port_id, predicate in branches:
        if evaluate_predicate(predicate, resolve=resolve):
            return output_port_id
    return else_output_port_id


def render_restricted_template(
    template: RestrictedTemplate,
    *,
    resolve: BindingResolver,
) -> str:
    """Render literal text and typed values without evaluating expressions."""

    rendered: list[str] = []
    for segment in template.segments:
        if segment.kind == "text":
            rendered.append(segment.value)
            continue
        value = resolve(segment.value)
        if isinstance(value, str):
            rendered.append(value)
            continue
        try:
            rendered.append(canonical_json_value(value))
        except (TypeError, ValueError) as error:
            raise RestrictedTemplateEvaluationError("template binding did not resolve to a portable JSON value") from error
    return "".join(rendered)


class LoopCommitDecision(StrEnum):
    CONTINUE = "continue"
    DONE = "done"
    LIMIT_EXCEEDED = "limit_exceeded"


class LoopCommitProtocolError(WorkflowSemanticError):
    pass


@dataclass(frozen=True, slots=True)
class LoopCommitResult:
    iteration: int
    variables: tuple[tuple[str, Any], ...]
    decision: LoopCommitDecision
    error_code: str | None = None


def commit_loop_iteration(
    *,
    current_iteration: int,
    max_iterations: int,
    variable_ids: Sequence[str],
    next_values: Mapping[str, Any],
    termination: Callable[[Mapping[str, Any], int], bool],
) -> LoopCommitResult:
    """Atomically replace all Loop variables, increment, then evaluate route.

    The function returns a value object only after the complete exact next set
    and predicate result have validated.  A runtime adapter can therefore
    checkpoint the returned commit as one recoverable state update.
    """

    if type(current_iteration) is not int or current_iteration < 0:
        raise LoopCommitProtocolError("current iteration must be a non-negative integer")
    if type(max_iterations) is not int or max_iterations <= 0:
        raise LoopCommitProtocolError("max iterations must be a positive integer")
    if current_iteration >= max_iterations:
        raise LoopCommitProtocolError("cannot commit beyond the Loop iteration limit")
    ordered_ids = tuple(variable_ids)
    if not ordered_ids or len(ordered_ids) != len(set(ordered_ids)) or any(not item for item in ordered_ids):
        raise LoopCommitProtocolError("Loop variable ids must be a non-empty unique sequence")
    if set(next_values) != set(ordered_ids):
        raise LoopCommitProtocolError("Loop commit requires the complete exact variable set")

    committed = {variable_id: next_values[variable_id] for variable_id in ordered_ids}
    iteration = current_iteration + 1
    terminated = termination(MappingProxy(committed), iteration)
    if type(terminated) is not bool:
        raise LoopCommitProtocolError("Loop termination predicate must return a real boolean")
    if terminated:
        decision = LoopCommitDecision.DONE
        error_code = None
    elif iteration >= max_iterations:
        decision = LoopCommitDecision.LIMIT_EXCEEDED
        error_code = "WORKFLOW_LOOP_LIMIT_EXCEEDED"
    else:
        decision = LoopCommitDecision.CONTINUE
        error_code = None
    return LoopCommitResult(
        iteration=iteration,
        variables=tuple(committed.items()),
        decision=decision,
        error_code=error_code,
    )


class MappingProxy(Mapping[str, Any]):
    """Tiny immutable mapping view without exposing the mutable source dict."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._items = tuple(values.items())
        self._values = dict(self._items)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)


__all__ = [
    "LoopCommitDecision",
    "LoopCommitProtocolError",
    "LoopCommitResult",
    "PredicateEvaluationError",
    "RestrictedTemplateEvaluationError",
    "WorkflowSemanticError",
    "commit_loop_iteration",
    "evaluate_predicate",
    "render_restricted_template",
    "route_condition",
]
