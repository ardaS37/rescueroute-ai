import os
import tempfile
import unittest
from unittest.mock import patch

from app.models import Priority
from app.services.camara_simulator import CamaraSimulator
from app.services.emergency_agent import EmergencyAgent
from app.services.incident_service import IncidentService
from app.services.persistence import SQLiteStore
from app.services.routing import RoutingService


class RoutingTests(unittest.TestCase):
    def test_high_north_congestion_avoids_gate_a(self) -> None:
        route = RoutingService(CamaraSimulator()).shortest_route("ambulance_bay", "main_stage")

        self.assertIn("gate_c", route.nodes)
        self.assertNotIn("gate_a", route.nodes)
        self.assertGreater(route.eta_seconds, 0)

    def test_all_templates_can_route_from_ambulance_bay(self) -> None:
        camara = CamaraSimulator()
        for template in ("stadium_match", "music_festival", "pilgrimage_flow"):
            camara.configure(template, seed=7, crowd_pattern="balanced")
            destination = "main_stage" if template != "pilgrimage_flow" else "kaaba_tawaf"
            route = RoutingService(camara).shortest_route("ambulance_bay", destination)
            self.assertTrue(any(node in camara.gates for node in route.nodes[1:]))

    def test_same_seed_produces_same_crowd_distribution(self) -> None:
        first = CamaraSimulator().configure("music_festival", seed=99, crowd_pattern="stage_cluster")
        second = CamaraSimulator().configure("music_festival", seed=99, crowd_pattern="stage_cluster")
        self.assertEqual(first.zone_congestion, second.zone_congestion)

    def test_hajj_tawaf_surge_uses_named_haram_gates(self) -> None:
        camara = CamaraSimulator()
        state = camara.apply_scenario("hajj_tawaf_surge")
        route = RoutingService(camara).shortest_route("ambulance_bay", "kaaba_tawaf")

        self.assertEqual(state.template, "pilgrimage_flow")
        self.assertEqual(state.zone_congestion["mataf_zone"], 0.95)
        self.assertIn("king_abdulaziz_gate", camara.gates)
        self.assertTrue(any(node in camara.gates for node in route.nodes))

    def test_langgraph_agent_uses_safe_fallback_and_records_trace(self) -> None:
        camara = CamaraSimulator()
        service = IncidentService(camara, RoutingService(camara))
        incident = service.create("main_stage", Priority.CRITICAL, "Agent test")
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            agent = EmergencyAgent(service)
            response = agent.dispatch(incident.id)
        trace = agent.trace(incident.id)
        self.assertEqual(trace.model_source, "deterministic_fallback")
        self.assertIn("congestion_insights", trace.tool_plan)
        self.assertIn("qos_on_demand", trace.tool_plan)
        self.assertIn("AI Agent observation", response.decision.api_calls[0])
        self.assertIn("AI Agent plan (recorded fallback)", response.decision.api_calls[1])

    def test_closed_corridor_is_excluded_from_route(self) -> None:
        camara = CamaraSimulator()
        camara.set_corridor_status("south_corridor", "first_aid", closed=True)
        route = RoutingService(camara).shortest_route("ambulance_bay", "main_stage")
        self.assertNotIn("first_aid", route.nodes)

    def test_live_congestion_can_change_route(self) -> None:
        camara = CamaraSimulator()
        before = RoutingService(camara).shortest_route("ambulance_bay", "main_stage")
        camara.update_congestion("south_zone", 0.98)
        after = RoutingService(camara).shortest_route("ambulance_bay", "main_stage")
        self.assertNotEqual(before.nodes, after.nodes)

    def test_geofence_arrival_resolves_incident(self) -> None:
        camara = CamaraSimulator()
        service = IncidentService(camara, RoutingService(camara))
        incident = service.create("main_stage", Priority.CRITICAL, "Test")
        _, decision = service.dispatch(incident.id)
        service.record_geofence_event(incident.id, decision.team_id, decision.selected_gate, "entered_selected_gate")
        progress = service.record_geofence_event(incident.id, decision.team_id, "main_stage", "reached_patient")
        self.assertTrue(progress.completed)
        self.assertEqual(service.get(incident.id).status.value, "resolved")

    def test_nokia_geofence_callback_marks_selected_gate(self) -> None:
        camara = CamaraSimulator()
        service = IncidentService(camara, RoutingService(camara))
        incident = service.create("main_stage", Priority.HIGH, "Nokia callback test")
        _, decision = service.dispatch(incident.id)
        service._nokia_geofences["sub-demo-1"] = (
            incident.id, decision.team_id, decision.selected_gate
        )

        progress, status = service.process_nokia_geofence_callback({
            "data": {"subscriptionId": "sub-demo-1"},
            "type": "org.camaraproject.geofencing-subscriptions.v0.area-entered",
        })

        self.assertIsNotNone(progress)
        self.assertIn("processed", status)
        self.assertEqual(progress.last_location, decision.selected_gate)
        self.assertEqual(progress.events[-1].event_type, "entered_selected_gate")

    def test_recorded_gate_a_busy_scenario_prefers_another_gate(self) -> None:
        camara = CamaraSimulator()
        camara.apply_scenario("gate_a_busy")
        route = RoutingService(camara).shortest_route("ambulance_bay", "main_stage")
        self.assertNotIn("gate_a", route.nodes)
        self.assertEqual(camara.state().active_scenario, "gate_a_busy")

    def test_primary_team_unavailable_assigns_backup_team(self) -> None:
        camara = CamaraSimulator()
        camara.apply_scenario("primary_team_unavailable")
        service = IncidentService(camara, RoutingService(camara))
        incident = service.create("main_stage", Priority.CRITICAL, "Fallback test")
        _, decision = service.dispatch(incident.id)
        self.assertEqual(decision.team_id, "medic_bravo")
        self.assertIn("Device Status: medic_bravo", decision.api_calls)

    def test_decision_exposes_cost_breakdown_and_gate_options(self) -> None:
        camara = CamaraSimulator()
        service = IncidentService(camara, RoutingService(camara))
        incident = service.create("main_stage", Priority.HIGH, "Cost test")
        _, decision = service.dispatch(incident.id)
        cost = decision.cost_breakdown
        self.assertEqual(cost.total_seconds, decision.estimated_arrival_seconds)
        self.assertGreater(cost.distance_seconds, 0)
        self.assertEqual(len(decision.gate_options), len(camara.gates))

    def test_dispatch_and_reroute_are_recorded_in_decision_history(self) -> None:
        camara = CamaraSimulator()
        service = IncidentService(camara, RoutingService(camara))
        incident = service.create("main_stage", Priority.HIGH, "History test")
        _, first = service.dispatch(incident.id)
        camara.set_corridor_status("gate_c", "south_corridor", closed=True)
        _, second = service.dispatch(incident.id, trigger="Gate C corridor closed")
        history = service.get_history(incident.id)

        self.assertEqual(len(history.entries), 2)
        self.assertEqual(history.entries[0].event_type, "dispatch")
        self.assertEqual(history.entries[1].event_type, "reroute")
        self.assertEqual(history.entries[1].trigger, "Gate C corridor closed")
        self.assertEqual(history.entries[1].previous_route, first.route)
        self.assertEqual(history.entries[1].route, second.route)

    def test_active_incident_filter_only_selects_routes_using_changed_zone(self) -> None:
        camara = CamaraSimulator()
        service = IncidentService(camara, RoutingService(camara))
        incident = service.create("main_stage", Priority.HIGH, "Automatic reroute test")
        _, decision = service.dispatch(incident.id)
        changed_zone = next(segment.zone for segment in decision.segments if segment.zone)

        self.assertEqual(
            service.affected_active_incidents(zones={changed_zone}), [incident.id]
        )
        self.assertEqual(service.affected_active_incidents(zones={"not_a_venue_zone"}), [])

    def test_incident_history_and_progress_survive_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "rescueroute.db")
            camara = CamaraSimulator()
            first_store = SQLiteStore(database_path)
            service = IncidentService(camara, RoutingService(camara), first_store)
            incident = service.create("main_stage", Priority.HIGH, "Persistence test")
            _, decision = service.dispatch(incident.id)
            service.record_geofence_event(
                incident.id, decision.team_id, decision.selected_gate, "entered_selected_gate"
            )

            restored_store = SQLiteStore(database_path)
            restored = IncidentService(camara, RoutingService(camara), restored_store)
            self.assertEqual(restored.get(incident.id).description, "Persistence test")
            self.assertEqual(len(restored.get_history(incident.id).entries), 1)
            self.assertEqual(restored.get_progress(incident.id).last_location, decision.selected_gate)
            restored_store.close()
            first_store.close()


if __name__ == "__main__":
    unittest.main()
