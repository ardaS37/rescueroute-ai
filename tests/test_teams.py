"""Regression tests for team allocation and the waiting queue."""

import tempfile
import unittest
from pathlib import Path

from app.models import IncidentStatus, Priority, TeamStatus
from app.services.camara_simulator import CamaraSimulator
from app.services.incident_service import ON_SITE, IncidentService, IncidentStateError, NoTeamAvailableError
from app.services.persistence import SQLiteStore
from app.services.routing import RoutingService


def build_service(store: SQLiteStore | None = None) -> tuple[CamaraSimulator, IncidentService]:
    simulator = CamaraSimulator()
    simulator.configure("stadium_match", seed=42, crowd_pattern="balanced")
    return simulator, IncidentService(simulator, RoutingService(simulator), store)


def dispatch_at(service: IncidentService, location: str, priority: Priority = Priority.HIGH):
    incident = service.create(location, priority, f"call at {location}")
    return incident, service.dispatch(incident.id)[1]


def resolve(service: IncidentService, incident_id: str) -> None:
    decision = service._decisions[incident_id]
    incident = service.get(incident_id)
    service.record_geofence_event(
        incident_id, decision.team_id, decision.selected_gate, "entered_selected_gate"
    )
    service.record_geofence_event(
        incident_id, decision.team_id, incident.location, "reached_patient"
    )


class AllocationTests(unittest.TestCase):
    def test_concurrent_incidents_get_different_teams(self) -> None:
        """Four calls used to be handed to medic_alpha, twice over for criticals."""
        _, service = build_service()
        assigned = [
            dispatch_at(service, location, Priority.CRITICAL)[1].team_id
            for location in ("main_stage", "east_concourse", "first_aid")
        ]
        self.assertEqual(len(set(assigned)), 3, assigned)

    def test_the_team_that_can_arrive_soonest_is_chosen(self) -> None:
        simulator, service = build_service()
        first, _ = dispatch_at(service, "main_stage", Priority.HIGH)
        resolve(service, first.id)  # that team is now standing at main_stage

        freed = service._team_locations
        self.assertEqual(set(freed.values()), {"main_stage"})

        _, decision = dispatch_at(service, "central_plaza", Priority.HIGH)
        from_base = service.routing.shortest_route("ambulance_bay", "central_plaza")
        self.assertEqual(decision.route[0], "main_stage")
        self.assertLess(decision.estimated_arrival_seconds, from_base.eta_seconds)

    def test_unreachable_teams_are_skipped(self) -> None:
        simulator, service = build_service()
        simulator._device_status["medic_alpha"] = False
        _, decision = dispatch_at(service, "main_stage", Priority.CRITICAL)
        self.assertEqual(decision.team_id, "medic_bravo")
        self.assertIn("Device Status: medic_bravo", decision.api_calls)

    def test_a_reroute_keeps_the_assigned_team(self) -> None:
        simulator, service = build_service()
        incident, first = dispatch_at(service, "main_stage", Priority.HIGH)
        simulator.update_congestion("south_zone", 0.95)
        _, second = service.dispatch(incident.id, trigger="crowd changed")
        self.assertEqual(second.team_id, first.team_id)

    def test_a_reroute_reassigns_when_the_team_drops_off_the_network(self) -> None:
        simulator, service = build_service()
        incident, first = dispatch_at(service, "main_stage", Priority.HIGH)
        simulator._device_status[first.team_id] = False
        _, second = service.dispatch(incident.id, trigger="team handset lost")
        self.assertNotEqual(second.team_id, first.team_id)
        self.assertEqual(service.get_progress(incident.id).team_id, second.team_id)

    def test_resolving_an_incident_frees_its_team(self) -> None:
        _, service = build_service()
        incident, decision = dispatch_at(service, "main_stage", Priority.HIGH)
        self.assertEqual(service._assignments[decision.team_id], incident.id)
        resolve(service, incident.id)
        self.assertNotIn(decision.team_id, service._assignments)

    def test_cancelling_an_incident_frees_its_team(self) -> None:
        simulator, service = build_service()
        _, decision = dispatch_at(service, "main_stage", Priority.HIGH)
        simulator.configure("pilgrimage_flow", seed=42, crowd_pattern="balanced")
        service.cancel_incidents_outside_venue()
        self.assertNotIn(decision.team_id, service._assignments)


