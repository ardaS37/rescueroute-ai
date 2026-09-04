import hmac
import os

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models import (AdvanceSimulationRequest, ApplyScenarioRequest, ConfigureSimulationRequest, CorridorStatusRequest,
    BulkCongestionRequest, CreateIncidentRequest, DispatchResponse, GeofenceEventRequest, HealthResponse, Incident,
    AgentRuntimeStatus, AgentTrace, IncidentProgress, IncidentRouteHistory, SimulationState,
    UpdateCongestionRequest, VenueLayout, VenueTemplateSummary)
from app.services.camara_simulator import CamaraSimulator
from app.services.incident_service import IncidentNotFoundError, IncidentService
from app.services.persistence import SQLiteStore
from app.services.emergency_agent import EmergencyAgent
from app.services.routing import RouteNotFoundError, RoutingService
from app.services.realtime import DashboardBroadcaster
from app.settings import load_local_env

load_local_env()

app = FastAPI(
    title="RescueRoute AI API",
    version="0.1.0",
    description="Network-aware emergency routing prototype for crowded events.",
)

store = SQLiteStore()
camara = CamaraSimulator()
saved_simulation = store.load_state("simulation")
if saved_simulation:
    camara.restore_state(SimulationState.model_validate(saved_simulation))
incident_service = IncidentService(camara, RoutingService(camara), store)
emergency_agent = EmergencyAgent(incident_service)
realtime = DashboardBroadcaster()


def persist_simulation(simulation_state: SimulationState) -> None:
    store.save_state("simulation", simulation_state.model_dump(mode="json"))


def verify_nokia_webhook(request: Request) -> None:
    """Accept only Nokia callbacks carrying our configured Bearer sink credential."""
    expected = os.getenv("NAC_WEBHOOK_TOKEN", "").strip()
    provided = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Nokia webhook credential")


async def reroute_affected_incidents(
    trigger: str, *, zones: set[str] | None = None, corridor: tuple[str, str] | None = None
) -> int:
    """Automatically reroute only active teams whose existing route was affected."""
    rerouted = 0
    for incident_id in incident_service.affected_active_incidents(zones=zones, corridor=corridor):
        try:
            response = emergency_agent.dispatch(incident_id, trigger=trigger)
        except RouteNotFoundError as error:
            await realtime.broadcast({
                "type": "reroute_alert", "incident_id": incident_id,
                "message": f"Automatic reroute unavailable: {error}",
            })
            continue
        await realtime.broadcast({
            "type": "agent_trace", "trace": emergency_agent.trace(incident_id).model_dump(mode="json")
        })
        await realtime.broadcast({"type": "reroute", "response": response.model_dump(mode="json"), "automatic": True})
        rerouted += 1
    return rerouted


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse("frontend/menu.html")


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/agent/status", response_model=AgentRuntimeStatus, tags=["AI agent"])
def agent_status() -> AgentRuntimeStatus:
    """Expose readiness without ever returning the Gemini API key."""
    return emergency_agent.runtime_status()


@app.post("/incidents", response_model=Incident, status_code=status.HTTP_201_CREATED, tags=["incidents"])
async def create_incident(payload: CreateIncidentRequest) -> Incident:
    if not camara.is_known_node(payload.location):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown venue location '{payload.location}'.",
        )
    incident = incident_service.create(payload.location, payload.priority, payload.description)
    await realtime.broadcast({"type": "incident_created", "incident": incident.model_dump(mode="json")})
    return incident


@app.get("/incidents/{incident_id}", response_model=Incident, tags=["incidents"])
def get_incident(incident_id: str) -> Incident:
    try:
        return incident_service.get(incident_id)
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")


@app.post("/incidents/{incident_id}/dispatch", response_model=DispatchResponse, tags=["incidents"])
async def dispatch_incident(incident_id: str) -> DispatchResponse:
    try:
        response = emergency_agent.dispatch(incident_id)
        await realtime.broadcast({"type": "agent_trace", "trace": emergency_agent.trace(incident_id).model_dump(mode="json")})
        await realtime.broadcast({"type": "dispatch", "response": response.model_dump(mode="json")})
        return response
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except RouteNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.post("/incidents/{incident_id}/recalculate-route", response_model=DispatchResponse, tags=["incidents"])
async def recalculate_route(incident_id: str) -> DispatchResponse:
    """Re-evaluate the active incident after a crowd or access event."""
    try:
        response = emergency_agent.dispatch(incident_id, trigger="Crowd density or access conditions changed")
        await realtime.broadcast({"type": "agent_trace", "trace": emergency_agent.trace(incident_id).model_dump(mode="json")})
        await realtime.broadcast({"type": "reroute", "response": response.model_dump(mode="json")})
        return response
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except RouteNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.get("/incidents/{incident_id}/history", response_model=IncidentRouteHistory, tags=["incidents"])
def get_incident_history(incident_id: str) -> IncidentRouteHistory:
    try:
        return incident_service.get_history(incident_id)
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")


