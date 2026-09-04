"""Per-visitor simulations.

Every visitor to the demo used to drive the same `CamaraSimulator`, so loading a
scenario, switching venue or closing a corridor changed what everyone else was
looking at.  A workspace bundles one simulation with the incidents, agent and
dashboard sockets that belong to it, keyed by the visitor's session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from app.models import SimulationState
from app.services.camara_simulator import CamaraSimulator
from app.services.emergency_agent import EmergencyAgent
from app.services.incident_service import IncidentService
from app.services.persistence import SQLiteStore
from app.services.realtime import DashboardBroadcaster
from app.services.routing import RoutingService

logger = logging.getLogger(__name__)


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class Workspace:
    """One isolated simulation and everything attached to it."""

    id: str
    camara: CamaraSimulator
    incidents: IncidentService
    agent: EmergencyAgent
    realtime: DashboardBroadcaster
    # Serialises this workspace's dispatch pipeline; workspaces do not block
    # each other.
    dispatch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.touched_at = time.monotonic()

    def persist_simulation(self, state: SimulationState) -> None:
        self.incidents.store.save_state("simulation", state.model_dump(mode="json"))


class WorkspaceRegistry:
    """Creates workspaces on demand and retires the ones nobody is using."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.max_workspaces = _positive_int("RESCUEROUTE_MAX_WORKSPACES", 40)
        self.idle_seconds = _positive_int("RESCUEROUTE_WORKSPACE_TTL_SECONDS", 6 * 60 * 60)
        self._workspaces: dict[str, Workspace] = {}

    def get(self, session_id: str) -> Workspace:
        existing = self._workspaces.get(session_id)
        if existing is not None:
            existing.touch()
            return existing
        self._retire_idle()
        workspace = self._build(session_id)
        self._workspaces[session_id] = workspace
        logger.info("Opened workspace %s (%d active)", session_id, len(self._workspaces))
        return workspace

    def _build(self, session_id: str) -> Workspace:
        store = self.store.scoped(session_id)
        camara = CamaraSimulator()
        saved = store.load_state("simulation")
        if saved:
            camara.restore_state(SimulationState.model_validate(saved))
        incidents = IncidentService(camara, RoutingService(camara), store)
        return Workspace(
            id=session_id,
            camara=camara,
            incidents=incidents,
            agent=EmergencyAgent(incidents),
            realtime=DashboardBroadcaster(),
        )

    def _retire_idle(self) -> None:
        """Drop workspaces that have gone quiet, oldest first when over capacity.

        Only the in-memory objects are released; the audit records stay in the
        database, so returning with the same session cookie restores the state.
        """
        now = time.monotonic()
        for session_id, workspace in list(self._workspaces.items()):
            if workspace.realtime.connection_count == 0 and now - workspace.touched_at > self.idle_seconds:
                del self._workspaces[session_id]
        while len(self._workspaces) >= self.max_workspaces:
            oldest = min(self._workspaces.values(), key=lambda space: space.touched_at)
            del self._workspaces[oldest.id]
            logger.info("Retired workspace %s to stay within capacity", oldest.id)

    def all(self) -> list[Workspace]:
        """Every live workspace, for callbacks that arrive without a session."""
        return list(self._workspaces.values())

    @property
    def active(self) -> int:
        return len(self._workspaces)
