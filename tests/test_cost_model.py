"""Regression tests for the four-term ETA model.

Each test pins a property the documented formula claims but the previous
implementation did not deliver.
"""

import unittest

from app.models import Priority
from app.services.camara_simulator import CamaraSimulator
from app.services.incident_service import IncidentService
from app.services.routing import RoutingService
from app.venue import TEMPLATES


def clear_venue(template: str = "stadium_match") -> CamaraSimulator:
    simulator = CamaraSimulator()
    simulator.configure(template, seed=42, crowd_pattern="balanced")
    for zone in list(simulator.state().zone_congestion):
        simulator.update_congestion(zone, 0.0)
    return simulator


class NetworkTermTests(unittest.TestCase):
    def test_operator_load_costs_time_in_an_empty_venue(self) -> None:
        """The network term must not be a restatement of this corridor's crowd."""
        simulator = clear_venue()
        simulator._network_load, simulator._network_source = 1.0, "live_nokia"
        loaded = RoutingService(simulator).shortest_route("ambulance_bay", "main_stage")

        simulator._network_load = 0.0
        quiet = RoutingService(simulator).shortest_route("ambulance_bay", "main_stage")

        self.assertEqual(loaded.crowd_penalty_seconds, 0)
        self.assertGreater(loaded.network_penalty_seconds, 0)
        self.assertEqual(quiet.network_penalty_seconds, 0)
        self.assertGreater(loaded.eta_seconds, quiet.eta_seconds)

    def test_local_density_still_raises_network_pressure(self) -> None:
        simulator = clear_venue()
        self.assertGreater(simulator.network_pressure(0.9), simulator.network_pressure(0.1))

    def test_deterministic_demo_reports_a_non_zero_network_load(self) -> None:
        """Without an operator feed the load used to stay pinned at zero."""
        simulator = CamaraSimulator()
        simulator.refresh_network_congestion("medic_alpha")
        state = simulator.state()
        self.assertEqual(state.network_source, "simulation")
        self.assertGreater(state.network_load, 0)


class QosTermTests(unittest.TestCase):
    def test_qod_relieves_the_decision_that_requested_it(self) -> None:
        simulator = CamaraSimulator()
        service = IncidentService(simulator, RoutingService(simulator))
        with_qos = service.create("main_stage", Priority.CRITICAL, "with qos")
        _, relieved = service.dispatch(with_qos.id, agent_tools=["qos_on_demand"])

        plain_simulator = CamaraSimulator()
        plain_service = IncidentService(plain_simulator, RoutingService(plain_simulator))
        without_qos = plain_service.create("main_stage", Priority.LOW, "no qos")
        _, full_price = plain_service.dispatch(without_qos.id, agent_tools=["device_status"])

        self.assertTrue(simulator.state().qos_active)
        self.assertFalse(plain_simulator.state().qos_active)
        self.assertLess(
            relieved.cost_breakdown.network_penalty_seconds,
            full_price.cost_breakdown.network_penalty_seconds,
        )

    def test_qos_relief_factor_is_applied_to_edge_cost(self) -> None:
        simulator = CamaraSimulator()
        routing = RoutingService(simulator)
        self.assertEqual(simulator.qos_relief(), 1.0)
        simulator.activate_qos("medic_alpha", "incident")
        self.assertLess(simulator.qos_relief(), 1.0)


class AccessTermTests(unittest.TestCase):
    def test_access_penalty_is_part_of_every_decision(self) -> None:
        simulator = CamaraSimulator()
        service = IncidentService(simulator, RoutingService(simulator))
        incident = service.create("main_stage", Priority.HIGH, "access")
        _, decision = service.dispatch(incident.id)
        self.assertGreater(decision.cost_breakdown.access_penalty_seconds, 0)

    def test_restricted_corridor_costs_access_time_without_removing_the_edge(self) -> None:
        simulator = CamaraSimulator()
        before = RoutingService(simulator).shortest_route("ambulance_bay", "main_stage")
        simulator.set_corridor_status("gate_c", "south_corridor", closed=False, restricted=True)
        after = RoutingService(simulator).shortest_route("ambulance_bay", "main_stage")

        self.assertEqual(simulator.state().restricted_corridors, ["gate_c <-> south_corridor"])
        self.assertEqual(simulator.state().closed_corridors, [])
        self.assertGreater(after.access_penalty_seconds, before.access_penalty_seconds)

    def test_restriction_is_cleared_when_the_corridor_reopens(self) -> None:
        simulator = CamaraSimulator()
        simulator.set_corridor_status("gate_c", "south_corridor", closed=False, restricted=True)
        state = simulator.set_corridor_status("gate_c", "south_corridor", closed=False)
        self.assertEqual(state.restricted_corridors, [])

    def test_restricted_corridors_survive_a_restart(self) -> None:
        simulator = CamaraSimulator()
        snapshot = simulator.set_corridor_status("gate_c", "south_corridor", closed=False, restricted=True)
        restored = CamaraSimulator().restore_state(snapshot)
        self.assertEqual(restored.restricted_corridors, ["gate_c <-> south_corridor"])


