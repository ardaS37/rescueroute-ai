"""Replaceable simulator for CAMARA-aligned network signals.

The functions intentionally mirror the kind of data the production adapter will
request from Nokia Network-as-Code / an operator gateway.
"""

from __future__ import annotations

import random

from app.models import SimulationState, VenueEdge, VenueLayout, VenueNode, VenueTemplateSummary
from app.services.nokia_nac import NokiaNaCClient, NokiaNaCError
from app.venue import Edge, ResponseTeam, TEMPLATES, distance_metres, get_template

# How the two cellular signals combine into the pressure a route pays.
NETWORK_LOAD_SHARE = 0.70
LOCAL_CONTENTION_SHARE = 0.30
# An active QoD session removes most of the coordination cost.
QOS_RELIEF_FACTOR = 0.40
# Added to a corridor that is restricted rather than closed.
RESTRICTED_ACCESS_SECONDS = 45
# How close a reported device fix must be to a node before it is treated as
# that node. Derived per venue rather than fixed: a constant wide enough for the
# stadium would swallow the 29 m between the Mataf ring and the Kaaba, snapping
# a fix to whichever of the two it happened to reach first.
LOCATION_SNAP_SHARE = 0.45
LOCATION_SNAP_MAX_M = 120.0


class CamaraSimulator:
    """Venue fallback state enriched by Nokia NaC signals when enabled."""

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
        self._restricted_corridors: set[tuple[str, str]] = set()
        self._device_status = {team.id: True for team in self._template.teams}
        self._pinned_device_status: dict[str, bool] = {}
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
            # Pinned, so a live Device Status lookup cannot undo the scenario
            # the operator deliberately loaded.
            self._pinned_device_status["medic_alpha"] = False
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
        # Each template names the zones its patterns load.  Selecting them by
        # position in ``zones`` surged the wrong areas: "stage cluster" on the
        # festival map loaded the entry and east lanes and never the stage.
        biases = self._template.crowd_bias.get(self._crowd_pattern, {})
        self._zone_congestion, self._crowd_distribution = {}, {}
        for zone in self._template.zones:
            density = min(0.95, max(0.08, 0.28 + biases.get(zone, 0) + self._rng.uniform(-0.12, 0.12)))
            self._zone_congestion[zone] = round(density, 2)
            self._crowd_distribution[zone] = round(density * 1_200)
        for zone, density in self._template.crowd_overrides.get(self._crowd_pattern, {}).items():
            self._zone_congestion[zone] = density
            self._crowd_distribution[zone] = round(density * 1_200)
        self._sync_simulated_network_load()

    def advance(self, minutes: int) -> SimulationState:
        self._minutes += minutes
        for zone, current in self._zone_congestion.items():
            self._zone_congestion[zone] = round(min(0.98, max(0.03, current + self._rng.uniform(-0.18, 0.18) * min(minutes / 10, 1))), 2)
            self._crowd_distribution[zone] = round(self._zone_congestion[zone] * 1_200)
        self._sync_simulated_network_load()
        return self.state()

    def update_congestion(self, zone: str, density: float) -> SimulationState:
        if zone not in self._zone_congestion:
            raise ValueError(f"Unknown zone '{zone}' for {self._template.key}.")
        if not 0 <= density <= 1:
            raise ValueError("Zone density must be between 0 and 1.")
        self._zone_congestion[zone] = round(density, 2)
        self._crowd_distribution[zone] = round(density * 1_200)
        self._sync_simulated_network_load()
        return self.state()

    def update_congestion_many(self, zone_densities: dict[str, float]) -> SimulationState:
        """Apply a single estimated-pressure snapshot atomically for all venue zones."""
        invalid = [zone for zone in zone_densities if zone not in self._zone_congestion]
        if invalid:
            raise ValueError(f"Unknown zone '{invalid[0]}' for {self._template.key}.")
        invalid_density = next((value for value in zone_densities.values() if not 0 <= value <= 1), None)
        if invalid_density is not None:
            raise ValueError("Zone density must be between 0 and 1.")
        for zone, density in zone_densities.items():
            self._zone_congestion[zone] = round(density, 2)
            self._crowd_distribution[zone] = round(density * 1_200)
        self._sync_simulated_network_load()
        return self.state()

    def set_corridor_status(
        self, source: str, destination: str, closed: bool, restricted: bool = False
    ) -> SimulationState:
        """Close a corridor outright, or restrict it so it costs access time.

        A restricted corridor stays walkable but adds controlled-access delay,
        which is what the access term of the ETA formula measures.  Closing
        still removes the edge entirely.
        """
        if destination not in {edge.destination for edge in self._template.graph.get(source, ())}:
            raise ValueError(f"'{source}' and '{destination}' are not directly connected.")
        corridor = tuple(sorted((source, destination)))
        self._closed_corridors.discard(corridor)
        self._restricted_corridors.discard(corridor)
        if closed:
            self._closed_corridors.add(corridor)
        elif restricted:
            self._restricted_corridors.add(corridor)
        return self.state()

    def state(self) -> SimulationState:
        return SimulationState(template=self._template.key, title=self._template.title, seed=self._seed,
            crowd_pattern=self._crowd_pattern, simulated_minutes=self._minutes,
            zone_congestion=self._zone_congestion, crowd_distribution=self._crowd_distribution,
            closed_corridors=[" <-> ".join(corridor) for corridor in sorted(self._closed_corridors)],
            restricted_corridors=[" <-> ".join(corridor) for corridor in sorted(self._restricted_corridors)],
            active_scenario=self._active_scenario, device_status=self._device_status,
            network_load=self._network_load, network_source=self._network_source,
            qos_active=self._qos_active)

    def restore_state(self, state: SimulationState) -> SimulationState:
        """Restore the last operational snapshot after a controlled container restart."""
        self.configure(state.template, state.seed, state.crowd_pattern)
        self._minutes = state.simulated_minutes
        self._zone_congestion = dict(state.zone_congestion)
        self._crowd_distribution = dict(state.crowd_distribution)
        self._closed_corridors = {
            tuple(sorted(corridor.split(" <-> ")))
            for corridor in state.closed_corridors if " <-> " in corridor
        }
        self._restricted_corridors = {
            tuple(sorted(corridor.split(" <-> ")))
            for corridor in state.restricted_corridors if " <-> " in corridor
        }
        self._active_scenario = state.active_scenario
        self._device_status = dict(state.device_status)
        self._pinned_device_status = {}
        self._network_load = state.network_load
        self._network_source = state.network_source
        self._qos_active = state.qos_active
        return self.state()

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
                kind="gate" if node in self._template.gates else "landmark",
                latitude=(coordinates := self._template.coordinates(node)) and coordinates[0],
                longitude=coordinates and coordinates[1])
                for node, position in self._template.positions.items()],
            edges=edges,
        )

    @property
    def gates(self) -> frozenset[str]:
        return self._template.gates

    @property
    def teams(self) -> tuple[ResponseTeam, ...]:
        """Roster for the loaded venue, in the order ties are broken."""
        return self._template.teams

    def team(self, team_id: str) -> ResponseTeam:
        for team in self._template.teams:
            if team.id == team_id:
                return team
        raise KeyError(team_id)

    @property
    def team_phone_numbers(self) -> dict[str, str]:
        return {team.id: team.phone_number for team in self._template.teams}

    def is_known_node(self, node: str) -> bool:
        return node in self._template.graph

    def coordinates(self, node: str) -> tuple[float, float] | None:
        """Real position of a venue node, if the template is geo-anchored."""
        return self._template.coordinates(node)

    def location_snap_radius_m(self) -> float:
        """Half the closest node pair, so a fix can only resolve one way."""
        positions = [p for node in self._template.graph if (p := self._template.coordinates(node))]
        closest = min(
            (distance_metres(a, b) for index, a in enumerate(positions) for b in positions[index + 1:]),
            default=0.0,
        )
        return min(LOCATION_SNAP_MAX_M, LOCATION_SNAP_SHARE * closest) if closest else LOCATION_SNAP_MAX_M

    def nearest_node(self, latitude: float, longitude: float) -> tuple[str | None, float]:
        """Closest graph node to a real fix, with its distance in metres."""
        best: tuple[str | None, float] = (None, float("inf"))
        for node in self._template.graph:
            position = self._template.coordinates(node)
            if position and (metres := distance_metres(position, (latitude, longitude))) < best[1]:
                best = (node, metres)
        return best

    def neighbors(self, node: str) -> tuple[Edge, ...]:
        return tuple(edge for edge in self._template.graph.get(node, ())
            if tuple(sorted((node, edge.destination))) not in self._closed_corridors)

    def get_congestion(self, zone: str | None) -> float:
        """Simulates CAMARA Congestion Insights for a venue zone."""
        return self._zone_congestion.get(zone or "", 0.0)

    def access_seconds(self, source: str, edge: Edge) -> float:
        """Controlled-access time for one corridor, including runtime restrictions."""
        corridor = tuple(sorted((source, edge.destination)))
        restriction = RESTRICTED_ACCESS_SECONDS if corridor in self._restricted_corridors else 0
        return edge.access_seconds + restriction

    def _simulated_network_load(self) -> float:
        """Recorded cellular load for the venue when no operator feed is available.

        A packed venue loads the local cells, but the signal is venue-wide: it
        must not collapse into a restatement of one corridor's density.
        """
        if not self._zone_congestion:
            return 0.0
        densities = list(self._zone_congestion.values())
        average = sum(densities) / len(densities)
        return round(min(0.95, 0.55 * average + 0.45 * max(densities)), 2)

    def _sync_simulated_network_load(self) -> None:
        if self._network_source == "live_nokia":
            return
        self._network_load = self._simulated_network_load()

    def refresh_network_congestion(self, team_id: str) -> None:
        """Fetch one live network signal per decision, never per graph edge."""
        if self.nokia.enabled:
            try:
                response = self.nokia.congestion(self.team_phone_numbers[team_id])
                self._network_load = self._extract_fraction(response)
                self._network_source = "live_nokia"
                self._live_api_calls.append(
                    f"Congestion Insights (Nokia NaC): network load {self._network_load:.0%}"
                )
                return
            except NokiaNaCError as error:
                self._network_source = "recorded_fallback"
                self._live_api_calls.append(
                    f"Congestion Insights: Nokia unavailable ({error}) - recorded network fallback used"
                )
        else:
            self._network_source = "simulation"
        # Without an operator feed the load used to stay at 0.0, which silently
        # removed the network term from every deterministic demo route.
        self._network_load = self._simulated_network_load()

    def network_pressure(self, zone_congestion: float) -> float:
        """Cellular pressure on a corridor: operator load plus local contention.

        ``network_load`` dominates because it is the signal CAMARA actually
        reports; local density adds the extra device contention of a packed
        zone.  A quiet corridor in a loaded venue still pays a network cost,
        which the previous crowd-multiplied formula made impossible.
        """
        pressure = NETWORK_LOAD_SHARE * self._network_load + LOCAL_CONTENTION_SHARE * zone_congestion
        return max(0.0, min(1.0, pressure))

    def qos_relief(self) -> float:
        """An active QoD session lowers the network cost of coordination."""
        return QOS_RELIEF_FACTOR if self._qos_active else 1.0

    def drain_live_api_calls(self) -> list[str]:
        calls, self._live_api_calls = self._live_api_calls, []
        return calls

    def get_team_location(self, team_id: str, known_location: str | None = None) -> str:
        """Simulates Location Retrieval for an authorised response team.

        The provider call verifies the team; the venue graph node comes from the
        operational record the dispatcher keeps, because the graph carries no
        geographic coordinates to map a real fix onto.
        """
        location = known_location or self.team(team_id).home_base
        if not self.nokia.enabled:
            return location
        try:
            response = self.nokia.location(self.team_phone_numbers[team_id])
        except NokiaNaCError as error:
            self._live_api_calls.append(
                f"Location Retrieval: Nokia unavailable ({error}) — recorded venue position used"
            )
            return location
        fix = self._extract_coordinates(response)
        if fix is None:
            self._live_api_calls.append(
                f"Location Retrieval (Nokia NaC): {team_id} returned no usable fix — recorded position {location} used"
            )
            return location
        node, metres = self.nearest_node(*fix)
        if node and metres <= self.location_snap_radius_m():
            self._live_api_calls.append(
                f"Location Retrieval (Nokia NaC): {team_id} fixed {metres:.0f} m from {node} → routing from {node}"
            )
            return node
        # A simulator handset sits at a fixed lab position far from the venue.
        # Saying so is more useful than silently pretending the fix was used.
        self._live_api_calls.append(
            f"Location Retrieval (Nokia NaC): {team_id} fixed {metres / 1000:.0f} km outside the venue "
            f"(simulator handset) — recorded position {location} used"
        )
        return location

    @staticmethod
    def _extract_coordinates(response: object) -> tuple[float, float] | None:
        """Pull a latitude/longitude pair out of a CAMARA location response."""
        if isinstance(response, dict):
            if "latitude" in response and "longitude" in response:
                try:
                    return float(response["latitude"]), float(response["longitude"])
                except (TypeError, ValueError):
                    return None
            for value in response.values():
                if (found := CamaraSimulator._extract_coordinates(value)) is not None:
                    return found
        elif isinstance(response, list):
            for value in response:
                if (found := CamaraSimulator._extract_coordinates(value)) is not None:
                    return found
        return None

    def device_status(self, team_id: str) -> bool:
        """Last recorded reachability, without contacting the provider."""
        return self._device_status.get(team_id, False)

    # Nokia reports the bearer as well as the state, e.g. CONNECTED_DATA and
    # CONNECTED_SMS, so the prefix is what carries the meaning.
    REACHABLE_PREFIXES = ("CONNECTED", "REACHABLE", "TRUE")
    UNREACHABLE_PREFIXES = ("NOT_CONNECTED", "DISCONNECTED", "UNREACHABLE", "FALSE")

    @classmethod
    def _interpret_connectivity(cls, connectivity: str) -> bool | None:
        """True, False, or None when the provider said something unrecognised."""
        if connectivity.startswith(cls.UNREACHABLE_PREFIXES):
            return False
        if connectivity.startswith(cls.REACHABLE_PREFIXES):
            return True
        return None

    def check_device_status(self, team_id: str) -> bool:
        """Simulates CAMARA Device Status from recorded fallback data."""
        if team_id in self._pinned_device_status:
            pinned = self._pinned_device_status[team_id]
            self._live_api_calls.append(
                f"Device Status: {team_id} → {'reachable' if pinned else 'unreachable'} "
                f"(recorded scenario '{self._active_scenario}')"
            )
            return pinned
        if self.nokia.enabled:
            try:
                connectivity = self.nokia.connectivity(self.team_phone_numbers[team_id]).upper()
                reachable = self._interpret_connectivity(connectivity)
                if reachable is None:
                    self._live_api_calls.append(
                        f"Device Status (Nokia NaC): {team_id} returned '{connectivity.lower()}'; "
                        f"recorded reachability used"
                    )
                    return self._device_status.get(team_id, False)
                # Record it, so the roster shown to the control centre agrees
                # with what the network just reported.
                self._device_status[team_id] = reachable
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
        """Simulates QoS on Demand activation for critical communication.

        The session is recorded as active in every mode.  Setting the flag only
        on a live Nokia success meant the deterministic demo reported "QoS
        activated" while the routing engine never applied the relief.
        """
        if self.nokia.enabled:
            try:
                session_id = self.nokia.create_qod_session(self.team_phone_numbers[team_id])
                self._qos_active = True
                return f"QoS on Demand (Nokia NaC) activated for {team_id}; session {session_id}"
            except NokiaNaCError as error:
                self._qos_active = True
                return f"QoS on Demand fallback ({error}) activated for {team_id} on incident {incident_id}"
        self._qos_active = True
        return f"QoS on Demand activated for {team_id} on incident {incident_id}"

    def subscribe_geofence(self, team_id: str, gate: str) -> tuple[str | None, str | None]:
        """Watch the selected gate, so an area-entered callback means arrival there."""
        if not self.nokia.enabled:
            return None, None
        area = self.coordinates(gate)
        if area is None:
            return f"Geofencing: {gate} has no mapped position — in-app geofence fallback active", None
        radius = self._template.geo.gate_radius_m if self._template.geo else 60
        try:
            subscription_id = self.nokia.create_geofence_subscription(
                self.team_phone_numbers[team_id], area[0], area[1], radius
            )
            return (
                f"Geofencing (Nokia NaC): {team_id} watched at {gate} "
                f"({area[0]:.5f}, {area[1]:.5f}) r={radius}m; subscription {subscription_id}",
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
