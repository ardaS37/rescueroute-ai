"""Small in-memory WebSocket broadcaster for the live demo dashboard."""

from __future__ import annotations

import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class DashboardBroadcaster:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def broadcast(self, event: dict[str, object]) -> None:
        """Deliver to every live dashboard; one dead client must not stop the rest.

        Starlette raises several unrelated exception types for a socket that has
        gone away.  Catching only ``RuntimeError`` used to abort the whole
        fan-out, so the remaining dashboards silently missed the event and the
        HTTP request that triggered it failed with a 500.
        """
        for websocket in tuple(self._connections):
            try:
                await websocket.send_json(event)
            except Exception:  # noqa: BLE001 - any transport failure means the client is gone
                logger.debug("Dropping dashboard socket that failed to receive an event", exc_info=True)
                self.disconnect(websocket)
