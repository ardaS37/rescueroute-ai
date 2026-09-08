"""Nokia Network as Code (RapidAPI) adapter with simulator-safe fallback support."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# One decision fans out into up to seven sequential provider calls (device
# status per candidate team, location, congestion, QoD, geofencing).  At the
# stock ten-second socket timeout plus rate-limit retries a single slow
# provider could stall a dispatch for minutes, and the operator sees nothing
# but a frozen dashboard.  Both bounds are configurable so a deployment can
# trade responsiveness for provider fidelity.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_DECISION_BUDGET_SECONDS = 6.0


class NokiaNaCError(RuntimeError):
    pass


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


class NokiaNaCClient:
    """Calls Nokia's CAMARA-compatible simulator only when explicitly enabled."""

    # Nokia API Hub exposes CAMARA paths directly; RapidAPI remains the auth gateway.
    base_url = "https://network-as-code.p-eu.apihub.nokia.io"
    rapidapi_host = "network-as-code.nokia.rapidapi.com"

    def __init__(self) -> None:
        self.api_key = os.getenv("RAPIDAPI_KEY", "")
        self.enabled = os.getenv("NAC_LIVE_ENABLED", "false").lower() == "true" and bool(self.api_key)
        self.rapidapi_host = os.getenv("RAPIDAPI_HOST", self.rapidapi_host)
        self.application_server_ip = os.getenv("NAC_APPLICATION_SERVER_IP", "1.1.1.1")
        self.webhook_base_url = os.getenv("NAC_WEBHOOK_BASE_URL", "https://tech-mate.tech").rstrip("/")
        self.webhook_token = os.getenv("NAC_WEBHOOK_TOKEN", "").strip()
        self.request_timeout = _positive_float(
            "NAC_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        self.decision_budget = _positive_float(
            "NAC_DECISION_BUDGET_SECONDS", DEFAULT_DECISION_BUDGET_SECONDS
        )
        self._deadline: float | None = None

    def start_decision_budget(self) -> None:
        """Open the wall-clock window one decision may spend on this provider."""
        self._deadline = time.monotonic() + self.decision_budget

    def clear_decision_budget(self) -> None:
        self._deadline = None

    def _next_timeout(self) -> float:
        """Seconds the next call may take, or raise once the window is spent."""
        if self._deadline is None:
            return self.request_timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise NokiaNaCError(
                f"Nokia NaC budget of {self.decision_budget:g}s for this decision is spent"
            )
        return min(self.request_timeout, remaining)

    def _may_wait(self, seconds: float) -> bool:
        """Whether a rate-limit back-off still fits inside the decision window."""
        return self._deadline is None or self._deadline - time.monotonic() > seconds

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        if not self.enabled:
            raise NokiaNaCError("Nokia NaC live simulator is not enabled.")
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-RapidAPI-Key": self.api_key,
                "X-RapidAPI-Host": self.rapidapi_host,
            },
            method="POST",
        )
        for attempt in range(3):
            timeout = self._next_timeout()
            try:
                with urlopen(request, timeout=timeout) as response:  # nosec B310 - configured Nokia API URL
                    return json.loads(response.read().decode())
            except HTTPError as error:
                if error.code == 429 and attempt < 2:
                    retry_after = error.headers.get("Retry-After", "1")
                    try:
                        delay = max(1, min(5, int(retry_after)))
                    except ValueError:
                        delay = 1
                    if not self._may_wait(delay):
                        raise NokiaNaCError(
                            "Nokia NaC rate-limited and the decision budget cannot absorb the back-off"
                        ) from error
                    time.sleep(delay)
                    continue
                raise NokiaNaCError(f"Nokia NaC request failed: {error}") from error
            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                raise NokiaNaCError(f"Nokia NaC request failed: {error}") from error
        raise NokiaNaCError("Nokia NaC request failed after rate-limit retries.")

    def connectivity(self, phone_number: str) -> str:
        response = self._post("/device-status/v0/connectivity", {"device": {"phoneNumber": phone_number}})
        return str(
            response.get("connectivityStatus")
            or response.get("connectivity")
            or response.get("status")
            or "UNKNOWN"
        )

    def location(self, phone_number: str) -> dict[str, object]:
        return self._post(
            "/location-retrieval/v0/retrieve",
            {"device": {"phoneNumber": phone_number}, "maxAge": 600},
        )

    def congestion(self, phone_number: str) -> dict[str, object]:
        now = datetime.now(UTC)
        return self._post(
            "/congestion-insights/v0/query",
            {
                "device": {"phoneNumber": phone_number},
                "start": (now - timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                "end": now.isoformat().replace("+00:00", "Z"),
            },
        )

    def create_qod_session(self, phone_number: str) -> str:
        response = self._post(
            "/qod/v0/sessions",
            {
                "duration": 900,
                "qosProfile": "QOS_E",
                "device": {
                    "phoneNumber": phone_number,
                    "ipv4Address": {
                        "publicAddress": "1.1.1.1", "privateAddress": "1.1.1.1"
                    },
                },
                "applicationServer": {"ipv4Address": self.application_server_ip},
                "webhook": {
                    "notificationUrl": f"{self.webhook_base_url}/webhooks/nokia/qod",
                    "notificationAuthToken": self.webhook_token,
                },
            },
        )
        return str(response.get("sessionId") or response.get("id") or "session requested")

    def create_geofence_subscription(
        self, phone_number: str, latitude: float, longitude: float, radius_m: int
    ) -> str:
        """Watch the entry the team was actually routed to.

        The area used to be a fixed circle in Doha regardless of venue or gate,
        so an ``area-entered`` callback carried no information about the route
        it was meant to confirm.  It is now the selected gate's real position.
        """
        if not self.webhook_token:
            raise NokiaNaCError("NAC_WEBHOOK_TOKEN is required for authenticated geofencing callbacks.")
        response = self._post(
            "/geofencing-subscriptions/v0.3/subscriptions",
            {
                "protocol": "HTTP",
                "sink": f"{self.webhook_base_url}/webhooks/nokia/geofence",
                "types": [
                    "org.camaraproject.geofencing-subscriptions.v0.area-entered",
                ],
                "config": {
                    "subscriptionDetail": {
                        "device": {"phoneNumber": phone_number},
                        "area": {
                            "areaType": "CIRCLE",
                            "center": {"latitude": latitude, "longitude": longitude},
                            "radius": radius_m,
                        },
                    },
                    "initialEvent": True,
                },
                "sinkCredential": {
                    "credentialType": "PLAIN",
                    "identifier": "rescueroute-ai",
                    "secret": self.webhook_token,
                },
            },
        )
        return str(response.get("subscriptionId") or response.get("id") or "subscription requested")
