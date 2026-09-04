import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, camara
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

    def test_changed_active_corridor_triggers_backend_reroute(self) -> None:
        with patch.object(camara.nokia, "enabled", False), TestClient(app) as client:
            client.post("/simulation/configure", json={
                "template": "stadium_match", "seed": 42, "crowd_pattern": "balanced"
            })
            incident = client.post("/incidents", json={
                "location": "main_stage", "priority": "critical", "description": "Auto reroute test"
            }).json()
            dispatch = client.post(f"/incidents/{incident['id']}/dispatch").json()
            segment = dispatch["decision"]["segments"][1]
            response = client.post("/simulation/events/corridor", json={
                "source": segment["source"], "destination": segment["destination"], "closed": True
            })
            history = client.get(f"/incidents/{incident['id']}/history").json()["entries"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["event_type"], "reroute")
        self.assertIn("Automatic reroute", history[-1]["trigger"])

    def test_nokia_webhook_rejects_missing_bearer_credential(self) -> None:
        with TestClient(app) as client:
            response = client.post("/webhooks/nokia/geofence", json={"type": "area-entered"})
        self.assertEqual(response.status_code, 401)

    def test_batch_pressure_update_changes_multiple_zones_in_one_request(self) -> None:
        with TestClient(app) as client:
            client.post("/simulation/configure", json={
                "template": "stadium_match", "seed": 42, "crowd_pattern": "balanced"
            })
            response = client.post("/simulation/events/congestion/batch", json={
                "zone_densities": {"north_zone": 0.61, "west_zone": 0.42}
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["zone_congestion"]["north_zone"], 0.61)
        self.assertEqual(response.json()["zone_congestion"]["west_zone"], 0.42)