class QueueTests(unittest.TestCase):
    def fill_every_team(self, service: IncidentService) -> list[str]:
        return [
            dispatch_at(service, location, Priority.HIGH)[0].id
            for location in ("main_stage", "east_concourse", "first_aid")
        ]

    def test_an_incident_with_no_free_team_is_queued(self) -> None:
        _, service = build_service()
        self.fill_every_team(service)
        overflow = service.create("central_plaza", Priority.LOW, "overflow")

        with self.assertRaises(NoTeamAvailableError):
            service.dispatch(overflow.id)
        self.assertEqual(service.get(overflow.id).status, IncidentStatus.QUEUED)
        self.assertIsNone(service.next_queued_incident())

    def test_the_queue_releases_the_most_urgent_incident_first(self) -> None:
        _, service = build_service()
        busy = self.fill_every_team(service)
        for location, priority in (("central_plaza", Priority.LOW), ("north_corridor", Priority.CRITICAL)):
            waiting = service.create(location, priority, "waiting")
            with self.assertRaises(NoTeamAvailableError):
                service.dispatch(waiting.id)

        resolve(service, busy[0])
        released = service.next_queued_incident()
        self.assertIsNotNone(released)
        self.assertEqual(service.get(released).priority, Priority.CRITICAL)

        _, decision = service.dispatch(released, trigger="team became available")
        self.assertEqual(service.get(released).status, IncidentStatus.DISPATCHED)
        self.assertIsNotNone(decision.team_id)

    def test_the_queue_stays_closed_while_every_team_is_committed(self) -> None:
        _, service = build_service()
        self.fill_every_team(service)
        waiting = service.create("central_plaza", Priority.CRITICAL, "waiting")
        with self.assertRaises(NoTeamAvailableError):
            service.dispatch(waiting.id)
        self.assertIsNone(service.next_queued_incident())

    def test_a_queued_incident_can_be_skipped(self) -> None:
        _, service = build_service()
        busy = self.fill_every_team(service)
        waiting = service.create("central_plaza", Priority.LOW, "waiting")
        with self.assertRaises(NoTeamAvailableError):
            service.dispatch(waiting.id)
        resolve(service, busy[0])
        self.assertEqual(service.next_queued_incident(), waiting.id)
        self.assertIsNone(service.next_queued_incident(skip={waiting.id}))


