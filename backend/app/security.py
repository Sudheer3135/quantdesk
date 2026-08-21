"""API-key access control.

Audit finding H-2: every endpoint was open, including the ones that write to
Postgres. On a laptop behind a firewall that is merely untidy; the moment the
port is reachable from a shared network it means anyone can push rows into
the tables the backtester treats as ground truth.

The design is deliberately the smallest thing that fits a personal tool:

  - One shared key, supplied only through the environment. Never a literal,
    never a default value — a default key is the same as no key, except it
    looks like security.
  - Reads stay open. The dashboard polls them, they expose nothing that is
    not already on screen, and putting a key in front of them would push the
    key into the browser for no gain.
  - Writes require the key. That is the boundary worth defending: the
    archive is the one thing here that cannot be reconstructed.

Unset in development, the layer disables itself and says so. In production
an unset key is a startup failure rather than a silent open door — a
misconfiguration that fails loudly is one you fix, and one that fails
quietly is one you ship.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import Header, HTTPException, status

from .config import get_settings

log = logging.getLogger(__name__)

HEADER = "X-API-Key"


class AuthNotConfigured(RuntimeError):
    """Raised at startup when production has no key set."""


def configured_key() -> str | None:
    key = get_settings().api_key
    return key or None            # treat "" the same as unset


def auth_enabled() -> bool:
    return configured_key() is not None


def verify_startup() -> None:
    """Refuse to boot a production deployment with no key.

    Called once from the app lifespan. Development is allowed to run open,
    because requiring a key to run the thing locally is how people end up
    committing one.
    """
    settings = get_settings()
    if settings.environment == "prod" and not configured_key():
        raise AuthNotConfigured(
            "ENVIRONMENT=prod requires API_KEY to be set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
            "and put it in the environment — never in the repository."
        )
    if not configured_key():
        log.warning(
            "API_KEY is not set — write endpoints are unprotected. Fine for "
            "localhost, not for anything reachable from another machine."
        )


def key_is_valid(candidate: str | None) -> bool:
    """Constant-time comparison, so a wrong key leaks nothing by timing."""
    expected = configured_key()
    if expected is None:
        return True                      # auth disabled
    if not candidate:
        return False
    return secrets.compare_digest(candidate, expected)


def require_api_key(x_api_key: str | None = Header(default=None, alias=HEADER)) -> None:
    """Dependency for endpoints that change stored state."""
    if not key_is_valid(x_api_key):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"This endpoint changes stored data. Send your key in the {HEADER} header.",
            headers={"WWW-Authenticate": HEADER},
        )
