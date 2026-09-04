import asyncio
import base64
import hmac
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.models import (AdvanceSimulationRequest, ApplyScenarioRequest, ConfigureSimulationRequest, CorridorStatusRequest,
    BulkCongestionRequest, CreateIncidentRequest, DispatchResponse, GeofenceEventRequest, HealthResponse, Incident,
    AgentRuntimeStatus, AgentTrace, IncidentProgress, IncidentRouteHistory, RouteDecision, SimulationState,
    TeamState, UpdateCongestionRequest, VenueLayout, VenueTemplateSummary)
from app import access
from app.security import (SIMULATION_LIMITER, dispatch_guard, log_startup_posture,
    rate_limiting_enabled, write_guard)
from app.workspace import Workspace, WorkspaceRegistry
from app.services.incident_service import IncidentNotFoundError, IncidentService
from app.services.persistence import SQLiteStore
from app.settings import load_local_env

load_local_env()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="RescueRoute AI API",
    version="0.1.0",
    description="Network-aware emergency routing prototype for crowded events.",
)

store = SQLiteStore()
workspaces = WorkspaceRegistry(store)
logger = logging.getLogger(__name__)
log_startup_posture()
access.log_startup_posture()


def _enforce_access_attempt(request: Request) -> None:
    """Bound how fast a shared code can be guessed."""
    if not rate_limiting_enabled():
        return
    client = request.client.host if request.client else "unknown"
    if SIMULATION_LIMITER.consume(f"access:{client}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many access attempts. Try again shortly.",
        )


def workspace_for(request: Request) -> Workspace:
    """Resolve the caller's isolated simulation."""
    return workspaces.get(access.session_id_for(request))


async def run_dispatch(
    space: Workspace, incident_id: str, trigger: str = "Initial emergency dispatch"
) -> DispatchResponse:
    """Run the agent pipeline off the event loop.

    The pipeline is synchronous end to end and performs outbound HTTP with
    second-scale timeouts (Nokia NaC via ``urlopen`` with retries, Gemini via
    ``httpx``).  Running it inline froze every WebSocket client and every other
    request for the duration of the slowest provider call.  The lock is per
    workspace, so one visitor's slow dispatch does not queue another's.
    """
    async with space.dispatch_lock:
        return await run_in_threadpool(space.agent.dispatch, incident_id, trigger)


async def dispatch_queued_incidents(space: Workspace) -> int:
    """Release waiting incidents, most urgent first, as teams become free.

    A mass event produces simultaneous calls, so an incident that arrives while
    every team is committed waits instead of being refused outright.
    """
    released, attempted = 0, set()
    while (incident_id := space.incidents.next_queued_incident(skip=attempted)):
        attempted.add(incident_id)
        try:
            response = await run_dispatch(
                space, incident_id, "Automatic dispatch: a response team became available"
            )
        except ValueError as error:
            logger.info("Queued incident %s could not be dispatched: %s", incident_id, error)
            continue
        await space.realtime.broadcast({
            "type": "agent_trace", "trace": space.agent.trace(incident_id).model_dump(mode="json")
        })
        await space.realtime.broadcast({
            "type": "dispatch", "response": response.model_dump(mode="json"), "from_queue": True
        })
        released += 1
    return released


async def cancel_incidents_outside_venue(space: Workspace) -> None:
    """Close out incidents that the newly loaded venue can no longer route to."""
    cancelled = space.incidents.cancel_incidents_outside_venue()
    space.incidents.reset_team_positions()
    if cancelled:
        logger.info("Cancelled %d incident(s) that are not part of the loaded venue", len(cancelled))
        await space.realtime.broadcast({"type": "incidents_cancelled", "incident_ids": cancelled})
    await dispatch_queued_incidents(space)


def verify_nokia_webhook(request: Request) -> None:
    """Accept Nokia's documented PLAIN sink credential without exposing it.

    QoD uses a bearer token, while Geofencing's ``PLAIN`` sink credential is
    delivered as HTTP Basic authentication (identifier + secret).  Supporting
    both keeps the receiver compatible with the two Nokia callback styles.
    """
    expected = os.getenv("NAC_WEBHOOK_TOKEN", "").strip()
    provided = request.headers.get("authorization", "")
    allow_unsigned_simulator = os.getenv("NAC_SIMULATOR_ALLOW_UNSIGNED_CALLBACKS", "").lower() == "true"
    if expected and not provided and allow_unsigned_simulator:
        logger.warning("Accepted unsigned Nokia simulator callback because simulator mode is explicitly enabled")
        return
    basic_value = base64.b64encode(f"rescueroute-ai:{expected}".encode("utf-8")).decode("ascii")
    valid_credentials = (f"Bearer {expected}", f"Basic {basic_value}")
    if not expected or not any(hmac.compare_digest(provided, candidate) for candidate in valid_credentials):
        scheme = provided.split(" ", 1)[0] if provided else "missing"
        logger.warning("Rejected Nokia webhook credential (scheme=%s, value_length=%d)", scheme, len(provided))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Nokia webhook credential")


