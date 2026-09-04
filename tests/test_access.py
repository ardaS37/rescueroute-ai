"""Regression tests for the access code and per-visitor workspace isolation.

Before workspaces existed, every visitor drove the same simulation: loading a
scenario or switching venue changed what everyone else was looking at.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import access, security
from app.main import app, workspaces
from app.models import Priority, SimulationState
from app.services.persistence import SQLiteStore

CODE = {"RESCUEROUTE_ACCESS_CODE": "haram-2026", "RESCUEROUTE_SESSION_SECRET": "test-secret"}


class SessionTokenTests(unittest.TestCase):
    def test_a_valid_token_round_trips(self) -> None:
        with patch.dict(os.environ, CODE, clear=False):
            token = access.issue_session()
            self.assertIsNotNone(access.verify_session(token))

    def test_a_tampered_token_is_rejected(self) -> None:
        with patch.dict(os.environ, CODE, clear=False):
            session, expires, signature = access.issue_session().split(".")
            self.assertIsNone(access.verify_session(f"other.{expires}.{signature}"))
            self.assertIsNone(access.verify_session(f"{session}.{expires}.{signature[:-2]}xx"))
            self.assertIsNone(access.verify_session("nonsense"))
            self.assertIsNone(access.verify_session(None))

    def test_rotating_the_code_invalidates_old_sessions(self) -> None:
        with patch.dict(os.environ, CODE, clear=False):
            token = access.issue_session()
        with patch.dict(os.environ, {**CODE, "RESCUEROUTE_ACCESS_CODE": "rotated"}, clear=False):
            self.assertIsNone(access.verify_session(token))

    def test_an_expired_token_is_rejected(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), \
             patch.object(access, "SESSION_MAX_AGE_SECONDS", -1):
            self.assertIsNone(access.verify_session(access.issue_session()))


class AccessGateTests(unittest.TestCase):
    def setUp(self) -> None:
        security.reset_rate_limits()

    def tearDown(self) -> None:
        security.reset_rate_limits()

    def test_sign_in_does_not_need_a_multipart_parser(self) -> None:
        """The runtime image does not ship python-multipart."""
        with patch.dict(os.environ, CODE, clear=False), TestClient(app) as client:
            response = client.post(
                "/access",
                content="code=haram-2026",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn(access.SESSION_COOKIE, response.cookies)

    def test_the_demo_is_open_when_no_code_is_configured(self) -> None:
        with TestClient(app) as client:
            self.assertEqual(client.get("/simulation/state").status_code, 200)

    def test_a_gated_page_redirects_to_the_sign_in(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), TestClient(app) as client:
            response = client.get("/dashboard/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/access")

    def test_a_gated_api_answers_401_rather_than_redirecting(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), TestClient(app) as client:
            response = client.get("/simulation/state")
        self.assertEqual(response.status_code, 401)

    def test_the_health_probe_and_nokia_callbacks_stay_reachable(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            # Rejected on its own sink credential, not on the access gate.
            self.assertEqual(
                client.post("/webhooks/nokia/geofence", json={"type": "area-entered"}).status_code,
                401,
            )

    def test_the_right_code_opens_the_demo(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), TestClient(app) as client:
            response = client.post("/access", data={"code": "haram-2026"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertIn(access.SESSION_COOKIE, response.cookies)
            self.assertEqual(client.get("/simulation/state").status_code, 200)

    def test_the_wrong_code_does_not(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), TestClient(app) as client:
            response = client.post("/access", data={"code": "guess"}, follow_redirects=False)
            self.assertEqual(response.headers["location"], "/access?error=1")
            self.assertNotIn(access.SESSION_COOKIE, response.cookies)
            self.assertEqual(client.get("/simulation/state").status_code, 401)

    def test_guessing_the_code_is_rate_limited(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), \
             patch.object(security.SIMULATION_LIMITER, "burst", 3), \
             patch.object(security.SIMULATION_LIMITER, "per_minute", 1), \
             TestClient(app) as client:
            codes = [
                client.post("/access", data={"code": "guess"}, follow_redirects=False).status_code
                for _ in range(5)
            ]
        self.assertEqual(codes[-1], 429)


class WorkspaceIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        security.reset_rate_limits()

    def tearDown(self) -> None:
        security.reset_rate_limits()

    def sign_in(self, client: TestClient) -> None:
        client.post("/access", data={"code": "haram-2026"}, follow_redirects=False)

    def test_two_visitors_do_not_share_a_simulation(self) -> None:
        with patch.dict(os.environ, {**CODE, "GEMINI_API_KEY": ""}, clear=False):
            with TestClient(app) as first, TestClient(app) as second:
                self.sign_in(first)
                self.sign_in(second)

                first.post("/simulation/configure", json={
                    "template": "pilgrimage_flow", "seed": 42, "crowd_pattern": "balanced"})
                second.post("/simulation/configure", json={
                    "template": "music_festival", "seed": 7, "crowd_pattern": "stage_cluster"})

                self.assertEqual(first.get("/simulation/state").json()["template"], "pilgrimage_flow")
                self.assertEqual(second.get("/simulation/state").json()["template"], "music_festival")

    def test_an_incident_is_invisible_to_another_visitor(self) -> None:
        with patch.dict(os.environ, {**CODE, "GEMINI_API_KEY": ""}, clear=False):
            with TestClient(app) as first, TestClient(app) as second:
                self.sign_in(first)
                self.sign_in(second)
                first.post("/simulation/configure", json={
                    "template": "stadium_match", "seed": 42, "crowd_pattern": "balanced"})
                second.post("/simulation/configure", json={
                    "template": "stadium_match", "seed": 42, "crowd_pattern": "balanced"})

                incident = first.post("/incidents", json={
                    "location": "main_stage", "priority": "high", "description": "mine"}).json()

                self.assertEqual(first.get(f"/incidents/{incident['id']}").status_code, 200)
                self.assertEqual(second.get(f"/incidents/{incident['id']}").status_code, 404)

    def test_one_visitor_cannot_commit_another_visitors_teams(self) -> None:
        with patch.dict(os.environ, {**CODE, "GEMINI_API_KEY": ""}, clear=False):
            with TestClient(app) as first, TestClient(app) as second:
                self.sign_in(first)
                self.sign_in(second)
                for client in (first, second):
                    client.post("/simulation/configure", json={
                        "template": "stadium_match", "seed": 42, "crowd_pattern": "balanced"})
                for location in ("main_stage", "east_concourse", "first_aid"):
                    created = first.post("/incidents", json={
                        "location": location, "priority": "high", "description": "busy"}).json()
                    first.post(f"/incidents/{created['id']}/dispatch")

                self.assertTrue(all(t["status"] == "assigned" for t in first.get("/teams").json()))
                self.assertTrue(all(t["status"] == "available" for t in second.get("/teams").json()))

    def test_tabs_in_one_browser_share_the_same_workspace(self) -> None:
        with patch.dict(os.environ, CODE, clear=False), TestClient(app) as client:
            self.sign_in(client)
            client.post("/simulation/configure", json={
                "template": "pilgrimage_flow", "seed": 99, "crowd_pattern": "balanced"})
            # A second request with the same cookie jar is a second tab.
            self.assertEqual(client.get("/simulation/state").json()["seed"], 99)

    def test_a_visitor_socket_only_receives_their_own_events(self) -> None:
        with patch.dict(os.environ, {**CODE, "GEMINI_API_KEY": ""}, clear=False):
            with TestClient(app) as first, TestClient(app) as second:
                self.sign_in(first)
                self.sign_in(second)
                with second.websocket_connect("/ws/dashboard") as watcher:
                    watcher.receive_json()  # snapshot
                    first.post("/simulation/advance", json={"minutes": 5})
                    second.post("/simulation/scenarios", json={"scenario": "normal"})
                    event = watcher.receive_json()
        # The first visitor's advance must not have reached the second's socket.
        self.assertEqual(event["state"]["simulated_minutes"], 0)


class WorkspaceStoreTests(unittest.TestCase):
    def test_records_are_scoped_to_their_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(str(Path(directory) / "rescueroute.db"))
            alice, bob = store.scoped("alice"), store.scoped("bob")

            alice.save_state("simulation", {"template": "pilgrimage_flow"})
            bob.save_state("simulation", {"template": "music_festival"})
            alice.save_incident("inc-1", {"id": "inc-1"}, None, None, [])

            self.assertEqual(alice.load_state("simulation"), {"template": "pilgrimage_flow"})
            self.assertEqual(bob.load_state("simulation"), {"template": "music_festival"})
            self.assertEqual([r["incident_id"] for r in alice.load_incidents()], ["inc-1"])
            self.assertEqual(bob.load_incidents(), [])
            store.close()

    def test_a_database_from_before_workspaces_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.db")
            import sqlite3

            legacy = sqlite3.connect(path)
            try:
                legacy.executescript("""
                    CREATE TABLE application_state (state_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE incident_records (
                        incident_id TEXT PRIMARY KEY, incident_json TEXT NOT NULL,
                        decision_json TEXT, progress_json TEXT, history_json TEXT NOT NULL);
                    CREATE TABLE geofence_subscriptions (
                        subscription_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                        team_id TEXT NOT NULL, gate TEXT NOT NULL);
                """)
                legacy.execute(
                    "INSERT INTO incident_records VALUES('old', '{}', NULL, NULL, '[]')")
                legacy.commit()
            finally:
                # A context manager only commits; on Windows the file stays locked.
                legacy.close()

            store = SQLiteStore(path)
            self.assertEqual([r["incident_id"] for r in store.load_incidents()], ["old"])
            store.close()


class WorkspaceRegistryTests(unittest.TestCase):
    def test_capacity_is_bounded_and_the_quietest_is_retired(self) -> None:
        from app.workspace import WorkspaceRegistry

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(str(Path(directory) / "rescueroute.db"))
            with patch.dict(os.environ, {"RESCUEROUTE_MAX_WORKSPACES": "3"}, clear=False):
                registry = WorkspaceRegistry(store)
            for index in range(6):
                registry.get(f"visitor-{index}")
            self.assertLessEqual(registry.active, 3)
            store.close()

    def test_a_returning_visitor_gets_the_same_workspace(self) -> None:
        first = workspaces.get("returning-visitor")
        first.camara.configure("music_festival", seed=11, crowd_pattern="balanced")
        self.assertIs(workspaces.get("returning-visitor"), first)
        self.assertEqual(workspaces.get("returning-visitor").camara.state().seed, 11)

    def test_a_retired_workspace_reloads_its_saved_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from app.workspace import WorkspaceRegistry

            store = SQLiteStore(str(Path(directory) / "rescueroute.db"))
            registry = WorkspaceRegistry(store)
            space = registry.get("persistent")
            state = space.camara.configure("pilgrimage_flow", seed=5, crowd_pattern="balanced")
            space.persist_simulation(state)
            space.incidents.create("kaaba_tawaf", Priority.HIGH, "before restart")

            reopened = WorkspaceRegistry(store).get("persistent")
            self.assertEqual(reopened.camara.state().template, "pilgrimage_flow")
            self.assertEqual(reopened.camara.state().seed, 5)
            self.assertEqual(len(reopened.incidents._incidents), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
