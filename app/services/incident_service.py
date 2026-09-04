from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

from app.models import (GeofenceEvent, Incident, IncidentProgress, IncidentStatus,
    GateRouteOption, IncidentRouteHistory, Priority, RouteCostBreakdown, RouteDecision,
    RouteHistoryEntry, RouteSegment, TeamState, TeamStatus)
from app.services.camara_simulator import CamaraSimulator
from app.services.persistence import SQLiteStore
from app.services.routing import CalculatedRoute, RouteNotFoundError, RoutingService

CLOSED_STATUSES = (IncidentStatus.RESOLVED, IncidentStatus.CANCELLED)
# Sentinel selected gate for a team that is already inside the venue, which
# happens now that a team stays at the scene it last worked.
ON_SITE = "on_site"
# Highest urgency first: this is the order queued incidents are released in.
PRIORITY_RANK = {
    Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3,
}


class IncidentNotFoundError(KeyError):
    pass


class IncidentStateError(ValueError):
    """Raised when an operation is not valid for the incident's current status."""


class NoTeamAvailableError(IncidentStateError):
    """Every team is committed elsewhere or unreachable; the incident is queued."""


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
        self._max_incidents = self._configured_max_incidents()
        # team_id -> incident it is committed to; team_id -> venue node it is at.
        self._assignments: dict[str, str] = {}
        self._team_locations: dict[str, str] = {}
        self._restore_records()
        self._prune_incidents()
        self._restore_team_state()

    @staticmethod
    def _configured_max_incidents() -> int:
        """Bound the in-memory and on-disk incident store on a public deployment."""
        try:
            configured = int(os.getenv("RESCUEROUTE_MAX_INCIDENTS", "500"))
        except ValueError:
            return 500
        return max(1, configured)

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

    # ------------------------------------------------------------------ teams

    def _restore_team_state(self) -> None:
        """Rebuild the roster view after a restart.

        Only idle positions need storing: a commitment is derivable from the
        incident that holds it, so the two can never drift apart.
        """
        stored = self.store.load_state("teams") or {}
        self._team_locations = dict(stored.get("locations", {}))
        self._assignments = {}
        for incident_id, incident in self._incidents.items():
            decision = self._decisions.get(incident_id)
            if incident.status == IncidentStatus.DISPATCHED and decision:
                self._assignments[decision.team_id] = incident_id

    def _persist_team_state(self) -> None:
        self.store.save_state("teams", {"locations": self._team_locations})

    def team_location(self, team_id: str) -> str:
        """Where the team is now: the scene it last worked, else its home base."""
        location = self._team_locations.get(team_id)
        if location and self.camara.is_known_node(location):
            return location
        return self.camara.team(team_id).home_base

    def _release_team(self, incident_id: str, at_location: str | None = None) -> None:
        """Free whichever team held this incident, leaving it where it finished."""
        for team_id, held in list(self._assignments.items()):
            if held != incident_id:
                continue
            del self._assignments[team_id]
            if at_location and self.camara.is_known_node(at_location):
                self._team_locations[team_id] = at_location
        self._persist_team_state()

    def reset_team_positions(self) -> None:
        """Send every team back to its home base, e.g. after a venue change."""
        self._team_locations = {}
        self._persist_team_state()

    def teams(self) -> list[TeamState]:
        return [
            TeamState(
                id=team.id,
                name=team.name,
                location=self.team_location(team.id),
                status=(
                    TeamStatus.ASSIGNED if team.id in self._assignments
                    else TeamStatus.AVAILABLE if self.camara.device_status(team.id)
                    else TeamStatus.UNREACHABLE
                ),
                incident_id=self._assignments.get(team.id),
            )
            for team in self.camara.teams
        ]

    def _assign_team(self, incident: Incident, api_calls: list[str]) -> tuple[str, str]:
        """Pick the reachable, uncommitted team that can arrive soonest.

        Every incident used to be handed to ``medic_alpha``; the backup was only
        reached when alpha's handset was unreachable, never when it was already
        working another call.
        """
        best: tuple[int, int, str, str] | None = None
        for order, team in enumerate(self.camara.teams):
            committed_to = self._assignments.get(team.id)
            if committed_to is not None and committed_to != incident.id:
                continue
            api_calls.append(f"Device Status: {team.id}")
            if not self.camara.check_device_status(team.id):
                continue
            location = self.team_location(team.id)
            try:
                eta = self.routing.shortest_route(location, incident.location).eta_seconds
            except RouteNotFoundError:
                continue
            if best is None or (eta, order) < (best[0], best[1]):
                best = (eta, order, team.id, location)
        if best is None:
            raise NoTeamAvailableError(
                "No response team is available; the incident is queued until one frees up."
            )
        return best[2], best[3]

    def next_queued_incident(self, skip: set[str] | None = None) -> str | None:
        """The most urgent waiting incident, if any team could take it now."""
        if not any(
            team.id not in self._assignments and self.camara.device_status(team.id)
            for team in self.camara.teams
        ):
            return None
        waiting = [
            (PRIORITY_RANK[incident.priority], order, incident_id)
            for order, (incident_id, incident) in enumerate(self._incidents.items())
            if incident.status == IncidentStatus.QUEUED and incident_id not in (skip or set())
        ]
        return min(waiting)[2] if waiting else None

    def _forget(self, incident_id: str) -> None:
        """Drop every trace of one incident from memory and storage."""
        self._release_team(incident_id)
        self._incidents.pop(incident_id, None)
        self._decisions.pop(incident_id, None)
        self._progress.pop(incident_id, None)
        self._history.pop(incident_id, None)
        self._nokia_geofences = {
            subscription_id: target
            for subscription_id, target in self._nokia_geofences.items()
            if target[0] != incident_id
        }
        self.store.delete_incident(incident_id)

    def _forget_geofence_subscriptions(self, incident_id: str) -> None:
        """Stop honouring Nokia callbacks for an incident that is no longer active."""
        self._nokia_geofences = {
            subscription_id: target
            for subscription_id, target in self._nokia_geofences.items()
            if target[0] != incident_id
        }
        self.store.delete_geofence_subscriptions(incident_id)

    def _prune_incidents(self) -> None:
        """Keep the store bounded, dropping closed incidents before open ones."""
        overflow = len(self._incidents) - self._max_incidents
        if overflow <= 0:
            return
        closed = [
            incident_id for incident_id, incident in self._incidents.items()
            if incident.status in CLOSED_STATUSES
        ]
        ordered = closed + [
            incident_id for incident_id in self._incidents if incident_id not in set(closed)
        ]
        for incident_id in ordered[:overflow]:
            self._forget(incident_id)

    def create(self, location: str, priority: Priority, description: str) -> Incident:
        incident = Incident(
            id=str(uuid4()), location=location, priority=priority, description=description
        )
        self._incidents[incident.id] = incident
        self._persist(incident.id)
        self._prune_incidents()
        return incident

    def cancel_incidents_outside_venue(self) -> list[str]:
        """Cancel open incidents whose location is not part of the loaded venue.

        Switching template used to leave incidents ``dispatched`` forever with a
        decision that referenced nodes from a different graph; every reroute
        attempt on them then failed with ``RouteNotFoundError``.
        """
        cancelled: list[str] = []
        for incident_id, incident in self._incidents.items():
            if incident.status in CLOSED_STATUSES or self.camara.is_known_node(incident.location):
                continue
            incident.status = IncidentStatus.CANCELLED
            self._release_team(incident_id)
            self._forget_geofence_subscriptions(incident_id)
            self._persist(incident_id)
            cancelled.append(incident_id)
        return cancelled

    def get(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError as error:
            raise IncidentNotFoundError(incident_id) from error

    def dispatch(
        self, incident_id: str, trigger: str = "Initial emergency dispatch", agent_tools: list[str] | None = None
    ) -> tuple[Incident, RouteDecision]:
        incident = self.get(incident_id)
        if incident.status in CLOSED_STATUSES:
            raise IncidentStateError(
                f"Incident is already {incident.status.value}; it can no longer be dispatched."
            )
        previous_decision = self._decisions.get(incident_id)
        api_calls: list[str] = []

        # A reroute changes the route, not the responder: the assigned team keeps
        # the call unless its handset has since become unreachable.
        team_id = source = None
        if previous_decision and self._assignments.get(previous_decision.team_id) == incident_id:
            api_calls.append(f"Device Status: {previous_decision.team_id}")
            if self.camara.check_device_status(previous_decision.team_id):
                team_id = previous_decision.team_id
                source = self.team_location(team_id)
        if team_id is None:
            try:
                team_id, source = self._assign_team(incident, api_calls)
            except NoTeamAvailableError:
                incident.status = IncidentStatus.QUEUED
                self._release_team(incident.id)
                self._persist(incident.id)
                raise
        self._assignments[team_id] = incident.id
        api_calls.extend(self.camara.drain_live_api_calls())

        source = self.camara.get_team_location(team_id, source)
        api_calls.append(f"Location Retrieval: {team_id} → {source}")
        self.camara.refresh_network_congestion(team_id)
        api_calls.extend(self.camara.drain_live_api_calls())

        # The QoD session has to exist before the route is scored, otherwise the
        # relief it provides only ever reaches the *next* decision.
        selected_tools = set(agent_tools or {"qos_on_demand", "geofencing"})
        if "qos_on_demand" in selected_tools:
            api_calls.append(self.camara.activate_qos(team_id, incident.id))

        route = self.routing.shortest_route(source, incident.location)
        selected_gate = next((node for node in route.nodes if node in self.camara.gates), ON_SITE)
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

        if "geofencing" in selected_tools and selected_gate != ON_SITE:
            geofence, subscription_id = self.camara.subscribe_geofence(team_id, selected_gate)
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
        progress = self._progress.get(incident.id)
        if progress is None:
            self._progress[incident.id] = IncidentProgress(
                incident_id=incident.id,
                team_id=team_id,
                last_location=source,
                events=[],
                completed=False,
            )
        elif progress.team_id != team_id:
            # A reroute that reassigns the team must not leave progress reporting
            # the previous team; the two used to disagree after a failover.
            progress.team_id = team_id
            if not progress.events:
                progress.last_location = source
        self._persist_team_state()
        self._persist(incident.id)
        return incident, decision

    def get_decision(self, incident_id: str) -> RouteDecision:
        self.get(incident_id)
        try:
            return self._decisions[incident_id]
        except KeyError as error:
            raise ValueError("This incident has not been dispatched yet.") from error

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
        if incident.status == IncidentStatus.CANCELLED:
            raise IncidentStateError("This incident was cancelled; geofence events are not accepted.")
        if incident_id not in self._decisions:
            raise ValueError("Dispatch the incident before reporting geofence events.")
        progress = self._progress[incident_id]
        decision = self._decisions[incident_id]
        if team_id != decision.team_id:
            raise ValueError(f"'{team_id}' is not assigned to this incident.")
        if event_type == "entered_selected_gate" and decision.selected_gate == ON_SITE:
            raise IncidentStateError(
                f"'{decision.team_id}' started inside the venue, so this response has no gate crossing."
            )
        if event_type == "entered_selected_gate" and location != decision.selected_gate:
            raise ValueError(f"Selected gate is '{decision.selected_gate}', not '{location}'.")
        if event_type == "reached_patient" and location != incident.location:
            raise ValueError(f"Patient location is '{incident.location}', not '{location}'.")

        last_event = progress.events[-1] if progress.events else None
        if last_event and last_event.event_type == event_type and last_event.location == location:
            # Nokia re-sends area-entered callbacks (initialEvent plus the real
            # crossing), so repeating the newest event is a no-op, not an error.
            return progress
        if progress.completed:
            raise IncidentStateError(
                "This incident is already resolved; no further geofence events are accepted."
            )

        progress.last_location = location
        progress.events.append(GeofenceEvent(
            event_type=event_type, location=location, occurred_at=datetime.now(UTC)
        ))
        if event_type == "reached_patient":
            progress.completed = True
            incident.status = IncidentStatus.RESOLVED
            # The team stays where it finished, so it is closer to whatever
            # happens next nearby.
            self._release_team(incident_id, at_location=location)
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
        try:
            progress = self.record_geofence_event(
                incident_id, team_id, gate, "entered_selected_gate"
            )
        except ValueError as error:
            # A late callback for a finished response is expected, not a server fault.
            return None, f"ignored: {error}"
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
        if selected_gate == ON_SITE:
            # The team was already inside the venue, so no entry was chosen.
            return (
                f"No gate crossing needed: the team is already inside the venue. Distance "
                f"({route.distance_seconds} sec), crowd (+{route.crowd_penalty_seconds} sec) and "
                f"network (+{route.network_penalty_seconds} sec) give an estimated arrival of "
                f"{route.eta_seconds} sec."
            )
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
        access = (
            f", and access (+{route.access_penalty_seconds} sec)"
            if route.access_penalty_seconds
            else ""
        )
        return (
            f"{gate_name} selected: distance ({route.distance_seconds} sec), crowd "
            f"(+{route.crowd_penalty_seconds} sec), network (+{route.network_penalty_seconds} sec)"
            f"{access} produce the lowest estimated arrival ({route.eta_seconds} sec)."
        )
