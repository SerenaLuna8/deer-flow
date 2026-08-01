from __future__ import annotations

import pytest

from deerflow.sandbox.env_policy import build_sandbox_env, is_blocked_env_name


@pytest.mark.parametrize(
    "name",
    [
        "DB_PASS",
        "SMTP_PASS",
        "PGPASSFILE",
        "PGSERVICEFILE",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SUDO_ASKPASS",
    ],
)
def test_abbreviated_password_and_askpass_names_are_blocked(name: str) -> None:
    assert is_blocked_env_name(name) is True


def test_abbreviated_password_and_askpass_values_are_not_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = {
        "DB_PASS": "database-password",
        "SMTP_PASS": "smtp-password",
        "PGPASSFILE": "/private/credentials/.pgpass",
        "PGSERVICEFILE": "/private/credentials/pg_service.conf",
        "GIT_ASKPASS": "/private/bin/git-askpass",
        "SSH_ASKPASS": "/private/bin/ssh-askpass",
        "SUDO_ASKPASS": "/private/bin/sudo-askpass",
    }
    for name, value in blocked.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DEERFLOW_SAFE_ENV", "safe")
    monkeypatch.setenv("PWD", "/safe/current")
    monkeypatch.setenv("OLDPWD", "/safe/previous")

    env = build_sandbox_env()

    assert blocked.keys().isdisjoint(env)
    assert env["DEERFLOW_SAFE_ENV"] == "safe"
    assert env["PWD"] == "/safe/current"
    assert env["OLDPWD"] == "/safe/previous"


def test_explicit_request_scoped_injection_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_PASS", "host-password-must-not-leak")

    env = build_sandbox_env({"DB_PASS": "request-scoped-password"})

    assert env["DB_PASS"] == "request-scoped-password"
