"""Write-path protection for the publicly reachable demo deployment.

The demo user interfaces are intentionally anonymous, so the write path is
guarded by two independent controls:

* an optional shared token (``RESCUEROUTE_API_TOKEN``) that locks every state
  changing endpoint for private deployments, and
* an always-on per-client rate limit that bounds how often an anonymous caller
  can trigger outbound Gemini/Nokia work or grow the incident store.

Read endpoints stay open so the dashboards keep working without credentials.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from collections import OrderedDict

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

TOKEN_ENV = "RESCUEROUTE_API_TOKEN"


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


class RateLimiter:
    """Per-client token bucket with a bounded LRU of tracked clients.

    The client table is capped so that tracking the limit can never itself
    become the memory-exhaustion vector it is meant to prevent.
    """

    def __init__(self, name: str, per_minute: float, burst: float, max_clients: int = 4096) -> None:
        self.name = name
        self.per_minute = per_minute
        self.burst = burst
        self.max_clients = max_clients
        self._clients: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def consume(self, client: str) -> float:
        """Return 0 when the call is allowed, otherwise the retry delay in seconds."""
        now = time.monotonic()
        tokens, updated_at = self._clients.pop(client, (self.burst, now))
        tokens = min(self.burst, tokens + (now - updated_at) * self.per_minute / 60)
        if tokens < 1:
            self._clients[client] = (tokens, now)
            self._trim()
            return max(1.0, (1 - tokens) * 60 / self.per_minute)
        self._clients[client] = (tokens - 1, now)
        self._trim()
        return 0.0

    def reset(self) -> None:
        self._clients.clear()

    def _trim(self) -> None:
        while len(self._clients) > self.max_clients:
            self._clients.popitem(last=False)


# Cheap state changes (scenario load, geofence report, incident creation).
SIMULATION_LIMITER = RateLimiter(
    "simulation write",
    _positive_float("RESCUEROUTE_WRITE_PER_MINUTE", 60),
    _positive_float("RESCUEROUTE_WRITE_BURST", 20),
)
# Expensive state changes: every one of these can fan out into Gemini and Nokia calls.
DISPATCH_LIMITER = RateLimiter(
    "dispatch",
    _positive_float("RESCUEROUTE_DISPATCH_PER_MINUTE", 30),
    _positive_float("RESCUEROUTE_DISPATCH_BURST", 15),
)


def rate_limiting_enabled() -> bool:
    return os.getenv("RESCUEROUTE_RATE_LIMIT_ENABLED", "true").strip().lower() != "false"


def reset_rate_limits() -> None:
    SIMULATION_LIMITER.reset()
    DISPATCH_LIMITER.reset()


def _client_key(request: Request) -> str:
    """Identify the caller.

    Uvicorn runs with ``--proxy-headers --forwarded-allow-ips=*`` behind Caddy,
    so ``request.client`` already carries the real client address.
    """
    return request.client.host if request.client else "unknown"


def _require_token(request: Request) -> None:
    expected = os.getenv(TOKEN_ENV, "").strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    provided = (
        header[len("Bearer ") :].strip()
        if header.lower().startswith("bearer ")
        else request.headers.get("x-api-key", "")
    )
    if not hmac.compare_digest(provided, expected):
        logger.warning("Rejected unauthenticated write from %s", _client_key(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid RescueRoute API token is required for write operations.",
        )


def _enforce(request: Request, limiter: RateLimiter) -> None:
    if not rate_limiting_enabled():
        return
    retry_after = limiter.consume(_client_key(request))
    if retry_after:
        logger.warning("Rate limited %s on the %s budget", _client_key(request), limiter.name)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many {limiter.name} requests. Retry in {int(retry_after) + 1} seconds.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


async def write_guard(request: Request) -> None:
    """Protect a state-changing endpoint that stays inside the process."""
    _require_token(request)
    _enforce(request, SIMULATION_LIMITER)


async def dispatch_guard(request: Request) -> None:
    """Protect an endpoint that can trigger outbound Gemini/Nokia calls."""
    _require_token(request)
    _enforce(request, SIMULATION_LIMITER)
    _enforce(request, DISPATCH_LIMITER)


def log_startup_posture() -> None:
    if not os.getenv(TOKEN_ENV, "").strip():
        logger.warning(
            "%s is not set: write endpoints are anonymous and protected by rate limits only.",
            TOKEN_ENV,
        )
    if not rate_limiting_enabled():
        logger.warning("Rate limiting is disabled; do not run a public deployment this way.")
