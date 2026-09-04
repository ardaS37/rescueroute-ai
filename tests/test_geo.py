"""Regression tests for the venue's geographic anchoring.

The graph used to carry pixels only, so a Location Retrieval fix could not be
mapped to a node and the Geofencing area was a fixed circle in Doha regardless
of which venue or gate the team had been routed to.
"""

import unittest
from statistics import median
from unittest.mock import patch

from app.services.camara_simulator import CamaraSimulator
from app.venue import TEMPLATES, distance_metres


class VenueGeographyTests(unittest.TestCase):
    def test_every_template_is_anchored_on_the_map(self) -> None:
        for key, template in TEMPLATES.items():
            self.assertIsNotNone(template.geo, key)
            for node in template.graph:
                self.assertIsNotNone(template.coordinates(node), f"{key}/{node}")

    def test_the_hajj_venue_sits_on_masjid_al_haram(self) -> None:
        kaaba = TEMPLATES["pilgrimage_flow"].coordinates("kaaba_tawaf")
        assert kaaba is not None
        # The real Kaaba, to within the simplified model's own footprint.
        self.assertLess(distance_metres(kaaba, (21.422510, 39.826160)), 200)

    def test_the_scale_is_calibrated_against_the_graph(self) -> None:
        """The canvas is schematic, so the scale is fixed on the median corridor."""
        for key, template in TEMPLATES.items():
            ratios, seen = [], set()
            for source, edges in template.graph.items():
                for edge in edges:
                    pair = tuple(sorted((source, edge.destination)))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    first, second = template.coordinates(source), template.coordinates(edge.destination)
                    assert first is not None and second is not None
                    straight = distance_metres(first, second)
                    if straight:
                        ratios.append(edge.distance_m / straight)
            self.assertAlmostEqual(median(ratios), 1.0, delta=0.15, msg=key)

    def test_venue_layout_publishes_coordinates(self) -> None:
        layout = CamaraSimulator().layout()
        self.assertTrue(all(node.latitude and node.longitude for node in layout.nodes))


class LocationRetrievalTests(unittest.TestCase):
    def test_a_fix_inside_the_venue_snaps_to_the_nearest_node(self) -> None:
        camara = CamaraSimulator()
        position = camara.coordinates("first_aid")
        assert position is not None
        node, metres = camara.nearest_node(*position)
        self.assertEqual(node, "first_aid")
        self.assertLess(metres, 1)

    def test_a_distant_fix_falls_back_to_the_recorded_position(self) -> None:
        camara = CamaraSimulator()
        budapest = {"area": {"center": {"latitude": 47.486276, "longitude": 19.079156}}}
        with patch.object(camara.nokia, "enabled", True), \
             patch.object(camara.nokia, "location", return_value=budapest):
            resolved = camara.get_team_location("medic_alpha", "ambulance_bay")
        self.assertEqual(resolved, "ambulance_bay")
        self.assertIn("outside the venue", " ".join(camara.drain_live_api_calls()))

    def test_a_venue_fix_overrides_the_recorded_position(self) -> None:
        camara = CamaraSimulator()
        position = camara.coordinates("central_plaza")
        assert position is not None
        response = {"area": {"center": {"latitude": position[0], "longitude": position[1]}}}
        with patch.object(camara.nokia, "enabled", True), \
             patch.object(camara.nokia, "location", return_value=response):
            resolved = camara.get_team_location("medic_alpha", "ambulance_bay")
        self.assertEqual(resolved, "central_plaza")

    def test_no_fix_can_resolve_to_two_nodes(self) -> None:
        """The snap radius must stay under half of the closest node pair."""
        camara = CamaraSimulator()
        for key, template in TEMPLATES.items():
            camara.configure(key, seed=42, crowd_pattern="balanced")
            positions = [p for node in template.graph if (p := template.coordinates(node))]
            closest = min(
                distance_metres(a, b)
                for index, a in enumerate(positions)
                for b in positions[index + 1:]
            )
            self.assertLess(camara.location_snap_radius_m(), closest / 2, key)
            self.assertGreater(camara.location_snap_radius_m(), 0, key)


