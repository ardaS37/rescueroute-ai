/**
 * RescueRoute AI - medical-team interface.
 *
 * The control centre answers "which gate should we send them through".  This
 * view answers the responder's question instead: what am I assigned to, where
 * do I go, and how do I report that I got there.  It is built for a phone held
 * in one hand while walking.
 */

// ---------------------------------------------------------------- API types

type TeamStatus = "available" | "assigned" | "unreachable";
type IncidentStatus = "reported" | "dispatched" | "resolved" | "queued" | "cancelled";
type Priority = "low" | "medium" | "high" | "critical";

interface TeamState {
  id: string;
  name: string;
  location: string;
  status: TeamStatus;
  incident_id: string | null;
}

interface Incident {
  id: string;
  location: string;
  priority: Priority;
  description: string;
  status: IncidentStatus;
}

interface RouteSegment {
  source: string;
  destination: string;
  distance_m: number;
  zone: string | null;
}

interface RouteCostBreakdown {
  distance_seconds: number;
  crowd_penalty_seconds: number;
  network_penalty_seconds: number;
  access_penalty_seconds: number;
  total_seconds: number;
}

interface RouteDecision {
  incident_id: string;
  team_id: string;
  selected_gate: string;
  route: string[];
  segments: RouteSegment[];
  estimated_arrival_seconds: number;
  route_distance_m: number;
  cost_breakdown: RouteCostBreakdown;
  explanation: string;
}

interface GeofenceEvent {
  event_type: string;
  location: string;
}

interface IncidentProgress {
  incident_id: string;
  team_id: string;
  last_location: string;
  events: GeofenceEvent[];
  completed: boolean;
}

interface SimulationState {
  title: string;
  zone_congestion: Record<string, number>;
  network_source: "live_nokia" | "recorded_fallback" | "simulation";
  network_load: number;
  qos_active: boolean;
}

/** Anything the dashboard broadcasts that this view reacts to. */
type LiveEvent =
  | { type: "dispatch" | "reroute"; response: { incident: Incident; decision: RouteDecision } }
  | { type: "geofence_progress"; incident: Incident; progress: IncidentProgress }
  | { type: "incidents_cancelled"; incident_ids: string[] }
  | { type: "snapshot" | "simulation_state"; state: SimulationState }
  | { type: string; [key: string]: unknown };

const ON_SITE = "on_site";
const TEAM_STORAGE_KEY = "rescueroute.medic.team";

// ------------------------------------------------------------------- state

let teams: TeamState[] = [];
let teamId: string | null = null;
let incident: Incident | null = null;
let decision: RouteDecision | null = null;
let progress: IncidentProgress | null = null;
let simulation: SimulationState | null = null;
/** Keeps the arrival confirmation on screen after the team has been released. */
let completed: { incident: Incident; decision: RouteDecision; progress: IncidentProgress } | null = null;
let socket: WebSocket | null = null;
let reconnectTimer = 0;

// ------------------------------------------------------------------ helpers

function el(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (!found) throw new Error(`missing element #${id}`);
  return found;
}

function label(value: string): string {
  return value.replace(/_/g, " ");
}

function titleCase(value: string): string {
  return label(value).replace(/\b\w/g, (character) => character.toUpperCase());
}