@app.get("/incidents/{incident_id}/agent-trace", response_model=AgentTrace, tags=["AI agent"])
def get_agent_trace(incident_id: str) -> AgentTrace:
    try:
        return emergency_agent.trace(incident_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No agent trace recorded for this incident")


@app.get("/incidents/{incident_id}/progress", response_model=IncidentProgress, tags=["incidents"])
def get_incident_progress(incident_id: str) -> IncidentProgress:
    try:
        return incident_service.get_progress(incident_id)
    except IncidentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@app.post("/incidents/{incident_id}/events/geofence", response_model=IncidentProgress, tags=["incidents"])
async def record_geofence_event(incident_id: str, payload: GeofenceEventRequest) -> IncidentProgress:
    if not camara.is_known_node(payload.location):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown venue location")
    try:
        progress = incident_service.record_geofence_event(
            incident_id, payload.team_id, payload.location, payload.event_type
        )
        await realtime.broadcast({
            "type": "geofence_progress",
            "progress": progress.model_dump(mode="json"),
            "incident": incident_service.get(incident_id).model_dump(mode="json"),
        })
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
    progress, status_message = incident_service.process_nokia_geofence_callback(payload)
    if progress:
        await realtime.broadcast({
            "type": "geofence_progress",
            "progress": progress.model_dump(mode="json"),
            "incident": incident_service.get(progress.incident_id).model_dump(mode="json"),
            "source": "nokia_geofencing",
        })
    return {"status": status_message}


@app.post("/webhooks/nokia/qod", include_in_schema=False)
async def nokia_qod_webhook(request: Request) -> dict[str, str]:
    verify_nokia_webhook(request)
    await request.json()
    return {"status": "accepted"}


@app.get("/simulation/templates", response_model=list[VenueTemplateSummary], tags=["simulation"])
def list_templates() -> list[VenueTemplateSummary]:
    return camara.templates()


@app.get("/simulation/layout", response_model=VenueLayout, tags=["simulation"])
def get_simulation_layout() -> VenueLayout:
    return camara.layout()


@app.get("/simulation/state", response_model=SimulationState, tags=["simulation"])
def get_simulation_state() -> SimulationState:
    return camara.state()


@app.post("/simulation/configure", response_model=SimulationState, tags=["simulation"])
async def configure_simulation(payload: ConfigureSimulationRequest) -> SimulationState:
    simulation_state = camara.configure(payload.template, payload.seed, payload.crowd_pattern)
    persist_simulation(simulation_state)
    await realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
    return simulation_state


@app.post("/simulation/scenarios", response_model=SimulationState, tags=["simulation"])
async def apply_recorded_scenario(payload: ApplyScenarioRequest) -> SimulationState:
    """Run a deterministic fallback scenario when live network APIs are unavailable."""
    simulation_state = camara.apply_scenario(payload.scenario)
    persist_simulation(simulation_state)
    await realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
    return simulation_state


@app.post("/simulation/advance", response_model=SimulationState, tags=["simulation"])
async def advance_simulation(payload: AdvanceSimulationRequest) -> SimulationState:
    simulation_state = camara.advance(payload.minutes)
    persist_simulation(simulation_state)
    await realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
    await reroute_affected_incidents(
        f"Automatic reroute: crowd conditions changed after {payload.minutes} simulated minutes",
        zones=set(simulation_state.zone_congestion),
    )
    return simulation_state


@app.post("/simulation/events/congestion", response_model=SimulationState, tags=["simulation events"])
async def update_congestion(payload: UpdateCongestionRequest) -> SimulationState:
    try:
        simulation_state = camara.update_congestion(payload.zone, payload.density)
        persist_simulation(simulation_state)
        await realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
        await reroute_affected_incidents(
            f"Automatic reroute: congestion changed in {payload.zone.replace('_', ' ')}",
            zones={payload.zone},
        )
        return simulation_state
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))


@app.post("/simulation/events/congestion/batch", response_model=SimulationState, tags=["simulation events"])
async def update_congestion_batch(payload: BulkCongestionRequest) -> SimulationState:
    """Receive one whole-venue 2D pressure estimate and reroute at most once."""
    try:
        simulation_state = camara.update_congestion_many(payload.zone_densities)
        persist_simulation(simulation_state)
        await realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
        await reroute_affected_incidents(
            "Automatic reroute: network-informed 2D pressure estimate changed",
            zones=set(payload.zone_densities),
        )
        return simulation_state
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))


@app.post("/simulation/events/corridor", response_model=SimulationState, tags=["simulation events"])
async def update_corridor(payload: CorridorStatusRequest) -> SimulationState:
    try:
        simulation_state = camara.set_corridor_status(payload.source, payload.destination, payload.closed)
        persist_simulation(simulation_state)
        await realtime.broadcast({"type": "simulation_state", "state": simulation_state.model_dump(mode="json")})
        action = "closed" if payload.closed else "reopened"
        await reroute_affected_incidents(
            f"Automatic reroute: {payload.source.replace('_', ' ')} to {payload.destination.replace('_', ' ')} {action}",
            corridor=(payload.source, payload.destination),
        )
        return simulation_state
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))


@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket) -> None:
    """Push simulation and incident updates to all connected dashboards."""
    await realtime.connect(websocket)
    await websocket.send_json({"type": "snapshot", "state": camara.state().model_dump(mode="json")})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        realtime.disconnect(websocket)


app.mount("/dashboard", StaticFiles(directory="frontend", html=True), name="dashboard")
