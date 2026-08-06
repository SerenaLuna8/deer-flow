"""Canonical email identity helpers.

ActWeave treats an email address as one account identifier across local
password auth, OIDC provisioning, invitations, and administrative setup.
Keeping the normalization in one dependency-free module prevents those entry
points from drifting into different collision rules.
"""

from __future__ import annotations


def normalize_email(email: str) -> str:
    """Return the canonical stored and compared form of an email address."""

    if not isinstance(email, str):
        raise TypeError("email must be a string")
    return email.strip().lower()


__all__ = ["normalize_email"]