function minutes(seconds: number): string {
  if (seconds < 60) return `${seconds} sec`;
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} sec`;
}

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* a proxy error page is not JSON; keep the status text */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

function setStatus(message: string, tone: "ok" | "warn" | "idle" = "idle"): void {
  const banner = el("connection");
  banner.textContent = message;
  banner.className = `connection ${tone}`;
}

// -------------------------------------------------------------------- load

async function loadTeams(): Promise<void> {
  teams = await api<TeamState[]>("/teams");
  const picker = el("team") as HTMLSelectElement;
  const previous = teamId ?? localStorage.getItem(TEAM_STORAGE_KEY);
  picker.innerHTML = teams
    .map((team) => `<option value="${team.id}">${team.name}</option>`)
    .join("");
  teamId = teams.some((team) => team.id === previous) ? previous : (teams[0]?.id ?? null);
  if (teamId) picker.value = teamId;
}

/** Pull everything this responder needs for their current assignment. */
async function loadAssignment(): Promise<void> {
  const team = teams.find((candidate) => candidate.id === teamId) ?? null;
  // Resolving an incident frees the team immediately, so hold the arrival
  // confirmation until this responder is actually given something else.
  if (incident && decision && progress?.completed) {
    completed = { incident, decision, progress };
  }
  incident = null;
  decision = null;
  progress = null;

  if (team?.incident_id) {
    completed = null;
    incident = await api<Incident>(`/incidents/${team.incident_id}`);
    try {
      decision = await api<RouteDecision>(`/incidents/${team.incident_id}/decision`);
      progress = await api<IncidentProgress>(`/incidents/${team.incident_id}/progress`);
    } catch {
      /* dispatched but not yet scored; the header still shows the assignment */
    }
  } else if (completed && completed.progress.team_id === teamId) {
    ({ incident, decision, progress } = completed);
  }
  render();
}

async function refreshAll(): Promise<void> {
  try {
    [simulation] = await Promise.all([api<SimulationState>("/simulation/state"), loadTeams()]);
    await loadAssignment();
  } catch (error) {
    setStatus((error as Error).message, "warn");
  }
}

// ------------------------------------------------------------------ render

function renderRoute(): string {
  if (!decision) return "";
  const reached = new Set(progress?.events.map((event) => event.location) ?? []);
  const steps = decision.route.map((node, index) => {
    const segment = decision!.segments[index - 1];
    const isGate = node === decision!.selected_gate;
    const isTarget = index === decision!.route.length - 1;
    const done = reached.has(node) || node === progress?.last_location;
    const zone = segment?.zone ? `${label(segment.zone)}` : "";
    const congestion =
      segment?.zone && simulation
        ? Math.round((simulation.zone_congestion[segment.zone] ?? 0) * 100)
        : null;
    const meta = [
      segment ? `${segment.distance_m} m` : "start",
      zone,
      congestion !== null ? `${congestion}% crowd` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const tag = isTarget ? "PATIENT" : isGate ? "ENTRY GATE" : "";
    return `<li class="${done ? "done" : ""} ${isTarget ? "target" : ""} ${isGate ? "gate" : ""}">
      <b>${titleCase(node)}</b>${tag ? `<em>${tag}</em>` : ""}
      <span>${meta}</span>
    </li>`;
  });
  return `<ol class="route">${steps.join("")}</ol>`;
}

function renderNetwork(): string {
  if (!simulation) return "";
  const source =
    simulation.network_source === "live_nokia"
      ? "Live Nokia"
      : simulation.network_source === "recorded_fallback"
        ? "Fallback"
        : "Simulation";
  const qos = simulation.qos_active ? " · QoS active" : "";
  return `<span class="chip ${simulation.network_source}">${source} · ${Math.round(
    simulation.network_load * 100,
  )}% network load${qos}</span>`;
}

function render(): void {
  const team = teams.find((candidate) => candidate.id === teamId) ?? null;
  el("team-location").textContent = team ? titleCase(team.location) : "-";
  el("venue").textContent = simulation?.title ?? "";
  el("network").innerHTML = renderNetwork();

  const gateButton = el("mark-gate") as HTMLButtonElement;
  const arrivalButton = el("mark-arrival") as HTMLButtonElement;

  if (!team || !incident || !decision) {
    el("assignment").className = "assignment idle";
    el("headline").textContent = team?.status === "unreachable"
      ? "Handset unreachable"
      : "No active assignment";
    el("subline").textContent = team?.status === "unreachable"
      ? "The control centre cannot reach this device on the network."
      : "Standing by. You will be called automatically when an incident is assigned.";
    el("detail").innerHTML = "";
    gateButton.disabled = true;
    arrivalButton.disabled = true;
    return;
  }

  const onSite = decision.selected_gate === ON_SITE;
  const arrived = progress?.completed ?? false;
  el("assignment").className = `assignment ${arrived ? "done" : incident.priority}`;
  el("headline").textContent = arrived
    ? "Patient reached"
    : `${titleCase(incident.priority)} · ${titleCase(incident.location)}`;
  el("subline").textContent = arrived
    ? "This response is complete. You are available for the next call."
    : incident.description;

  const cost = decision.cost_breakdown;
  el("detail").innerHTML = `
    <div class="figures">
      <div><span>Estimated arrival</span><strong>${minutes(decision.estimated_arrival_seconds)}</strong></div>
      <div><span>Entry</span><strong>${onSite ? "Already inside" : titleCase(decision.selected_gate)}</strong></div>
      <div><span>Distance</span><strong>${decision.route_distance_m} m</strong></div>
    </div>
    <p class="why">${decision.explanation}</p>
    <div class="breakdown">
      <span>Walk ${cost.distance_seconds}s</span>
      <span>Crowd +${cost.crowd_penalty_seconds}s</span>
      <span>Network +${cost.network_penalty_seconds}s</span>
      <span>Access +${cost.access_penalty_seconds}s</span>
    </div>
    ${renderRoute()}
  `;

  const enteredGate =
    progress?.events.some((event) => event.event_type === "entered_selected_gate") ?? false;
  gateButton.disabled = arrived || onSite || enteredGate;
  gateButton.textContent = onSite
    ? "No gate on this route"
    : enteredGate
      ? `Entered ${titleCase(decision.selected_gate)}`
      : `I am at ${titleCase(decision.selected_gate)}`;
  arrivalButton.disabled = arrived;
  arrivalButton.textContent = arrived ? "Patient reached" : "I have reached the patient";
}

// ------------------------------------------------------------------ actions

async function report(eventType: "entered_selected_gate" | "reached_patient"): Promise<void> {
  if (!incident || !decision) return;
  const location =
    eventType === "entered_selected_gate" ? decision.selected_gate : incident.location;
  try {
    progress = await api<IncidentProgress>(`/incidents/${incident.id}/events/geofence`, {
      method: "POST",
      body: JSON.stringify({ team_id: decision.team_id, location, event_type: eventType }),
    });
    if (progress.completed) incident = { ...incident, status: "resolved" };
    render();
    await refreshAll();
  } catch (error) {
    setStatus((error as Error).message, "warn");
  }
}

// --------------------------------------------------------------------- live

function handleEvent(event: LiveEvent): void {
  if (event.type === "snapshot" || event.type === "simulation_state") {
    simulation = (event as { state: SimulationState }).state;
    render();
    return;
  }
  if (event.type === "dispatch" || event.type === "reroute") {
    const payload = (event as { response: { incident: Incident; decision: RouteDecision } }).response;
    // A dispatch to this responder is the call-out; anything else may still have
    // freed or taken a team, so the roster is refreshed either way.
    if (payload.decision.team_id === teamId) {
      incident = payload.incident;
      decision = payload.decision;
      setStatus(
        event.type === "reroute" ? "Route updated by the control centre" : "New assignment received",
        "ok",
      );
    }
    void refreshAll();
    return;
  }
  if (event.type === "geofence_progress") {
    const payload = event as unknown as { incident: Incident; progress: IncidentProgress };
    if (payload.progress.team_id === teamId) {
      incident = payload.incident;
      progress = payload.progress;
      render();
    }
    return;
  }
  if (event.type === "incidents_cancelled") {
    const ids = (event as { incident_ids: string[] }).incident_ids;
    if (incident && ids.includes(incident.id)) {
      setStatus("Assignment cancelled by the control centre", "warn");
      void refreshAll();
    }
  }
}

function connect(): void {
  window.clearTimeout(reconnectTimer);
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws/dashboard`);
  socket.onopen = () => setStatus("Connected to the control centre", "ok");
  socket.onmessage = (message) => {
    try {
      handleEvent(JSON.parse(message.data as string) as LiveEvent);
    } catch {
      /* a malformed frame must not tear down the responder view */
    }
  };
  socket.onclose = () => {
    setStatus("Reconnecting…", "warn");
    reconnectTimer = window.setTimeout(connect, 2000);
  };
  socket.onerror = () => socket?.close();
}

// --------------------------------------------------------------------- boot

(el("team") as HTMLSelectElement).addEventListener("change", (event) => {
  teamId = (event.target as HTMLSelectElement).value;
  completed = null;
  localStorage.setItem(TEAM_STORAGE_KEY, teamId);
  void loadAssignment();
});
el("mark-gate").addEventListener("click", () => void report("entered_selected_gate"));
el("mark-arrival").addEventListener("click", () => void report("reached_patient"));

connect();
void refreshAll();
// A responder walking through a venue loses signal; poll as a safety net.
window.setInterval(() => void refreshAll(), 15000);
