"""Seam for Provider/model-specific final shaped-request measurement."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from .contracts import FinalRequestMeasurement

RequestT = TypeVar("RequestT", contravariant=True)


@runtime_checkable
class FinalShapedRequestCostAdapter(Protocol[RequestT]):
    """Hide wire framing, tokenizer, and visual-cost rules behind one Interface.

    Implementations receive the final request object at the innermost Provider
    seam and return only the safe measurement contract. The request itself must
    never be placed in Context Evidence or a Context Projection Head.
    """

    def measure_final_request(
        self,
        request: RequestT,
        /,
    ) -> FinalRequestMeasurement: ...


__all__ = ["FinalShapedRequestCostAdapter"]