class BreakdownTests(unittest.TestCase):
    def test_breakdown_always_adds_up_to_the_total(self) -> None:
        for seed in range(120):
            simulator = CamaraSimulator()
            simulator.configure("stadium_match", seed=seed, crowd_pattern="balanced")
            route = RoutingService(simulator).shortest_route("ambulance_bay", "main_stage")
            parts = (
                route.distance_seconds + route.crowd_penalty_seconds
                + route.network_penalty_seconds + route.access_penalty_seconds
            )
            self.assertEqual(parts, route.eta_seconds, f"breakdown does not sum for seed {seed}")


class CrowdResponseTests(unittest.TestCase):
    def test_walking_time_collapses_rather_than_degrading_linearly(self) -> None:
        moderate = RoutingService.crowd_multiplier(0.30)
        severe = RoutingService.crowd_multiplier(0.85)
        # A linear term made a crush only ~2.8x worse than a moderate crowd.
        self.assertGreater(severe / moderate, 8)

    def test_a_shorter_route_through_a_crush_is_rejected(self) -> None:
        simulator = CamaraSimulator()  # gate_surge: west_zone in front of gate B is at 0.85
        service = IncidentService(simulator, RoutingService(simulator))
        incident = service.create("main_stage", Priority.CRITICAL, "crush")
        _, decision = service.dispatch(incident.id)
        gate_b = next(option for option in decision.gate_options if option.gate == "gate_b")

        self.assertEqual(decision.selected_gate, "gate_c")
        self.assertLess(gate_b.route_distance_m, decision.route_distance_m)
        self.assertGreater(gate_b.eta_seconds, decision.estimated_arrival_seconds)
        self.assertIn("shorter", decision.explanation)


class GateComparisonTests(unittest.TestCase):
    def test_a_gate_option_enters_through_that_gate_only(self) -> None:
        simulator = CamaraSimulator()
        routing = RoutingService(simulator)
        for gate in sorted(simulator.gates):
            route = routing.route_via_gate("ambulance_bay", gate, "main_stage")
            other_gates = simulator.gates - {gate}
            self.assertFalse(
                other_gates.intersection(route.nodes),
                f"option for {gate} re-entered through {other_gates.intersection(route.nodes)}",
            )

    def test_the_selected_option_matches_the_decision_it_explains(self) -> None:
        simulator = CamaraSimulator()
        service = IncidentService(simulator, RoutingService(simulator))
        incident = service.create("main_stage", Priority.HIGH, "consistency")
        _, decision = service.dispatch(incident.id)
        selected = next(
            option for option in decision.gate_options if option.gate == decision.selected_gate
        )
        self.assertEqual(selected.eta_seconds, decision.estimated_arrival_seconds)
        self.assertEqual(selected.route_distance_m, decision.route_distance_m)


class CrowdPatternTests(unittest.TestCase):
    """Every pattern must load the zones it is named after."""

    EXPECTED = {
        ("stadium_match", "gate_surge"): {"north_zone", "west_zone"},
        ("stadium_match", "stage_cluster"): {"central_zone", "south_zone"},
        ("music_festival", "gate_surge"): {"entry_zone", "east_lane"},
        ("music_festival", "stage_cluster"): {"stage_zone", "food_zone"},
        ("pilgrimage_flow", "gate_surge"): {"western_courtyard", "northern_expansion"},
        ("pilgrimage_flow", "stage_cluster"): {"mataf_zone", "masaa_zone"},
    }

    def test_patterns_load_the_zones_they_name(self) -> None:
        for (template, pattern), expected in self.EXPECTED.items():
            state = CamaraSimulator().configure(template, seed=42, crowd_pattern=pattern)
            busiest = {
                zone for zone, _ in sorted(state.zone_congestion.items(), key=lambda kv: -kv[1])[:2]
            }
            self.assertEqual(busiest, expected, f"{template}/{pattern} loaded {busiest}")

    def test_every_template_declares_every_pattern(self) -> None:
        for template in TEMPLATES.values():
            self.assertEqual(
                set(template.crowd_bias), {"balanced", "gate_surge", "stage_cluster"}, template.key
            )


if __name__ == "__main__":
    unittest.main()
