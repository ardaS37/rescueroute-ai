import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.services.realtime import DashboardBroadcaster


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.events: list[dict[str, object]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, event: dict[str, object]) -> None:
        self.events.append(event)


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_reaches_every_connected_dashboard(self) -> None:
        broadcaster = DashboardBroadcaster()
        first, second = FakeWebSocket(), FakeWebSocket()
        await broadcaster.connect(first)  # type: ignore[arg-type]
        await broadcaster.connect(second)  # type: ignore[arg-type]

        event = {"type": "simulation_state", "state": {"template": "stadium_match"}}
        await broadcaster.broadcast(event)

        self.assertTrue(first.accepted)
        self.assertEqual(first.events, [event])
        self.assertEqual(second.events, [event])


class WebSocketEndpointTests(unittest.TestCase):
    def test_dashboard_socket_sends_current_simulation_snapshot(self) -> None:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/dashboard") as socket:
                event = socket.receive_json()
        self.assertEqual(event["type"], "snapshot")
        self.assertEqual(event["state"]["template"], "stadium_match")

    def test_simulation_change_is_pushed_to_dashboard_socket(self) -> None:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/dashboard") as socket:
                socket.receive_json()  # Initial snapshot.
                response = client.post("/simulation/advance", json={"minutes": 5})
                event = socket.receive_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(event["type"], "simulation_state")
        self.assertEqual(event["state"]["simulated_minutes"], 5)
