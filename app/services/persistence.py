"""Small SQLite persistence layer for demo operations and audit history."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any


class SQLiteStore:
    """Persist the current simulation and incident audit records without new dependencies."""

    def __init__(self, database_path: str | None = None) -> None:
        self.path = database_path or os.getenv("RESCUEROUTE_DB_PATH", ":memory:")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS application_state (
                    state_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incident_records (
                    incident_id TEXT PRIMARY KEY,
                    incident_json TEXT NOT NULL,
                    decision_json TEXT,
                    progress_json TEXT,
                    history_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS geofence_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    gate TEXT NOT NULL
                );
            """)

    def save_state(self, key: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"))
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO application_state(state_key, payload) VALUES(?, ?) "
                "ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload",
                (key, encoded),
            )

    def load_state(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM application_state WHERE state_key=?", (key,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_incident(
        self, incident_id: str, incident: dict[str, Any], decision: dict[str, Any] | None,
        progress: dict[str, Any] | None, history: list[dict[str, Any]],
    ) -> None:
        values = (
            incident_id, json.dumps(incident, separators=(",", ":")),
            json.dumps(decision, separators=(",", ":")) if decision else None,
            json.dumps(progress, separators=(",", ":")) if progress else None,
            json.dumps(history, separators=(",", ":")),
        )
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO incident_records(incident_id, incident_json, decision_json, progress_json, history_json) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(incident_id) DO UPDATE SET "
                "incident_json=excluded.incident_json, decision_json=excluded.decision_json, "
                "progress_json=excluded.progress_json, history_json=excluded.history_json",
                values,
            )

    def load_incidents(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT incident_id, incident_json, decision_json, progress_json, history_json FROM incident_records"
            ).fetchall()
        return [{
            "incident_id": row["incident_id"],
            "incident": json.loads(row["incident_json"]),
            "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
            "progress": json.loads(row["progress_json"]) if row["progress_json"] else None,
            "history": json.loads(row["history_json"]),
        } for row in rows]

    def save_geofence_subscription(
        self, subscription_id: str, incident_id: str, team_id: str, gate: str
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO geofence_subscriptions(subscription_id, incident_id, team_id, gate) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(subscription_id) DO UPDATE SET incident_id=excluded.incident_id, "
                "team_id=excluded.team_id, gate=excluded.gate",
                (subscription_id, incident_id, team_id, gate),
            )

    def load_geofence_subscriptions(self) -> dict[str, tuple[str, str, str]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT subscription_id, incident_id, team_id, gate FROM geofence_subscriptions"
            ).fetchall()
        return {row["subscription_id"]: (row["incident_id"], row["team_id"], row["gate"]) for row in rows}

    def close(self) -> None:
        with self.lock:
            self.connection.close()