async def reroute_affected_incidents(
    space: Workspace, trigger: str, *, zones: set[str] | None = None,
    corridor: tuple[str, str] | None = None,
) -> int:
    """Automatically reroute only active teams whose existing route was affected."""
    rerouted = 0
    for incident_id in space.incidents.affected_active_incidents(zones=zones, corridor=corridor):
        try:
            response = await run_dispatch(space, incident_id, trigger)
        except ValueError as error:  # RouteNotFoundError / IncidentStateError
            await space.realtime.broadcast({
                "type": "reroute_alert", "incident_id": incident_id,
                "message": f"Automatic reroute unavailable: {error}",
            })
            continue
        await space.realtime.broadcast({
            "type": "agent_trace", "trace": space.agent.trace(incident_id).model_dump(mode="json")
        })
        await space.realtime.broadcast({"type": "reroute", "response": response.model_dump(mode="json"), "automatic": True})
        rerouted += 1
    return rerouted


@app.middleware("http")
async def require_access_code(request: Request, call_next):
    """Keep the demo behind a shared access code when one is configured."""
    if not access.is_gated() or access.is_public_path(request.url.path):
        return await call_next(request)
    if access.verify_session(request.cookies.get(access.SESSION_COOKIE)):
        return await call_next(request)
    if request.url.path.startswith(("/dashboard", "/docs", "/redoc", "/openapi.json")) or request.url.path == "/":
        return RedirectResponse("/access", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        {"detail": "An access code is required. Open /access to sign in."},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.get("/access", include_in_schema=False)
def access_page(request: Request) -> Response:
    if not access.is_gated() or access.verify_session(request.cookies.get(access.SESSION_COOKIE)):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return FileResponse(FRONTEND_DIR / "access.html")


@app.post("/access", include_in_schema=False)
async def submit_access_code(request: Request) -> Response:
    """Exchange the shared code for a session, which is also the workspace key."""
    # Parsed by hand so the sign-in works without pulling in python-multipart,
    # which is not part of the runtime image.
    body = (await request.body()).decode("utf-8", "replace")
    submitted = parse_qs(body).get("code", [""])[0]
    _enforce_access_attempt(request)
    if not access.is_gated() or not access.code_matches(submitted):
        logger.warning("Rejected access attempt from %s", request.client.host if request.client else "unknown")
        return RedirectResponse("/access?error=1", status_code=status.HTTP_303_SEE_OTHER)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        access.SESSION_COOKIE,
        access.issue_session(),
        max_age=access.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "menu.html")


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/agent/status", response_model=AgentRuntimeStatus, tags=["AI agent"])
def agent_status() -> AgentRuntimeStatus:
    """Expose readiness without ever returning the Gemini API key."""
    return emergency_agent.runtime_status()


@app.get("/teams", response_model=list[TeamState], tags=["teams"])
def list_teams(space: Workspace = Depends(workspace_for)) -> list[TeamState]:
    """Roster for the loaded venue: where each team is and what holds it."""
    return space.incidents.teams()


@app.post("/incidents", response_model=Incident, status_code=status.HTTP_201_CREATED,
    tags=["incidents"], dependencies=[Depends(write_guard)])
async def create_incident(payload: CreateIncidentRequest, space: Workspace = Depends(workspace_for)) -> Incident:
    if not space.camara.is_known_node(payload.location):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown venue location '{payload.location}'.",
        )
    incident = space.incidents.create(payload.location, payload.priority, payload.description)
    await space.realtime.broadcast({"type": "incident_created", "incident": incident.model_dump(mode="json")})
    return incident


@app.get("/incidents/{incident_id}", response_model=Incident, tags=["incidents"])
def get_incident(incident_id: str, space: Workspace = Depends(workspace_for)) -> Incident:
    try:
        return space.incidents.get(incident_id)
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")


@app.post("/incidents/{incident_id}/dispatch", response_model=DispatchResponse,
    tags=["incidents"], dependencies=[Depends(dispatch_guard)])