class RosterViewTests(unittest.TestCase):
    def test_roster_reports_location_status_and_holder(self) -> None:
        simulator, service = build_service()
        incident, decision = dispatch_at(service, "main_stage", Priority.HIGH)
        simulator._device_status["medic_charlie"] = False

        roster = {team.id: team for team in service.teams()}
        self.assertEqual(len(roster), 3)
        self.assertEqual(roster[decision.team_id].status, TeamStatus.ASSIGNED)
        self.assertEqual(roster[decision.team_id].incident_id, incident.id)
        self.assertEqual(roster["medic_charlie"].status, TeamStatus.UNREACHABLE)
        idle = next(t for t in roster.values() if t.status == TeamStatus.AVAILABLE)
        self.assertEqual(idle.location, "ambulance_bay")
        self.assertIsNone(idle.incident_id)

    def test_team_positions_reset_when_the_venue_changes(self) -> None:
        simulator, service = build_service()
        incident, _ = dispatch_at(service, "main_stage", Priority.HIGH)
        resolve(service, incident.id)
        self.assertEqual(service.team_location("medic_alpha"), "main_stage")

        simulator.configure("pilgrimage_flow", seed=42, crowd_pattern="balanced")
        service.reset_team_positions()
        self.assertEqual(service.team_location("medic_alpha"), "ambulance_bay")

    def test_a_position_from_another_venue_falls_back_to_the_home_base(self) -> None:
        simulator, service = build_service()
        service._team_locations["medic_alpha"] = "main_stage"
        simulator.configure("pilgrimage_flow", seed=42, crowd_pattern="balanced")
        self.assertEqual(service.team_location("medic_alpha"), "ambulance_bay")

    def test_assignments_and_positions_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "rescueroute.db")
            first_store = SQLiteStore(database)
            simulator, service = build_service(first_store)
            resolved, _ = dispatch_at(service, "main_stage", Priority.HIGH)
            resolve(service, resolved.id)
            busy, decision = dispatch_at(service, "east_concourse", Priority.HIGH)
            first_store.close()

            restored_store = SQLiteStore(database)
            restored = IncidentService(simulator, RoutingService(simulator), restored_store)
            roster = {team.id: team for team in restored.teams()}

            self.assertEqual(roster[decision.team_id].status, TeamStatus.ASSIGNED)
            self.assertEqual(roster[decision.team_id].incident_id, busy.id)
            self.assertEqual(restored.team_location("medic_alpha"), "main_stage")
            restored_store.close()


class OnSiteResponseTests(unittest.TestCase):
    """A team that is already inside the venue never crosses a gate."""

    def dispatch_from_inside(self):
        _, service = build_service()
        first, _ = dispatch_at(service, "main_stage", Priority.HIGH)
        resolve(service, first.id)  # the team now stands at main_stage
        return service, dispatch_at(service, "central_plaza", Priority.HIGH)

    def test_a_route_without_a_gate_is_marked_on_site(self) -> None:
        _, (_, decision) = self.dispatch_from_inside()
        self.assertEqual(decision.selected_gate, ON_SITE)
        self.assertNotIn(ON_SITE, decision.route)

    def test_the_gate_event_is_refused_with_an_explanation(self) -> None:
        service, (incident, decision) = self.dispatch_from_inside()
        with self.assertRaises(IncidentStateError) as raised:
            service.record_geofence_event(
                incident.id, decision.team_id, ON_SITE, "entered_selected_gate"
            )
        self.assertIn("no gate crossing", str(raised.exception))

    def test_arrival_still_resolves_an_on_site_response(self) -> None:
        service, (incident, decision) = self.dispatch_from_inside()
        progress = service.record_geofence_event(
            incident.id, decision.team_id, "central_plaza", "reached_patient"
        )
        self.assertTrue(progress.completed)
        self.assertNotIn(decision.team_id, service._assignments)

    def test_no_geofence_subscription_is_registered_without_a_gate(self) -> None:
        service, (incident, _) = self.dispatch_from_inside()
        self.assertFalse(
            [target for target in service._nokia_geofences.values() if target[0] == incident.id]
        )

    def test_the_explanation_does_not_claim_a_gate_was_chosen(self) -> None:
        _, (_, decision) = self.dispatch_from_inside()
        self.assertIn("already inside the venue", decision.explanation)
        self.assertNotIn("On Site selected", decision.explanation)


class ResponderEndpointTests(unittest.TestCase):
    """The medical-team interface reads the live decision it has to follow."""

    def test_the_current_decision_is_readable_after_dispatch(self) -> None:
        _, service = build_service()
        incident, decision = dispatch_at(service, "main_stage", Priority.HIGH)
        self.assertEqual(service.get_decision(incident.id).route, decision.route)

    def test_an_undispatched_incident_has_no_decision(self) -> None:
        _, service = build_service()
        incident = service.create("main_stage", Priority.HIGH, "not dispatched")
        with self.assertRaises(ValueError):
            service.get_decision(incident.id)


if __name__ == "__main__":
    unittest.main()
