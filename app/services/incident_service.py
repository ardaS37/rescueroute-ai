from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models import (GeofenceEvent, Incident, IncidentProgress, IncidentStatus,
    GateRouteOption, IncidentRouteHistory, Priority, RouteCostBreakdown, RouteDecision,
    RouteHistoryEntry, RouteSegment)
from app.services.camara_simulator import CamaraSimulator
from app.services.persistence import SQLiteStore
from app.services.routing import CalculatedRoute, RouteNotFoundError, RoutingService


class IncidentNotFoundError(KeyError):
    pass


class IncidentService:
    def __init__(self, camara: CamaraSimulator, routing: RoutingService, store: SQLiteStore | None = None) -> None:
        self.camara = camara
        self.routing = routing
        self.store = store or SQLiteStore()
        self._incidents: dict[str, Incident] = {}
        self._decisions: dict[str, RouteDecision] = {}
        self._progress: dict[str, IncidentProgress] = {}
        self._history: dict[str, list[RouteHistoryEntry]] = {}
        self._nokia_geofences = self.store.load_geofence_subscriptions()
        self._restore_records()

    def _restore_records(self) -> None:
        for record in self.store.load_incidents():
            incident = Incident.model_validate(record["incident"])
            self._incidents[incident.id] = incident
            if record["decision"]:
                self._decisions[incident.id] = RouteDecision.model_validate(record["decision"])
            if record["progress"]:
                self._progress[incident.id] = IncidentProgress.model_validate(record["progress"])
            self._history[incident.id] = [
                RouteHistoryEntry.model_validate(entry) for entry in record["history"]
            ]

    def _persist(self, incident_id: str) -> None:
        incident = self._incidents[incident_id]
        decision = self._decisions.get(incident_id)
        progress = self._progress.get(incident_id)
        self.store.save_incident(
            incident_id,
            incident.model_dump(mode="json"),
            decision.model_dump(mode="json") if decision else None,
            progress.model_dump(mode="json") if progress else None,
            [entry.model_dump(mode="json") for entry in self._history.get(incident_id, [])],
        )

    def create(self, location: str, priority: Priority, description: str) -> Incident:
        incident = Incident(
            id=str(uuid4()), location=location, priority=priority, description=description
        )
        self._incidents[incident.id] = incident
        self._persist(incident.id)
        return incident

    def get(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError as error:
            raise IncidentNotFoundError(incident_id) from error

    def dispatch(
        self, incident_id: str, trigger: str = "Initial emergency dispatch", agent_tools: list[str] | None = None
    ) -> tuple[Incident, RouteDecision]:
        incident = self.get(incident_id)
        previous_decision = self._decisions.get(incident_id)
        api_calls = ["Device Status: medic_alpha"]
        team_id = "medic_alpha"
        if not self.camara.check_device_status(team_id):
            team_id = "medic_bravo"
            api_calls.append("Device Status: medic_bravo")
            if not self.camara.check_device_status(team_id):
                raise RouteNotFoundError("No reachable emergency team is available.")
        api_calls.extend(self.camara.drain_live_api_calls())

        source = self.camara.get_team_location(team_id)
        api_calls.append(f"Location Retrieval: {team_id} → {source}")
        self.camara.refresh_network_congestion(team_id)
        api_calls.extend(self.camara.drain_live_api_calls())
        route = self.routing.shortest_route(source, incident.location)
        selected_gate = next((node for node in route.nodes if node in self.camara.gates), "on_site")
        zones = sorted({edge.zone for edge in route.edges if edge.zone})
        api_calls.extend(
            f"Congestion Insights: {zone} ({self.camara.get_congestion(zone):.0%})"
            for zone in zones
        )

        gate_options: list[GateRouteOption] = []
        for gate in sorted(self.camara.gates):
            try:
                candidate = self.routing.route_via_gate(source, gate, incident.location)
                gate_options.append(GateRouteOption(
                    gate=gate, eta_seconds=candidate.eta_seconds,
                    route_distance_m=candidate.distance_m, available=True,
                ))
            except RouteNotFoundError:
                gate_options.append(GateRouteOption(
                    gate=gate, available=False, reason="Access route is closed or unavailable."
                ))

        selected_tools = set(agent_tools or {"qos_on_demand", "geofencing"})
        if "qos_on_demand" in selected_tools:
            api_calls.append(self.camara.activate_qos(team_id, incident.id))
        if "geofencing" in selected_tools:
            geofence, subscription_id = self.camara.subscribe_geofence(team_id)
            if geofence:
                api_calls.append(geofence)
            if subscription_id:
                self._nokia_geofences[subscription_id] = (incident.id, team_id, selected_gate)
                self.store.save_geofence_subscription(subscription_id, incident.id, team_id, selected_gate)

        incident.status = IncidentStatus.DISPATCHED
        segments = [
            RouteSegment(
                source=route.nodes[index],
                destination=edge.destination,
                distance_m=edge.distance_m,
                zone=edge.zone,
            )
            for index, edge in enumerate(route.edges)
        ]
        decision = RouteDecision(
            incident_id=incident.id,
            team_id=team_id,
            selected_gate=selected_gate,
            route=route.nodes,
            segments=segments,
            estimated_arrival_seconds=route.eta_seconds,
            route_distance_m=route.distance_m,
            cost_breakdown=RouteCostBreakdown(
                distance_seconds=route.distance_seconds,
                crowd_penalty_seconds=route.crowd_penalty_seconds,
                network_penalty_seconds=route.network_penalty_seconds,
                access_penalty_seconds=route.access_penalty_seconds,
                total_seconds=route.eta_seconds,
            ),
            gate_options=gate_options,
            explanation=self._explanation(selected_gate, route, gate_options),
            api_calls=api_calls,
        )
        self._decisions[incident.id] = decision
        history_entry = RouteHistoryEntry(
            occurred_at=datetime.now(UTC),
            event_type="reroute" if previous_decision else "dispatch",
            trigger=trigger,
            previous_route=previous_decision.route if previous_decision else None,
            previous_eta_seconds=(
                previous_decision.estimated_arrival_seconds if previous_decision else None
            ),
            route=decision.route,
            eta_seconds=decision.estimated_arrival_seconds,
            selected_gate=decision.selected_gate,
            explanation=decision.explanation,
        )
        self._history.setdefault(incident.id, []).append(history_entry)
        self._progress.setdefault(
            incident.id,
            IncidentProgress(
                incident_id=incident.id,
                team_id=team_id,
                last_location=source,
                events=[],
                completed=False,
            ),
        )
        self._persist(incident.id)
        return incident, decision

    def get_history(self, incident_id: str) -> IncidentRouteHistory:
        self.get(incident_id)
        return IncidentRouteHistory(
            incident_id=incident_id, entries=self._history.get(incident_id, [])
        )

    def affected_active_incidents(
        self, *, zones: set[str] | None = None, corridor: tuple[str, str] | None = None
    ) -> list[str]:
        """Return dispatched incidents whose current route uses a changed area.

        This deliberately filters changes before calling Nokia APIs again. A crowd update
        in an unrelated zone should not create an unnecessary QoS session or reroute.
        """
        affected: list[str] = []
        corridor_key = tuple(sorted(corridor)) if corridor else None
        for incident_id, incident in self._incidents.items():
            if incident.status != IncidentStatus.DISPATCHED or not self.camara.is_known_node(incident.location):
                continue
            decision = self._decisions.get(incident_id)
            if not decision:
                continue
            route_zones = {segment.zone for segment in decision.segments if segment.zone}
            route_corridors = {
                tuple(sorted((segment.source, segment.destination))) for segment in decision.segments
            }
            if (zones and route_zones.intersection(zones)) or (corridor_key and corridor_key in route_corridors):
                affected.append(incident_id)
        return affected

    def record_geofence_event(
        self, incident_id: str, team_id: str, location: str, event_type: str
    ) -> IncidentProgress:
        incident = self.get(incident_id)
        if incident_id not in self._decisions:
            raise ValueError("Dispatch the incident before reporting geofence events.")
        progress = self._progress[incident_id]
        decision = self._decisions[incident_id]
        if team_id != decision.team_id:
            raise ValueError(f"'{team_id}' is not assigned to this incident.")
        if event_type == "entered_selected_gate" and location != decision.selected_gate:
            raise ValueError(f"Selected gate is '{decision.selected_gate}', not '{location}'.")
        if event_type == "reached_patient" and location != incident.location:
            raise ValueError(f"Patient location is '{incident.location}', not '{location}'.")

        progress.last_location = location
        progress.events.append(GeofenceEvent(
            event_type=event_type, location=location, occurred_at=datetime.now(UTC)
        ))
        if event_type == "reached_patient":
            progress.completed = True
            incident.status = IncidentStatus.RESOLVED
        self._persist(incident_id)
        return progress

    def get_progress(self, incident_id: str) -> IncidentProgress:
        self.get(incident_id)
        try:
            return self._progress[incident_id]
        except KeyError as error:
            raise ValueError("Dispatch the incident before requesting progress.") from error

    def process_nokia_geofence_callback(
        self, payload: dict[str, object]
    ) -> tuple[IncidentProgress | None, str]:
        """Convert a Nokia area-entered callback into an incident progress event."""
        subscription_id = self._find_payload_value(payload, "subscriptionId", "subscription_id")
        event_type = str(self._find_payload_value(payload, "type", "eventType", "event_type") or "")
        if not subscription_id:
            return None, "ignored: callback has no subscription ID"
        if "area-entered" not in event_type:
            return None, "ignored: callback is not an area-entered event"
        target = self._nokia_geofences.get(str(subscription_id))
        if not target:
            return None, "ignored: unknown or expired geofence subscription"
        incident_id, team_id, gate = target
        decision = self._decisions.get(incident_id)
        if not decision or decision.team_id != team_id or decision.selected_gate != gate:
            return None, "ignored: callback belongs to a superseded route"
        progress = self.record_geofence_event(
            incident_id, team_id, gate, "entered_selected_gate"
        )
        return progress, "processed: team entered selected gate"

    @staticmethod
    def _find_payload_value(payload: object, *keys: str) -> object | None:
        if isinstance(payload, dict):
            for key in keys:
                if key in payload:
                    return payload[key]
            for value in payload.values():
                found = IncidentService._find_payload_value(value, *keys)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = IncidentService._find_payload_value(value, *keys)
                if found is not None:
                    return found
        return None

    def _explanation(
        self, selected_gate: str, route: CalculatedRoute, options: list[GateRouteOption]
    ) -> str:
        alternatives = [option for option in options if option.available and option.gate != selected_gate]
        shorter = min(alternatives, key=lambda option: option.route_distance_m or 0, default=None)
        gate_name = selected_gate.replace("_", " ").title()
        if shorter and (shorter.route_distance_m or 0) < route.distance_m:
            distance_saved = route.distance_m - (shorter.route_distance_m or 0)
            eta_delay = (shorter.eta_seconds or 0) - route.eta_seconds
            if eta_delay > 0:
                return (
                    f"{gate_name} selected: {shorter.gate.replace('_', ' ').title()} is {distance_saved} m shorter, "
                    f"but crowd and network delays make its estimated arrival {eta_delay // 60} min "
                    f"{eta_delay % 60} sec slower."
                )
        return (
            f"{gate_name} selected: distance ({route.distance_seconds} sec), crowd "
            f"(+{route.crowd_penalty_seconds} sec), and network (+{route.network_penalty_seconds} sec) "
            f"produce the lowest estimated arrival ({route.eta_seconds} sec)."
        )