async def dispatch_incident(incident_id: str, space: Workspace = Depends(workspace_for)) -> DispatchResponse:
    try:
        response = await run_dispatch(space, incident_id)
        await space.realtime.broadcast({"type": "agent_trace", "trace": space.agent.trace(incident_id).model_dump(mode="json")})
        await space.realtime.broadcast({"type": "dispatch", "response": response.model_dump(mode="json")})
        return response
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except ValueError as error:  # RouteNotFoundError / IncidentStateError
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.post("/incidents/{incident_id}/recalculate-route", response_model=DispatchResponse,
    tags=["incidents"], dependencies=[Depends(dispatch_guard)])
async def recalculate_route(incident_id: str, space: Workspace = Depends(workspace_for)) -> DispatchResponse:
    """Re-evaluate the active incident after a crowd or access event."""
    try:
        response = await run_dispatch(space, incident_id, "Crowd density or access conditions changed")
        await space.realtime.broadcast({"type": "agent_trace", "trace": space.agent.trace(incident_id).model_dump(mode="json")})
        await space.realtime.broadcast({"type": "reroute", "response": response.model_dump(mode="json")})
        return response
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except ValueError as error:  # RouteNotFoundError / IncidentStateError
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.get("/incidents/{incident_id}/decision", response_model=RouteDecision, tags=["incidents"])
def get_incident_decision(incident_id: str, space: Workspace = Depends(workspace_for)) -> RouteDecision:
    """The route the assigned team is currently following.

    The responder interface needs the live decision, which until now existed
    only in the reply to the dispatch call that produced it.
    """
    try:
        return space.incidents.get_decision(incident_id)
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.get("/incidents/{incident_id}/history", response_model=IncidentRouteHistory, tags=["incidents"])
def get_incident_history(incident_id: str, space: Workspace = Depends(workspace_for)) -> IncidentRouteHistory:
    try:
        return space.incidents.get_history(incident_id)
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")


@app.get("/incidents/{incident_id}/agent-trace", response_model=AgentTrace, tags=["AI agent"])
def get_agent_trace(incident_id: str, space: Workspace = Depends(workspace_for)) -> AgentTrace:
    try:
        return space.agent.trace(incident_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No agent trace recorded for this incident")


@app.get("/incidents/{incident_id}/progress", response_model=IncidentProgress, tags=["incidents"])
def get_incident_progress(incident_id: str, space: Workspace = Depends(workspace_for)) -> IncidentProgress:
    try:
        return space.incidents.get_progress(incident_id)
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.post("/incidents/{incident_id}/events/geofence", response_model=IncidentProgress,
    tags=["incidents"], dependencies=[Depends(write_guard)])
async def record_geofence_event(
    incident_id: str, payload: GeofenceEventRequest, space: Workspace = Depends(workspace_for)
) -> IncidentProgress:
    if not space.camara.is_known_node(payload.location):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown venue location")
    try:
        progress = space.incidents.record_geofence_event(
            incident_id, payload.team_id, payload.location, payload.event_type
        )
        await space.realtime.broadcast({
            "type": "geofence_progress",
            "progress": progress.model_dump(mode="json"),
            "incident": space.incidents.get(incident_id).model_dump(mode="json"),
        })
        await dispatch_queued_incidents(space)
        return progress
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.post("/webhooks/nokia/geofence", include_in_schema=False)
async def nokia_geofence_webhook(request: Request) -> dict[str, str]:
    """Public Nokia callback receiver linked to the active emergency route."""
    verify_nokia_webhook(request)
    payload = await request.json()
    # A callback carries a subscription id, not a session cookie, so every
    # workspace is offered it and only the one that owns it acts.
    for space in workspaces.all():
        progress, status_message = space.incidents.process_nokia_geofence_callback(payload)
        if not progress:
            continue
        await space.realtime.broadcast({
            "type": "geofence_progress",
            "progress": progress.model_dump(mode="json"),
            "incident": space.incidents.get(progress.incident_id).model_dump(mode="json"),
            "source": "nokia_geofencing",
        })
        await dispatch_queued_incidents(space)
        return {"status": status_message}
    return {"status": "ignored: unknown or expired geofence subscription"}


@app.post("/webhooks/nokia/qod", include_in_schema=False)
async def nokia_qod_webhook(request: Request) -> dict[str, str]:
    verify_nokia_webhook(request)
    await request.json()
    return {"status": "accepted"}


@app.get("/simulation/templates", response_model=list[VenueTemplateSummary], tags=["simulation"])
def list_templates(space: Workspace = Depends(workspace_for)) -> list[VenueTemplateSummary]:
    return space.camara.templates()


@app.get("/simulation/layout", response_model=VenueLayout, tags=["simulation"])
def get_simulation_layout(space: Workspace = Depends(workspace_for)) -> VenueLayout:
    return space.camara.layout()


@app.get("/simulation/state", response_model=SimulationState, tags=["simulation"])
def get_simulation_state(space: Workspace = Depends(workspace_for)) -> SimulationState:
    return space.camara.state()


@app.post("/simulation/configure", response_model=SimulationState,
    tags=["simulation"], dependencies=[Depends(write_guard)])
async def configure_simulation(payload: ConfigureSimulationRequest, space: Workspace = Depends(workspace_for)) -> SimulationState:
    simulation_state = space.camara.configure(payload.template, payload.seed, payload.crowd_pattern)
    space.persist_simulation(simulation_state)
    await cancel_incidents_outside_venue(space)
    await space.realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
    return simulation_state


@app.post("/simulation/scenarios", response_model=SimulationState,
    tags=["simulation"], dependencies=[Depends(write_guard)])
async def apply_recorded_scenario(payload: ApplyScenarioRequest, space: Workspace = Depends(workspace_for)) -> SimulationState:
    """Run a deterministic fallback scenario when live network APIs are unavailable."""
    simulation_state = space.camara.apply_scenario(payload.scenario)
    space.persist_simulation(simulation_state)
    await cancel_incidents_outside_venue(space)
    await space.realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
    return simulation_state


@app.post("/simulation/advance", response_model=SimulationState,
    tags=["simulation"], dependencies=[Depends(dispatch_guard)])
async def advance_simulation(payload: AdvanceSimulationRequest, space: Workspace = Depends(workspace_for)) -> SimulationState:
    simulation_state = space.camara.advance(payload.minutes)
    space.persist_simulation(simulation_state)
    await space.realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
    await reroute_affected_incidents(
        space,
        f"Automatic reroute: crowd conditions changed after {payload.minutes} simulated minutes",
        zones=set(simulation_state.zone_congestion),
    )
    return simulation_state


@app.post("/simulation/events/congestion", response_model=SimulationState,
    tags=["simulation events"], dependencies=[Depends(dispatch_guard)])
async def update_congestion(payload: UpdateCongestionRequest, space: Workspace = Depends(workspace_for)) -> SimulationState:
    try:
        simulation_state = space.camara.update_congestion(payload.zone, payload.density)
        space.persist_simulation(simulation_state)
        await space.realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
        await reroute_affected_incidents(
            space,
            f"Automatic reroute: congestion changed in {payload.zone.replace('_', ' ')}",
            zones={payload.zone},
        )
        return simulation_state
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))


