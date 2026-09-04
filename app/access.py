"""Access code gate and per-visitor session identity.

The public demo used to be open to anyone and, worse, everyone shared a single
simulation: one visitor loading a scenario changed what every other visitor saw.
A session solves both problems at once.  The access code admits a visitor and
issues a signed session cookie; that cookie is also the key to the visitor's own
isolated workspace, so their tabs share state with each other and with nobody
else.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

from fastapi import Request

logger = logging.getLogger(__name__)

ACCESS_CODE_ENV = "RESCUEROUTE_ACCESS_CODE"
SESSION_COOKIE = "rescueroute_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60

# Reachable without the access code: the sign-in page itself, the health probe,
# and the Nokia callbacks, which carry their own sink credential.
PUBLIC_PREFIXES = ("/access", "/health", "/webhooks/")


def access_code() -> str:
    return os.getenv(ACCESS_CODE_ENV, "").strip()


def is_gated() -> bool:
    return bool(access_code())


def _signing_key() -> bytes:
    """Derive the cookie key from the access code and a per-deployment secret.

    Changing the access code therefore invalidates every session issued under
    the old one, which is what an operator expects when they rotate it.
    """
    secret = os.getenv("RESCUEROUTE_SESSION_SECRET", "").strip() or _fallback_secret()
    return hashlib.sha256(f"{secret}:{access_code()}".encode("utf-8")).digest()


_FALLBACK_SECRET = secrets.token_urlsafe(32)


def _fallback_secret() -> str:
    """Used when no secret is configured; sessions then end with the process."""
    return _FALLBACK_SECRET


def issue_session() -> str:
    """A signed, expiring token whose payload is the workspace identifier."""
    session_id = secrets.token_urlsafe(12)
    expires_at = int(time.time()) + SESSION_MAX_AGE_SECONDS
    payload = f"{session_id}.{expires_at}"
    signature = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload}.{urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def verify_session(token: str | None) -> str | None:
    """Return the workspace id carried by a valid token, else None."""
    if not token or token.count(".") != 2:
        return None
    session_id, expires_at, signature = token.split(".")
    payload = f"{session_id}.{expires_at}"
    expected = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    try:
        provided = urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except Exception:  # noqa: BLE001 - a malformed cookie is simply invalid
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        if int(expires_at) < time.time():
            return None
    except ValueError:
        return None
    return session_id


def code_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate.strip(), access_code())


def is_public_path(path: str) -> bool:
    return path.startswith(PUBLIC_PREFIXES)


def session_id_for(request: Request) -> str:
    """The caller's workspace key.

    When no access code is configured every caller shares the ``default``
    workspace, which keeps local development and the test suite simple.
    """
    if not is_gated():
        return "default"
    return verify_session(request.cookies.get(SESSION_COOKIE)) or "default"


def log_startup_posture() -> None:
    if not is_gated():
        logger.warning(
            "%s is not set: the demo is open to anyone and all visitors share one simulation.",
            ACCESS_CODE_ENV,
        )
    elif not os.getenv("RESCUEROUTE_SESSION_SECRET", "").strip():
        logger.warning(
            "RESCUEROUTE_SESSION_SECRET is not set; sessions will end when the process restarts."
        )
