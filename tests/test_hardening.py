"""Regression tests for the write-path, state-machine and realtime hardening."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import security
from app.main import app, workspaces
from app.models import IncidentStatus, Priority
from app.services.camara_simulator import CamaraSimulator
from app.services.incident_service import IncidentService, IncidentStateError
from app.services.realtime import DashboardBroadcaster
from app.services.routing import RoutingService

def default_workspace():
    """The workspace every caller shares while no access code is configured."""
    return workspaces.get("default")



def build_service() -> tuple[CamaraSimulator, IncidentService]:
    simulator = CamaraSimulator()
    return simulator, IncidentService(simulator, RoutingService(simulator))


class IncidentStateMachineTests(unittest.TestCase):
    """A closed incident must stay closed, and progress must track the real team."""

    def test_resolved_incident_cannot_be_dispatched_again(self) -> None:
        _, service = build_service()
        incident = service.create("main_stage", Priority.HIGH, "State test")
        _, decision = service.dispatch(incident.id)
        service.record_geofence_event(incident.id, decision.team_id, decision.selected_gate, "entered_selected_gate")
        service.record_geofence_event(incident.id, decision.team_id, "main_stage", "reached_patient")

        with self.assertRaises(IncidentStateError):
            service.dispatch(incident.id, trigger="re-dispatch after arrival")
        self.assertEqual(service.get(incident.id).status, IncidentStatus.RESOLVED)
        self.assertEqual(len(service.get_history(incident.id).entries), 1)

    def test_geofence_events_are_refused_after_arrival(self) -> None:
        _, service = build_service()
        incident = service.create("main_stage", Priority.HIGH, "Ordering test")
        _, decision = service.dispatch(incident.id)
        service.record_geofence_event(incident.id, decision.team_id, decision.selected_gate, "entered_selected_gate")
        service.record_geofence_event(incident.id, decision.team_id, "main_stage", "reached_patient")

        with self.assertRaises(IncidentStateError):
            service.record_geofence_event(
                incident.id, decision.team_id, decision.selected_gate, "entered_selected_gate"
            )
        progress = service.get_progress(incident.id)
        self.assertEqual(progress.last_location, "main_stage")
        self.assertEqual([event.event_type for event in progress.events], ["entered_selected_gate", "reached_patient"])

    def test_repeating_the_newest_geofence_event_is_idempotent(self) -> None:
        _, service = build_service()
        incident = service.create("main_stage", Priority.HIGH, "Duplicate callback test")
        _, decision = service.dispatch(incident.id)
        service.record_geofence_event(incident.id, decision.team_id, decision.selected_gate, "entered_selected_gate")
        progress = service.record_geofence_event(
            incident.id, decision.team_id, decision.selected_gate, "entered_selected_gate"
        )
        self.assertEqual(len(progress.events), 1)

    def test_duplicate_nokia_callback_is_reported_as_ignored(self) -> None:
        _, service = build_service()
        incident = service.create("main_stage", Priority.HIGH, "Nokia duplicate test")
        _, decision = service.dispatch(incident.id)
        service._nokia_geofences["sub-1"] = (incident.id, decision.team_id, decision.selected_gate)
        callback = {
            "data": {"subscriptionId": "sub-1"},
            "type": "org.camaraproject.geofencing-subscriptions.v0.area-entered",
        }
        service.process_nokia_geofence_callback(callback)
        service.record_geofence_event(incident.id, decision.team_id, "main_stage", "reached_patient")

        progress, status_message = service.process_nokia_geofence_callback(callback)
        self.assertIsNone(progress)
        self.assertTrue(status_message.startswith("ignored"))

    def test_progress_follows_the_team_assigned_by_a_reroute(self) -> None:
        simulator, service = build_service()
        incident = service.create("main_stage", Priority.HIGH, "Failover test")
        service.dispatch(incident.id)
        simulator._device_status["medic_alpha"] = False
        _, decision = service.dispatch(incident.id, trigger="primary team dropped")

        self.assertEqual(decision.team_id, "medic_bravo")
        self.assertEqual(service.get_progress(incident.id).team_id, "medic_bravo")

    def test_venue_change_cancels_incidents_it_can_no_longer_route_to(self) -> None:
        simulator, service = build_service()
        incident = service.create("main_stage", Priority.HIGH, "Venue switch test")
        service.dispatch(incident.id)
        simulator.configure("pilgrimage_flow", seed=42, crowd_pattern="balanced")

        self.assertEqual(service.cancel_incidents_outside_venue(), [incident.id])
        self.assertEqual(service.get(incident.id).status, IncidentStatus.CANCELLED)
        self.assertEqual(service.affected_active_incidents(zones={"mataf_zone"}), [])
        with self.assertRaises(IncidentStateError):
            service.dispatch(incident.id, trigger="after venue switch")

    def test_incident_store_stays_bounded(self) -> None:
        simulator, _ = build_service()
        with patch.dict(os.environ, {"RESCUEROUTE_MAX_INCIDENTS": "5"}, clear=False):
            service = IncidentService(simulator, RoutingService(simulator))
            created = [service.create("main_stage", Priority.LOW, f"Incident {index}").id for index in range(8)]
        self.assertEqual(len(service._incidents), 5)
        self.assertEqual(list(service._incidents), created[3:])


class BroadcastResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failing_client_does_not_stop_the_fan_out(self) -> None:
        class FailingSocket:
            async def accept(self) -> None:
                pass

            async def send_json(self, event: dict[str, object]) -> None:
                raise ConnectionResetError("client is gone")

        class WorkingSocket:
            def __init__(self) -> None:
                self.events: list[dict[str, object]] = []

            async def accept(self) -> None:
                pass

            async def send_json(self, event: dict[str, object]) -> None:
                self.events.append(event)

        broadcaster = DashboardBroadcaster()
        failing, working = FailingSocket(), WorkingSocket()
        await broadcaster.connect(failing)  # type: ignore[arg-type]
        await broadcaster.connect(working)  # type: ignore[arg-type]

        event = {"type": "simulation_state"}
        await broadcaster.broadcast(event)

        self.assertEqual(working.events, [event])
        self.assertEqual(broadcaster.connection_count, 1)


class WritePathProtectionTests(unittest.TestCase):
    """The public deployment must bound anonymous writes without breaking reads."""

    def setUp(self) -> None:
        security.reset_rate_limits()

    def tearDown(self) -> None:
        security.reset_rate_limits()

    def test_configured_token_is_required_for_writes_but_not_reads(self) -> None:
        with patch.dict(os.environ, {"RESCUEROUTE_API_TOKEN": "demo-secret"}, clear=False):
            with TestClient(app) as client:
                self.assertEqual(client.get("/simulation/state").status_code, 200)
                rejected = client.post("/simulation/advance", json={"minutes": 1})
                accepted = client.post(
                    "/simulation/advance",
                    json={"minutes": 1},
                    headers={"Authorization": "Bearer demo-secret"},
                )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)

    def test_write_burst_is_rate_limited(self) -> None:
        with patch.object(security.SIMULATION_LIMITER, "burst", 3), \
             patch.object(security.SIMULATION_LIMITER, "per_minute", 1):
            with TestClient(app) as client:
                codes = [
                    client.post("/simulation/scenarios", json={"scenario": "normal"}).status_code
                    for _ in range(5)
                ]
                readable = client.get("/simulation/state").status_code
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3:], [429, 429])
        self.assertEqual(readable, 200)

    def test_rate_limit_can_be_disabled_for_local_development(self) -> None:
        with patch.dict(os.environ, {"RESCUEROUTE_RATE_LIMIT_ENABLED": "false"}, clear=False), \
             patch.object(security.SIMULATION_LIMITER, "burst", 1):
            with TestClient(app) as client:
                codes = [
                    client.post("/simulation/scenarios", json={"scenario": "normal"}).status_code
                    for _ in range(3)
                ]
        self.assertEqual(codes, [200, 200, 200])


class VenueSwitchEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        security.reset_rate_limits()

    def tearDown(self) -> None:
        security.reset_rate_limits()
        default_workspace().camara.configure("stadium_match", seed=42, crowd_pattern="gate_surge")

    def test_switching_venue_cancels_the_active_incident_over_the_socket(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False), \
             patch.object(default_workspace().camara.nokia, "enabled", False), TestClient(app) as client:
            client.post("/simulation/configure", json={
                "template": "stadium_match", "seed": 42, "crowd_pattern": "balanced"
            })
            incident = client.post("/incidents", json={
                "location": "main_stage", "priority": "high", "description": "Venue switch"
            }).json()
            client.post(f"/incidents/{incident['id']}/dispatch")

            with client.websocket_connect("/ws/dashboard") as socket:
                socket.receive_json()  # Initial snapshot.
                client.post("/simulation/configure", json={
                    "template": "pilgrimage_flow", "seed": 42, "crowd_pattern": "balanced"
                })
                cancelled = socket.receive_json()

            status_after = client.get(f"/incidents/{incident['id']}").json()["status"]
            redispatch = client.post(f"/incidents/{incident['id']}/recalculate-route")

        self.assertEqual(cancelled["type"], "incidents_cancelled")
        self.assertIn(incident["id"], cancelled["incident_ids"])
        self.assertEqual(status_after, "cancelled")
        self.assertEqual(redispatch.status_code, 409)

    def test_disconnecting_a_dashboard_releases_its_broadcast_slot(self) -> None:
        realtime = default_workspace().realtime
        before = realtime.connection_count
        with TestClient(app) as client:
            with client.websocket_connect("/ws/dashboard") as socket:
                socket.receive_json()
        self.assertEqual(realtime.connection_count, before)


if __name__ == "__main__":
    unittest.main()
