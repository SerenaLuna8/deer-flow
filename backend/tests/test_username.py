from __future__ import annotations

import pytest

from app.gateway.auth.username import (
    UsernameInvalid,
    parse_username,
    username_from_email_local_part,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Admin", "admin"),
        ("user_01", "user_01"),
        ("  Alice_1  ", "alice_1"),
    ],
)
def test_parse_username_accepts_ascii_identifiers(raw: str, expected: str) -> None:
    assert parse_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ab",
        "1admin",
        "用户名",
        "alice.chen",
        "alice-chen",
        "alice@site",
        "alice chen",
        "a" * 33,
    ],
)
def test_parse_username_rejects_chinese_and_special_characters(raw: str) -> None:
    with pytest.raises(UsernameInvalid):
        parse_username(raw)


def test_username_from_email_local_part_is_a_valid_username() -> None:
    assert parse_username(username_from_email_local_part("Ada.Lovelace+dev@example.com")) == "adalovelacedev"


class _IdentifierRepository:
    def __init__(self, user) -> None:
        self._user = user

    async def get_user_by_email(self, email: str):
        return self._user if email == self._user.email else None

    async def get_user_by_username(self, username: str):
        return self._user if username == self._user.username else None


@pytest.mark.asyncio
async def test_local_provider_authenticates_email_or_username() -> None:
    from app.gateway.auth.local_provider import LocalAuthProvider
    from app.gateway.auth.models import User
    from app.gateway.auth.password import hash_password

    password = "s3cr3tP@ssw0rd!"
    user = User(email="ada@example.com", username="ada", password_hash=hash_password(password))
    provider = LocalAuthProvider(_IdentifierRepository(user))

    assert await provider.authenticate({"email": "ada@example.com", "password": password}) == user
    assert await provider.authenticate({"email": "ada", "password": password}) == user
    assert await provider.authenticate({"email": "missing", "password": password}) is None
    assert await provider.authenticate({"email": "ada", "password": "wrong-password"}) is None
