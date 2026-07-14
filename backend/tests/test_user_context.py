"""Tests for runtime.user_context — contextvar three-state semantics.

These tests opt out of the autouse contextvar fixture (added in
commit 6) because they explicitly test the cases where the contextvar
is set or unset.
"""

import asyncio
from types import SimpleNamespace

import pytest

from deerflow.runtime.user_context import (
    AUTO,
    DEFAULT_USER_ID,
    CurrentUser,
    get_current_user,
    get_effective_user_id,
    get_runtime_storage_user_id,
    require_current_user,
    reset_current_user,
    reset_runtime_storage_user_id,
    resolve_user_id,
    set_current_user,
    set_runtime_storage_user_id,
)


@pytest.mark.no_auto_user
def test_default_is_none():
    """Before any set, contextvar returns None."""
    assert get_current_user() is None


@pytest.mark.no_auto_user
def test_set_and_reset_roundtrip():
    """set_current_user returns a token that reset restores."""
    user = SimpleNamespace(id="user-1")
    token = set_current_user(user)
    try:
        assert get_current_user() is user
    finally:
        reset_current_user(token)
    assert get_current_user() is None


@pytest.mark.no_auto_user
def test_require_current_user_raises_when_unset():
    """require_current_user raises RuntimeError if contextvar is unset."""
    assert get_current_user() is None
    with pytest.raises(RuntimeError, match="without user context"):
        require_current_user()


@pytest.mark.no_auto_user
def test_require_current_user_returns_user_when_set():
    """require_current_user returns the user when contextvar is set."""
    user = SimpleNamespace(id="user-2")
    token = set_current_user(user)
    try:
        assert require_current_user() is user
    finally:
        reset_current_user(token)


@pytest.mark.no_auto_user
def test_protocol_accepts_duck_typed():
    """CurrentUser is a runtime_checkable Protocol matching any .id-bearing object."""
    user = SimpleNamespace(id="user-3")
    assert isinstance(user, CurrentUser)


@pytest.mark.no_auto_user
def test_protocol_rejects_no_id():
    """Objects without .id do not satisfy CurrentUser Protocol."""
    not_a_user = SimpleNamespace(email="no-id@example.com")
    assert not isinstance(not_a_user, CurrentUser)


# ---------------------------------------------------------------------------
# get_effective_user_id / DEFAULT_USER_ID tests
# ---------------------------------------------------------------------------


def test_default_user_id_is_default():
    assert DEFAULT_USER_ID == "default"


@pytest.mark.no_auto_user
def test_effective_user_id_returns_default_when_no_user():
    """No user in context -> fallback to DEFAULT_USER_ID."""
    assert get_effective_user_id() == "default"


@pytest.mark.no_auto_user
def test_effective_user_id_returns_user_id_when_set():
    user = SimpleNamespace(id="u-abc-123")
    token = set_current_user(user)
    try:
        assert get_effective_user_id() == "u-abc-123"
    finally:
        reset_current_user(token)


@pytest.mark.no_auto_user
def test_effective_user_id_coerces_to_str():
    """User.id might be a UUID object; must come back as str."""
    import uuid

    uid = uuid.uuid4()

    user = SimpleNamespace(id=uid)
    token = set_current_user(user)
    try:
        assert get_effective_user_id() == str(uid)
    finally:
        reset_current_user(token)


@pytest.mark.no_auto_user
def test_runtime_storage_override_is_separate_from_repository_identity():
    repository_user = SimpleNamespace(id="repository-owner")
    repository_token = set_current_user(repository_user)
    storage_token = set_runtime_storage_user_id("channel-runtime-bucket")
    try:
        assert get_runtime_storage_user_id() == "channel-runtime-bucket"
        assert get_effective_user_id() == "channel-runtime-bucket"
        assert get_current_user() is repository_user
        assert require_current_user() is repository_user
        assert resolve_user_id(AUTO, method_name="test repository") == "repository-owner"
    finally:
        reset_runtime_storage_user_id(storage_token)
        reset_current_user(repository_token)

    assert get_runtime_storage_user_id() is None
    assert get_current_user() is None
    assert get_effective_user_id() == DEFAULT_USER_ID


@pytest.mark.no_auto_user
def test_runtime_storage_override_is_task_local_and_resets_after_failure():
    async def scenario() -> None:
        repository_user = SimpleNamespace(id="repository-owner")
        repository_token = set_current_user(repository_user)
        release = asyncio.Event()
        ready = [asyncio.Event(), asyncio.Event()]
        observations: dict[str, tuple[str, str | None]] = {}

        async def worker(name: str, storage_user_id: str, index: int, *, fail: bool) -> None:
            storage_token = set_runtime_storage_user_id(storage_user_id)
            try:
                ready[index].set()
                await release.wait()
                current = get_current_user()
                observations[name] = (
                    get_effective_user_id(),
                    str(current.id) if current is not None else None,
                )
                if fail:
                    raise RuntimeError("expected worker failure")
            finally:
                reset_runtime_storage_user_id(storage_token)

        try:
            first = asyncio.create_task(worker("first", "runtime-a", 0, fail=False))
            second = asyncio.create_task(worker("second", "runtime-b", 1, fail=True))
            await asyncio.gather(*(event.wait() for event in ready))

            assert get_runtime_storage_user_id() is None
            assert get_effective_user_id() == "repository-owner"

            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert results[0] is None
            assert isinstance(results[1], RuntimeError)
            assert observations == {
                "first": ("runtime-a", "repository-owner"),
                "second": ("runtime-b", "repository-owner"),
            }

            async def later_task() -> tuple[str | None, str]:
                return get_runtime_storage_user_id(), get_effective_user_id()

            assert await asyncio.create_task(later_task()) == (None, "repository-owner")
        finally:
            reset_current_user(repository_token)

        assert get_runtime_storage_user_id() is None
        assert get_current_user() is None
        assert get_effective_user_id() == DEFAULT_USER_ID

    asyncio.run(scenario())