class GeofenceAreaTests(unittest.TestCase):
    def test_the_subscription_watches_the_selected_gate(self) -> None:
        camara = CamaraSimulator()
        recorded: dict[str, object] = {}

        def capture(phone: str, latitude: float, longitude: float, radius_m: int) -> str:
            recorded.update(latitude=latitude, longitude=longitude, radius=radius_m)
            return "sub-1"

        with patch.object(camara.nokia, "enabled", True), \
             patch.object(camara.nokia, "create_geofence_subscription", side_effect=capture):
            message, subscription = camara.subscribe_geofence("medic_alpha", "gate_c")

        gate = camara.coordinates("gate_c")
        assert gate is not None
        self.assertEqual(subscription, "sub-1")
        self.assertEqual((recorded["latitude"], recorded["longitude"]), gate)
        self.assertEqual(recorded["radius"], TEMPLATES["stadium_match"].geo.gate_radius_m)
        self.assertIn("gate_c", message or "")

    def test_each_gate_gets_its_own_area(self) -> None:
        camara = CamaraSimulator()
        areas = {gate: camara.coordinates(gate) for gate in camara.gates}
        self.assertEqual(len(set(areas.values())), len(areas))

    def test_gate_areas_do_not_overlap(self) -> None:
        for key, template in TEMPLATES.items():
            assert template.geo is not None
            gates = sorted(template.gates)
            for index, first in enumerate(gates):
                for second in gates[index + 1:]:
                    a, b = template.coordinates(first), template.coordinates(second)
                    assert a is not None and b is not None
                    self.assertGreater(
                        distance_metres(a, b), template.geo.gate_radius_m * 2,
                        f"{key}: {first} and {second} geofences overlap",
                    )


class DeviceStatusTests(unittest.TestCase):
    """Nokia reports the bearer alongside the state, e.g. CONNECTED_DATA."""

    def test_bearer_qualified_states_are_understood(self) -> None:
        interpret = CamaraSimulator._interpret_connectivity
        for value in ("CONNECTED", "CONNECTED_DATA", "CONNECTED_SMS", "REACHABLE"):
            self.assertIs(interpret(value), True, value)
        for value in ("NOT_CONNECTED", "DISCONNECTED", "UNREACHABLE"):
            self.assertIs(interpret(value), False, value)
        self.assertIsNone(interpret("SOMETHING_ELSE"))

    def test_a_live_check_updates_the_roster(self) -> None:
        camara = CamaraSimulator()
        with patch.object(camara.nokia, "enabled", True),              patch.object(camara.nokia, "connectivity", return_value="NOT_CONNECTED"):
            self.assertFalse(camara.check_device_status("medic_charlie"))
        self.assertFalse(camara.device_status("medic_charlie"))

    def test_a_recorded_scenario_survives_a_live_lookup(self) -> None:
        camara = CamaraSimulator()
        camara.apply_scenario("primary_team_unavailable")
        with patch.object(camara.nokia, "enabled", True),              patch.object(camara.nokia, "connectivity", return_value="CONNECTED_DATA") as live:
            self.assertFalse(camara.check_device_status("medic_alpha"))
            self.assertTrue(camara.check_device_status("medic_bravo"))
        live.assert_called_once()  # only the team the scenario did not pin
        self.assertIn("recorded scenario", " ".join(camara.drain_live_api_calls()))

    def test_every_team_uses_a_provisioned_simulator_number(self) -> None:
        for template in TEMPLATES.values():
            for team in template.teams:
                self.assertNotEqual(team.phone_number, "+99999991002", team.id)

    def test_an_unrecognised_state_leaves_the_roster_alone(self) -> None:
        camara = CamaraSimulator()
        with patch.object(camara.nokia, "enabled", True),              patch.object(camara.nokia, "connectivity", return_value="MAINTENANCE"):
            self.assertTrue(camara.check_device_status("medic_alpha"))
        self.assertIn("recorded reachability used", " ".join(camara.drain_live_api_calls()))


if __name__ == "__main__":
    unittest.main()
