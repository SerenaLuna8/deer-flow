from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.gateway.routers.private_work import (
    PRIVATE_THREAD_TITLE_MAX_LENGTH,
    PrivateThreadBranchRequest,
    PrivateThreadCreateRequest,
    PrivateThreadPatchRequest,
)


def test_private_thread_title_requests_accept_the_exact_limit() -> None:
    title = "X" * PRIVATE_THREAD_TITLE_MAX_LENGTH

    assert (
        PrivateThreadCreateRequest(
            thread_id=uuid.uuid4(),
            display_name=title,
        ).display_name
        == title
    )
    assert (
        PrivateThreadPatchRequest(
            expected_version=1,
            display_name=title,
        ).display_name
        == title
    )
    assert (
        PrivateThreadBranchRequest(
            message_id="message-1",
            title=title,
        ).title
        == title
    )


@pytest.mark.parametrize(
    ("request_model", "payload"),
    [
        (
            PrivateThreadCreateRequest,
            {"thread_id": uuid.uuid4()},
        ),
        (
            PrivateThreadPatchRequest,
            {"expected_version": 1},
        ),
        (
            PrivateThreadBranchRequest,
            {"message_id": "message-1"},
        ),
    ],
)
def test_private_thread_title_requests_reject_titles_over_the_limit(
    request_model: type[PrivateThreadCreateRequest | PrivateThreadPatchRequest | PrivateThreadBranchRequest],
    payload: dict[str, object],
) -> None:
    title_field = "title" if request_model is PrivateThreadBranchRequest else "display_name"

    with pytest.raises(ValidationError):
        request_model(**payload, **{title_field: "X" * (PRIVATE_THREAD_TITLE_MAX_LENGTH + 1)})