@app.post("/simulation/events/congestion/batch", response_model=SimulationState,
    tags=["simulation events"], dependencies=[Depends(dispatch_guard)])
async def update_congestion_batch(payload: BulkCongestionRequest, space: Workspace = Depends(workspace_for)) -> SimulationState:
    """Receive one whole-venue 2D pressure estimate and reroute at most once."""
    try:
        simulation_state = space.camara.update_congestion_many(payload.zone_densities)
        space.persist_simulation(simulation_state)
        await space.realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
        await reroute_affected_incidents(
            space,
            "Automatic reroute: network-informed 2D pressure estimate changed",
            zones=set(payload.zone_densities),
        )
        return simulation_state
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))


@app.post("/simulation/events/corridor", response_model=SimulationState,
    tags=["simulation events"], dependencies=[Depends(dispatch_guard)])
async def update_corridor(payload: CorridorStatusRequest, space: Workspace = Depends(workspace_for)) -> SimulationState:
    try:
        simulation_state = space.camara.set_corridor_status(
            payload.source, payload.destination, payload.closed, payload.restricted
        )
        space.persist_simulation(simulation_state)
        await space.realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
        action = "closed" if payload.closed else "restricted" if payload.restricted else "reopened"
        await reroute_affected_incidents(
            space,
            f"Automatic reroute: {payload.source.replace('_', ' ')} to {payload.destination.replace('_', ' ')} {action}",
            corridor=(payload.source, payload.destination),
        )
        return simulation_state
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))


@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket) -> None:
    """Push simulation and incident updates to this visitor's own dashboards."""
    space = workspaces.get(access.session_id_for(websocket))
    await space.realtime.connect(websocket)
    try:
        await websocket.send_json({"type": "snapshot", "state": space.camara.state().model_dump(mode="json")})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a transport fault must still release the slot
        logger.debug("Dashboard socket ended with an unexpected error", exc_info=True)
    finally:
        space.realtime.disconnect(websocket)


app.mount("/dashboard", StaticFiles(directory=FRONTEND_DIR, html=True), name="dashboard")
