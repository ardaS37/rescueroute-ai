from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class IncidentStatus(str, Enum):
    REPORTED = "reported"
    DISPATCHED = "dispatched"
    RESOLVED = "resolved"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CreateIncidentRequest(BaseModel):
    location: str = Field(
        description="Venue graph node where the emergency occurred, e.g. 'main_stage'."
    )
    priority: Priority = Priority.HIGH
    description: str = Field(default="Medical emergency", max_length=500)


class Incident(BaseModel):
    id: str
    location: str
    priority: Priority
    description: str
    status: IncidentStatus = IncidentStatus.REPORTED


class RouteSegment(BaseModel):
    source: str
    destination: str
    distance_m: int
    zone: str | None = None


class RouteCostBreakdown(BaseModel):
    distance_seconds: int
    crowd_penalty_seconds: int
    network_penalty_seconds: int
    access_penalty_seconds: int = 0
    total_seconds: int


class GateRouteOption(BaseModel):
    gate: str
    eta_seconds: int | None = None
    route_distance_m: int | None = None
    available: bool
    reason: str | None = None


class RouteDecision(BaseModel):
    incident_id: str
    team_id: str
    selected_gate: str
    route: list[str]
    segments: list[RouteSegment]
    estimated_arrival_seconds: int
    route_distance_m: int
    cost_breakdown: RouteCostBreakdown
    gate_options: list[GateRouteOption]
    explanation: str
    api_calls: list[str]


class DispatchResponse(BaseModel):
    incident: Incident
    decision: RouteDecision


class AgentTrace(BaseModel):
    incident_id: str
    trigger: str
    model_source: Literal["gemini", "deterministic_fallback"]
    tool_plan: list[str]
    reasoning: str


class RouteHistoryEntry(BaseModel):
    occurred_at: datetime
    event_type: Literal["dispatch", "reroute"]
    trigger: str
    previous_route: list[str] | None = None
    previous_eta_seconds: int | None = None
    route: list[str]
    eta_seconds: int
    selected_gate: str
    explanation: str


class IncidentRouteHistory(BaseModel):
    incident_id: str
    entries: list[RouteHistoryEntry]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "rescueroute-ai-backend"


class AgentRuntimeStatus(BaseModel):
    provider: Literal["gemini", "deterministic_fallback"]
    model: str | None = None
    configured: bool


class ConfigureSimulationRequest(BaseModel):
    template: Literal["stadium_match", "music_festival", "pilgrimage_flow"] = "stadium_match"
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    crowd_pattern: Literal["balanced", "gate_surge", "stage_cluster"] = "gate_surge"


class ApplyScenarioRequest(BaseModel):
    scenario: Literal["normal", "gate_a_busy", "corridor_closed", "primary_team_unavailable", "hajj_tawaf_surge", "hajj_masaa_congestion"]


class AdvanceSimulationRequest(BaseModel):
    minutes: int = Field(default=5, ge=1, le=120)


class UpdateCongestionRequest(BaseModel):
    zone: str = Field(min_length=1)
    density: float = Field(ge=0, le=1, description="0 = clear, 1 = severely congested")


class CorridorStatusRequest(BaseModel):
    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    closed: bool = True


class GeofenceEventRequest(BaseModel):
    team_id: str = Field(default="medic_alpha", min_length=1)
    location: str = Field(min_length=1)
    event_type: Literal["entered_selected_gate", "reached_patient"]


class GeofenceEvent(BaseModel):
    event_type: str
    location: str
    occurred_at: datetime


class IncidentProgress(BaseModel):
    incident_id: str
    team_id: str
    last_location: str
    events: list[GeofenceEvent]
    completed: bool


class VenueTemplateSummary(BaseModel):
    key: str
    title: str
    description: str
    gates: list[str]
    locations: list[str]
    zones: list[str]


class VenueNode(BaseModel):
    id: str
    x: int
    y: int
    kind: Literal["gate", "landmark"]


class VenueEdge(BaseModel):
    source: str
    destination: str
    distance_m: int
    zone: str | None


class VenueLayout(BaseModel):
    template: str
    title: str
    width: int = 840
    height: int = 510
    nodes: list[VenueNode]
    edges: list[VenueEdge]


class SimulationState(BaseModel):
    template: str
    title: str
    seed: int
    crowd_pattern: str
    simulated_minutes: int
    zone_congestion: dict[str, float]
    crowd_distribution: dict[str, int]
    closed_corridors: list[str]
    active_scenario: str = "custom"
    device_status: dict[str, bool]
    network_load: float = 0.0
    network_source: Literal["live_nokia", "recorded_fallback", "simulation"] = "simulation"
    qos_active: bool = False
