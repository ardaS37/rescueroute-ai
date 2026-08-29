"""Replaceable simulator for CAMARA-aligned network signals.

The functions intentionally mirror the kind of data the production adapter will
request from Nokia Network-as-Code / an operator gateway.
"""

from __future__ import annotations

import random

from app.models import SimulationState, VenueEdge, VenueLayout, VenueNode, VenueTemplateSummary
from app.services.nokia_nac import NokiaNaCClient, NokiaNaCError
from app.venue import Edge, TEMPLATES, get_template


class CamaraSimulator:
    """Venue fallback state enriched by Nokia NaC signals when enabled."""

    team_phone_numbers = {"medic_alpha": "+99999991000", "medic_bravo": "+99999991001"}

    def __init__(self, nokia: NokiaNaCClient | None = None) -> None:
        self.nokia = nokia or NokiaNaCClient()
        self._live_api_calls: list[str] = []
        self._network_load = 0.0
        self._network_source = "simulation"
        self._qos_active = False
        self.configure("stadium_match", seed=42, crowd_pattern="gate_surge")

    def configure(self, template_key: str, seed: int, crowd_pattern: str) -> SimulationState:
        self._template = get_template(template_key)
        self._seed = seed
        self._crowd_pattern = crowd_pattern
        self._minutes = 0
        self._rng = random.Random(seed)
        self._closed_corridors: set[tuple[str, str]] = set()
        self._device_status = {"medic_alpha": True, "medic_bravo": True}
        self._active_scenario = "custom"
        self._network_load = 0.0
        self._network_source = "simulation"
        self._qos_active = False
        self._live_api_calls = []
        self._generate_crowd()
        return self.state()

    def apply_scenario(self, scenario: str) -> SimulationState:
        """Apply a recorded fallback scenario without relying on live network APIs."""
        if scenario == "normal":
            self.configure("stadium_match", seed=42, crowd_pattern="balanced")
        elif scenario == "gate_a_busy":
            self.configure("stadium_match", seed=42, crowd_pattern="balanced")
            self.update_congestion("north_zone", 0.95)
            self.update_congestion("west_zone", 0.78)
        elif scenario == "corridor_closed":
            self.configure("stadium_match", seed=42, crowd_pattern="balanced")
            self.set_corridor_status("gate_c", "south_corridor", closed=True)
        elif scenario == "primary_team_unavailable":
            self.configure("stadium_match", seed=42, crowd_pattern="balanced")
            self._device_status["medic_alpha"] = False
        elif scenario == "hajj_tawaf_surge":
            self.configure("pilgrimage_flow", seed=786, crowd_pattern="balanced")
            self.update_congestion("mataf_zone", 0.95)
            self.update_congestion("western_courtyard", 0.78)
        elif scenario == "hajj_masaa_congestion":
            self.configure("pilgrimage_flow", seed=786, crowd_pattern="balanced")
            self.update_congestion("masaa_zone", 0.95)
        else:
            raise ValueError(f"Unknown recorded scenario '{scenario}'.")
        self._active_scenario = scenario
        return self.state()

    def _generate_crowd(self) -> None:
        biases = {
            "balanced": {},
            "gate_surge": {self._template.zones[0]: 0.40, self._template.zones[1]: 0.35},
            "stage_cluster": {self._template.zones[-2]: 0.45, self._template.zones[-1]: 0.30},
        }[self._crowd_pattern]
        self._zone_congestion, self._crowd_distribution = {}, {}
        for zone in self._template.zones:
            density = min(0.95, max(0.08, 0.28 + biases.get(zone, 0) + self._rng.uniform(-0.12, 0.12)))
            self._zone_congestion[zone] = round(density, 2)
            self._crowd_distribution[zone] = round(density * 1_200)
        if self._template.key == "stadium_match" and self._crowd_pattern == "gate_surge":
            self._zone_congestion.update({"north_zone": 0.85, "west_zone": 0.85, "south_zone": 0.15})

    def advance(self, minutes: int) -> SimulationState:
        self._minutes += minutes
        for zone, current in self._zone_congestion.items():
            self._zone_congestion[zone] = round(min(0.98, max(0.03, current + self._rng.uniform(-0.18, 0.18) * min(minutes / 10, 1))), 2)
            self._crowd_distribution[zone] = round(self._zone_congestion[zone] * 1_200)
        return self.state()

    def update_congestion(self, zone: str, density: float) -> SimulationState:
        if zone not in self._zone_congestion:
            raise ValueError(f"Unknown zone '{zone}' for {self._template.key}.")
        self._zone_congestion[zone] = round(density, 2)
        self._crowd_distribution[zone] = round(density * 1_200)
        return self.state()

    def set_corridor_status(self, source: str, destination: str, closed: bool) -> SimulationState:
        if destination not in {edge.destination for edge in self._template.graph.get(source, ())}:
            raise ValueError(f"'{source}' and '{destination}' are not directly connected.")
        corridor = tuple(sorted((source, destination)))
        if closed:
            self._closed_corridors.add(corridor)
        else:
            self._closed_corridors.discard(corridor)
        return self.state()

    def state(self) -> SimulationState:
        return SimulationState(template=self._template.key, title=self._template.title, seed=self._seed,
            crowd_pattern=self._crowd_pattern, simulated_minutes=self._minutes,
            zone_congestion=self._zone_congestion, crowd_distribution=self._crowd_distribution,
            closed_corridors=[" <-> ".join(corridor) for corridor in sorted(self._closed_corridors)],
            active_scenario=self._active_scenario, device_status=self._device_status,
            network_load=self._network_load, network_source=self._network_source,
            qos_active=self._qos_active)

    def templates(self) -> list[VenueTemplateSummary]:
        return [VenueTemplateSummary(key=t.key, title=t.title, description=t.description, gates=sorted(t.gates),
            locations=sorted(t.graph), zones=list(t.zones)) for t in TEMPLATES.values()]

    def layout(self) -> VenueLayout:
        edges: list[VenueEdge] = []
        seen: set[tuple[str, str]] = set()
        for source, neighbors in self._template.graph.items():
            for edge in neighbors:
                key = tuple(sorted((source, edge.destination)))
                if key not in seen:
                    seen.add(key)
                    edges.append(VenueEdge(source=source, destination=edge.destination,
                        distance_m=edge.distance_m, zone=edge.zone))
        return VenueLayout(
            template=self._template.key,
            title=self._template.title,
            nodes=[VenueNode(id=node, x=position[0], y=position[1],
                kind="gate" if node in self._template.gates else "landmark")
                for node, position in self._template.positions.items()],
            edges=edges,
        )

    @property
    def gates(self) -> frozenset[str]:
        return self._template.gates

    def is_known_node(self, node: str) -> bool:
        return node in self._template.graph

    def neighbors(self, node: str) -> tuple[Edge, ...]:
        return tuple(edge for edge in self._template.graph.get(node, ())
            if tuple(sorted((node, edge.destination))) not in self._closed_corridors)

    def get_congestion(self, zone: str | None) -> float:
        """Simulates CAMARA Congestion Insights for a venue zone."""
        return self._zone_congestion.get(zone or "", 0.0)

    def refresh_network_congestion(self, team_id: str) -> None:
        """Fetch one live network signal per decision, never per graph edge."""
        if not self.nokia.enabled:
            return
        try:
            response = self.nokia.congestion(self.team_phone_numbers[team_id])
            self._network_load = self._extract_fraction(response)
            self._network_source = "live_nokia"
            self._live_api_calls.append(
                f"Congestion Insights (Nokia NaC): network load {self._network_load:.0%}"
            )
        except NokiaNaCError as error:
            self._network_source = "recorded_fallback"
            self._live_api_calls.append(
                f"Congestion Insights: Nokia unavailable ({error}) — recorded network fallback used"
            )

    def network_penalty_multiplier(self) -> float:
        """Live cellular load raises routing network cost; QoD offsets that cost."""
        multiplier = 1.0 + self._network_load * 0.75
        return multiplier * (0.40 if self._qos_active else 1.0)

    def drain_live_api_calls(self) -> list[str]:
        calls, self._live_api_calls = self._live_api_calls, []
        return calls

    def get_team_location(self, team_id: str) -> str:
        """Simulates Location Retrieval for an authorised response team."""
        locations = {"medic_alpha": "ambulance_bay", "medic_bravo": "ambulance_bay"}
        if self.nokia.enabled:
            try:
                self.nokia.location(self.team_phone_numbers[team_id])
                self._live_api_calls.append(
                    f"Location Retrieval (Nokia NaC): {team_id} position verified → {locations[team_id]}"
                )
            except NokiaNaCError as error:
                self._live_api_calls.append(
                    f"Location Retrieval: Nokia unavailable ({error}) — recorded venue position used"
                )
        return locations[team_id]

    def check_device_status(self, team_id: str) -> bool:
        """Simulates CAMARA Device Status from recorded fallback data."""
        if self.nokia.enabled:
            try:
                connectivity = self.nokia.connectivity(self.team_phone_numbers[team_id]).upper()
                reachable = connectivity in {"CONNECTED", "REACHABLE", "TRUE"}
                if connectivity not in {"CONNECTED", "REACHABLE", "TRUE", "NOT_CONNECTED", "DISCONNECTED", "FALSE"}:
                    reachable = self._device_status.get(team_id, False)
                    self._live_api_calls.append(
                        f"Device Status (Nokia NaC): {team_id} response was ambiguous; recorded reachability used"
                    )
                self._live_api_calls.append(
                    f"Device Status (Nokia NaC): {team_id} → {connectivity.lower()}"
                )
                return reachable
            except NokiaNaCError as error:
                self._live_api_calls.append(
                    f"Device Status: Nokia unavailable ({error}) — recorded reachability used"
                )
        return self._device_status.get(team_id, False)

    def activate_qos(self, team_id: str, incident_id: str) -> str:
        """Simulates QoS on Demand activation for critical communication."""
        if self.nokia.enabled:
            try:
                session_id = self.nokia.create_qod_session(self.team_phone_numbers[team_id])
                self._qos_active = True
                return f"QoS on Demand (Nokia NaC) activated for {team_id}; session {session_id}"
            except NokiaNaCError as error:
                return f"QoS on Demand fallback ({error}) activated for {team_id} on incident {incident_id}"
        return f"QoS on Demand activated for {team_id} on incident {incident_id}"

    def subscribe_geofence(self, team_id: str) -> tuple[str | None, str | None]:
        if not self.nokia.enabled:
            return None, None
        try:
            subscription_id = self.nokia.create_geofence_subscription(self.team_phone_numbers[team_id])
            return (
                f"Geofencing (Nokia NaC) subscribed for {team_id}; subscription {subscription_id}",
                subscription_id,
            )
        except NokiaNaCError as error:
            return f"Geofencing: Nokia unavailable ({error}) — in-app geofence fallback active", None

    @staticmethod
    def _extract_fraction(value: object) -> float:
        """Tolerate provider response variations while keeping a safe 0–1 score."""
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value) / (100 if value > 1 else 1)))
        if isinstance(value, dict):
            preferred = ("congestion", "congestionLevel", "percentage", "load", "networkLoad")
            for key in preferred:
                if key in value:
                    return CamaraSimulator._extract_fraction(value[key])
            for child in value.values():
                fraction = CamaraSimulator._extract_fraction(child)
                if fraction:
                    return fraction
        if isinstance(value, list):
            for child in value:
                fraction = CamaraSimulator._extract_fraction(child)
                if fraction:
                    return fraction
        return 0.0
