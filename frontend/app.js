const api = "";
let state = null, layout = null, incident = null, decision = null, routeHistory = [], agentRuntime = null;
let teams = [];
let liveSocket = null, reconnectTimer = null;
const $ = (id) => document.getElementById(id);
const { draw: drawVenueMap, densityColor } = window.RescueRouteMap;

async function request(path, options = {}) {
  const response = await fetch(`${api}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error((await response.json()).detail || "Request failed");
  return response.json();
}
function addLiveActivity(message) {
  const activity = $("activity");
  activity.innerHTML = `<li>${formatActivity(message)}</li>${activity.innerHTML}`;
}
async function handleLiveEvent(event) {
  if (event.type === "snapshot" || event.type === "simulation_state") {
    state = event.state;
    if (!layout || layout.template !== state.template) layout = await request("/simulation/layout");
    render();
    if (event.type === "simulation_state") addLiveActivity("Live simulation update received");
    refreshTeams();
    return;
  }
  if ((event.type === "dispatch" || event.type === "reroute") && incident?.id === event.response.incident.id) {
    incident = event.response.incident; decision = event.response.decision;
    await refreshHistory(); render();
    addLiveActivity(event.type === "reroute" ? "Live reroute received" : "Live dispatch received");
    refreshTeams();
    return;
  }
  if (event.type === "geofence_progress" && incident?.id === event.incident.id) {
    incident = event.incident; render();
    addLiveActivity(`Live geofence update: ${event.progress.last_location}`);
    refreshTeams();
    return;
  }
  if (event.type === "dispatch" && event.from_queue) {
    addLiveActivity("Queued incident dispatched: a response team became available");
    refreshTeams();
    return;
  }
  if (event.type === "incidents_cancelled" && incident && event.incident_ids.includes(incident.id)) {
    incident = decision = null; routeHistory = []; render();
    addLiveActivity("Active incident cancelled: its location is not part of the loaded venue");
    $("decision").textContent = "The venue changed, so the active incident was cancelled. Create a new emergency.";
  }
}
function connectLiveUpdates() {
  clearTimeout(reconnectTimer);
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  liveSocket = new WebSocket(`${scheme}://${location.host}/ws/dashboard`);
  liveSocket.onopen = () => { $("connection-status").textContent = "Live updates connected"; };
  liveSocket.onmessage = (message) => {
    handleLiveEvent(JSON.parse(message.data)).catch(error => { $("connection-status").textContent = error.message; });
  };
  liveSocket.onclose = () => {
    $("connection-status").textContent = "Reconnecting live updates…";
    reconnectTimer = setTimeout(connectLiveUpdates, 2000);
  };
  liveSocket.onerror = () => liveSocket.close();
}
function label(value) { return value.replaceAll("_", " "); }
function escapeHtml(value) { const element = document.createElement("span"); element.textContent = value; return element.innerHTML; }
function apiSource(message) {
  if (message.startsWith("AI Agent")) return "agent";
  if (message.includes("Nokia NaC") && !message.includes("unavailable") && !message.includes("fallback")) return "live";
  if (/fallback|unavailable|recorded/i.test(message)) return "fallback";
  return "simulation";
}
function sourceLabel(source) { return source === "live" ? "Live Nokia" : source === "fallback" ? "Fallback" : source === "agent" ? "AI Agent" : "Simulation"; }
function formatActivity(message) { const source = apiSource(message); return `<em class="api-badge ${source}">${sourceLabel(source)}</em> ${escapeHtml(message)}`; }
function renderApiStatus() {
  const calls = decision?.api_calls || [];
  if (!calls.length) { const agentLabel = agentRuntime?.configured ? `Gemini configured · ${agentRuntime.model}` : "AI fallback policy"; $("api-status").innerHTML = `<em class="api-badge simulation">Simulation ready</em><em class="api-badge agent">${agentLabel}</em>`; return; }
  const sources = ["agent", "live", "fallback", "simulation"].filter(source => calls.some(call => apiSource(call) === source));
  $("api-status").innerHTML = sources.map(source => `<em class="api-badge ${source}">${sourceLabel(source)}</em>`).join("");
}
async function refreshTeams() {
  try { teams = await request("/teams"); renderTeams(); } catch { teams = []; }
}
function renderTeams() {
  $("team-list").innerHTML = teams.length
    ? teams.map(team => `<div class="team ${team.status}"><b>${escapeHtml(team.name)}</b><span>${team.status}</span><i>${label(team.location)}</i></div>`).join("")
    : "Roster unavailable.";
}
function render() {
  if (!state || !layout) return;
  $("venue-title").textContent = layout.title;
  $("simulated-time").textContent = `T+${state.simulated_minutes} min`;
  $("incident-status").textContent = incident ? incident.status : "No active incident";
  $("selected-gate").textContent = decision ? (decision.selected_gate === "on_site" ? "on site (no gate crossing)" : label(decision.selected_gate)) : "-";
  $("eta").textContent = decision ? `${Math.ceil(decision.estimated_arrival_seconds / 60)} min` : "-";
  $("active-scenario").textContent = label(state.active_scenario || "custom");
  $("distance-cost").textContent = decision ? `${decision.cost_breakdown.distance_seconds} sec` : "-";
  $("crowd-cost").textContent = decision ? `+${decision.cost_breakdown.crowd_penalty_seconds} sec` : "-";
  $("network-cost").textContent = decision ? `+${decision.cost_breakdown.network_penalty_seconds} sec` : "-";
  $("access-cost").textContent = decision ? `+${decision.cost_breakdown.access_penalty_seconds} sec` : "-";
  renderApiStatus();
  renderTeams();
  const onSite = decision?.selected_gate === "on_site";
  $("mark-gate").disabled = !decision || onSite;
  $("mark-gate").title = onSite ? "The team started inside the venue; this response has no gate crossing." : "";
  $("gate-options").innerHTML = decision
    ? decision.gate_options.map(option => `<span class="gate-option ${option.gate === decision.selected_gate ? "selected" : ""}">${label(option.gate)}: ${option.available ? `${option.route_distance_m} m · ${option.eta_seconds} sec` : option.reason}</span>`).join("")
    : "Create an emergency to compare entries.";
  $("decision-history").innerHTML = routeHistory.length
    ? routeHistory.map(entry => `<li><b>${entry.event_type === "reroute" ? "Reroute" : "Dispatch"}</b> · ${new Date(entry.occurred_at).toLocaleTimeString()} · ${entry.trigger}<br>${entry.previous_route ? `${entry.previous_route.map(label).join(" → ")} → ` : ""}${entry.route.map(label).join(" → ")} (${entry.eta_seconds} sec)</li>`).join("")
    : "<li>No route decisions recorded.</li>";
  $("zone-list").innerHTML = Object.entries(state.zone_congestion).map(([zone, value]) => `<div class="zone"><span>${label(zone)}</span><b>${Math.round(value * 100)}%</b><div class="bar"><i style="width:${value * 100}%;background:${densityColor(value)}"></i></div></div>`).join("");
  const corridor = $("corridor"); const previous = corridor.value;
  corridor.innerHTML = layout.edges.map((edge) => `<option value="${edge.source}|${edge.destination}">${label(edge.source)} ↔ ${label(edge.destination)}</option>`).join("");
  corridor.value = previous || corridor.value;
  drawMap();
}
function drawMap() {
  drawVenueMap($("venue-map"), {
    layout, state,
    route: decision?.route || [],
    incidentLocation: incident?.location,
    selectedGate: decision?.selected_gate,
  });
}
function log(items) { $("activity").innerHTML = items.map(item => `<li>${formatActivity(item)}</li>`).join(""); }
async function syncIncidentState() {
  if (!incident) return;
  try { incident = await request(`/incidents/${incident.id}`); } catch { return; }
  if (incident.status !== "dispatched") { decision = null; routeHistory = []; }
  render(); refreshTeams();
}
async function refreshHistory() { if (incident) { routeHistory = (await request(`/incidents/${incident.id}/history`)).entries; render(); } }
async function loadScenario() {
  try {
    incident = decision = null; routeHistory = [];
    state = await request("/simulation/configure", { method:"POST", body:JSON.stringify({ template:$("template").value, crowd_pattern:$("crowd-pattern").value, seed:Number($("seed").value) }) });
    layout = await request("/simulation/layout"); render(); log(["Scenario configured", "Awaiting emergency event"]); $("decision").textContent = "Scenario ready. Create an emergency to let the agent select a route.";
  } catch (error) { $("decision").textContent = error.message; }
}
async function applyRecordedScenario() {
  try {
    incident = decision = null; routeHistory = [];
    state = await request("/simulation/scenarios", { method:"POST", body:JSON.stringify({ scenario:$("recorded-scenario").value }) });
    layout = await request("/simulation/layout"); render();
    log([`Recorded fallback scenario: ${label(state.active_scenario)}`, `Medic alpha: ${state.device_status.medic_alpha ? "reachable" : "unreachable"}`]);
    $("decision").textContent = "Scenario ready. Create an emergency to run the orchestration flow.";
  } catch (error) { $("decision").textContent = error.message; }
}
async function createIncident() {
  try {
    const location = layout.nodes.find(n => n.id === "main_stage")?.id
      || layout.nodes.find(n => n.id === "kaaba_tawaf")?.id
      || layout.nodes.find(n => n.id === "prayer_area")?.id
      || layout.nodes.at(-1).id;
    incident = await request("/incidents", { method:"POST", body:JSON.stringify({ location, priority:"critical", description:"Simulated medical emergency" }) });
    const result = await request(`/incidents/${incident.id}/dispatch`, { method:"POST" }); incident = result.incident; decision = result.decision; await refreshHistory(); render(); log(decision.api_calls); $("decision").textContent = decision.explanation;
  } catch (error) { $("decision").textContent = error.message; await syncIncidentState(); }
}
async function advanceTime() { try { state = await request("/simulation/advance", { method:"POST", body:JSON.stringify({ minutes:10 }) }); render(); if (incident) addLiveActivity("Crowd update submitted; affected active routes reroute automatically."); } catch (error) { $("decision").textContent = error.message; } }
async function toggleCorridor() { try { const [source,destination] = $("corridor").value.split("|"); state = await request("/simulation/events/corridor", { method:"POST", body:JSON.stringify({source,destination,closed:true}) }); render(); if (incident) addLiveActivity(`${label(source)} corridor closed; affected active routes reroute automatically.`); else $("decision").textContent = "Corridor closed. Create an emergency to see its routing impact."; } catch (error) { $("decision").textContent = error.message; } }
async function markGate() { if (!decision || !incident) return; try { const progress = await request(`/incidents/${incident.id}/events/geofence`, { method:"POST", body:JSON.stringify({team_id:decision.team_id,location:decision.selected_gate,event_type:"entered_selected_gate"}) }); log(["Geofencing: team entered selected gate", ...progress.events.map(e => `${e.event_type}: ${label(e.location)}`)]); } catch (error) { $("decision").textContent = error.message; } }
async function markArrival() { if (!decision || !incident) return; try { const progress = await request(`/incidents/${incident.id}/events/geofence`, { method:"POST", body:JSON.stringify({team_id:decision.team_id,location:incident.location,event_type:"reached_patient"}) }); incident.status = progress.completed ? "resolved" : incident.status; render(); log(["Geofencing: team reached patient", ...progress.events.map(e => `${e.event_type}: ${label(e.location)}`)]); } catch (error) { $("decision").textContent = error.message; } }
$("apply-scenario").addEventListener("click", applyRecordedScenario);
$("load-scenario").addEventListener("click", loadScenario); $("create-incident").addEventListener("click", createIncident); $("advance-time").addEventListener("click", advanceTime); $("toggle-corridor").addEventListener("click", toggleCorridor);
$("mark-gate").addEventListener("click", markGate); $("mark-arrival").addEventListener("click", markArrival);
async function loadAgentRuntime() { try { agentRuntime = await request("/agent/status"); renderApiStatus(); } catch { agentRuntime = null; } }
connectLiveUpdates();
loadAgentRuntime().finally(loadScenario).finally(refreshTeams);
