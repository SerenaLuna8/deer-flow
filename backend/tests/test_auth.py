"""Focused tests for the active password, JWT, and User contracts."""

from datetime import timedelta
from uuid import uuid4

import bcrypt

from app.gateway.auth import create_access_token, decode_token, hash_password, verify_password
from app.gateway.auth.errors import TokenError
from app.gateway.auth.models import User
from app.gateway.auth.password import needs_rehash


def test_hash_password_and_verify() -> None:
    password = "s3cr3tP@ssw0rd!"
    hashed = hash_password(password)
    assert hashed.startswith("$dfv2$")
    assert verify_password(password, hashed) is True
    assert verify_password("wrongpassword", hashed) is False


def test_hash_password_uses_unique_salts() -> None:
    first = hash_password("testpassword")
    second = hash_password("testpassword")
    assert first != second
    assert verify_password("testpassword", first) is True
    assert verify_password("testpassword", second) is True


def test_verify_password_rejects_empty() -> None:
    assert verify_password("", hash_password("nonempty")) is False


def test_verify_v1_and_bare_bcrypt_hashes() -> None:
    password = "legacyP@ssw0rd"
    raw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    assert verify_password(password, f"$dfv1${raw}") is True
    assert verify_password(password, raw) is True
    assert needs_rehash(f"$dfv1${raw}") is True
    assert needs_rehash(raw) is True
    assert needs_rehash(hash_password(password)) is False


def test_create_and_decode_token(monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTH_JWT_SECRET",
        "test-secret-key-for-jwt-testing-minimum-32-chars",
    )
    user_id = str(uuid4())
    payload = decode_token(create_access_token(user_id))
    assert payload is not None
    assert not isinstance(payload, TokenError)
    assert payload.sub == user_id


def test_expired_and_invalid_tokens_fail_closed() -> None:
    expired = create_access_token(
        str(uuid4()),
        expires_delta=timedelta(seconds=-1),
    )
    assert decode_token(expired) == TokenError.EXPIRED
    for token in ("not.a.valid.token", "", "completely-wrong"):
        assert isinstance(decode_token(token), TokenError)


def test_user_model_defaults_and_setup_flag() -> None:
    user = User(email="test@example.com", password_hash="hash")
    assert user.needs_setup is False
    assert user.token_version == 0
    assert (
        User(
            email="admin@example.com",
            password_hash="hash",
            needs_setup=True,
        ).needs_setup
        is True
    )
