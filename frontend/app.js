const api = "";
let state = null, layout = null, incident = null, decision = null, routeHistory = [], demoRun = 0;
let liveSocket = null, reconnectTimer = null;
const $ = (id) => document.getElementById(id);

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
    return;
  }
  if ((event.type === "dispatch" || event.type === "reroute") && incident?.id === event.response.incident.id) {
    incident = event.response.incident; decision = event.response.decision;
    await refreshHistory(); render();
    addLiveActivity(event.type === "reroute" ? "Live reroute received" : "Live dispatch received");
    return;
  }
  if (event.type === "geofence_progress" && incident?.id === event.incident.id) {
    incident = event.incident; render();
    addLiveActivity(`Live geofence update: ${event.progress.last_location}`);
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
  if (!calls.length) { $("api-status").innerHTML = '<em class="api-badge simulation">Simulation ready</em>'; return; }
  const sources = ["agent", "live", "fallback", "simulation"].filter(source => calls.some(call => apiSource(call) === source));
  $("api-status").innerHTML = sources.map(source => `<em class="api-badge ${source}">${sourceLabel(source)}</em>`).join("");
}
function densityColor(value) { const hue = Math.round(140 - value * 140); return `hsl(${hue} 75% 56%)`; }
function nodeMap() { return new Map(layout.nodes.map((node) => [node.id, node])); }
function edgeKey(a, b) { return [a, b].sort().join(" <-> "); }

function render() {
  if (!state || !layout) return;
  $("venue-title").textContent = layout.title;
  $("simulated-time").textContent = `T+${state.simulated_minutes} min`;
  $("incident-status").textContent = incident ? incident.status : "No active incident";
  $("selected-gate").textContent = decision ? label(decision.selected_gate) : "-";
  $("eta").textContent = decision ? `${Math.ceil(decision.estimated_arrival_seconds / 60)} min` : "-";
  $("active-scenario").textContent = label(state.active_scenario || "custom");
  $("distance-cost").textContent = decision ? `${decision.cost_breakdown.distance_seconds} sec` : "-";
  $("crowd-cost").textContent = decision ? `+${decision.cost_breakdown.crowd_penalty_seconds} sec` : "-";
  $("network-cost").textContent = decision ? `+${decision.cost_breakdown.network_penalty_seconds} sec` : "-";
  renderApiStatus();
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
  const svg = $("venue-map"), nodes = nodeMap(), routePairs = new Set();
  if (decision) for (let i = 0; i < decision.route.length - 1; i++) routePairs.add(edgeKey(decision.route[i], decision.route[i + 1]));
  const routeLines = layout.edges.map((edge) => {
    const a = nodes.get(edge.source), b = nodes.get(edge.destination), key = edgeKey(edge.source, edge.destination);
    const closed = state.closed_corridors.includes(key); return `<line class="edge ${closed ? "closed" : ""}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>${routePairs.has(key) ? `<line class="route" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>` : ""}`;
  }).join("");
  const halos = layout.nodes.filter(n => n.id !== "ambulance_bay").map((node) => { const relevant = layout.edges.find(e => e.source === node.id || e.destination === node.id); const d = relevant?.zone ? state.zone_congestion[relevant.zone] || .2 : .1; return `<circle class="crowd-halo" cx="${node.x}" cy="${node.y}" r="${30 + d * 46}" fill="${densityColor(d)}"/>`; }).join("");
  const marks = layout.nodes.map((node) => `<g><circle class="node ${node.kind} ${incident?.location === node.id ? "incident" : ""}" cx="${node.x}" cy="${node.y}" r="${node.kind === "gate" ? 13 : 10}"/><text class="node-label" x="${node.x}" y="${node.y - 20}">${label(node.id)}</text></g>`).join("");
  svg.innerHTML = `<rect width="840" height="510" fill="#f7faf7"/>${halos}${routeLines}${marks}`;
}
function log(items) { $("activity").innerHTML = items.map(item => `<li>${formatActivity(item)}</li>`).join(""); }
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
const pause = (milliseconds) => new Promise(resolve => setTimeout(resolve, milliseconds));
function demoIncidentLocation() {
  const scenarioTargets = {
    hajj_tawaf_surge: "kaaba_tawaf",
    hajj_masaa_congestion: "masaa_corridor",
    corridor_closed: "first_aid",
  };
  const target = scenarioTargets[state.active_scenario];
  if (target && layout.nodes.some(node => node.id === target)) return target;
  const venueTargets = {
    stadium_match: ["main_stage", "first_aid", "central_plaza"],
    music_festival: ["main_stage", "food_court", "first_aid"],
    pilgrimage_flow: ["kaaba_tawaf", "masaa_corridor", "medical_post"],
  };
  const targets = venueTargets[layout.template] || [];
  const available = targets.filter(targetId => layout.nodes.some(node => node.id === targetId));
  const location = available[demoRun % available.length] || layout.nodes.at(-1).id;
  demoRun += 1;
  return location;
}
async function runFullDemo() {
  const button = $("run-full-demo"), activity = [];
  let temporaryClosure = null;
  button.disabled = true; button.textContent = "Running emergency demo…";
  try {
    incident = decision = null; routeHistory = [];
    if (!state || !layout) { state = await request("/simulation/state"); layout = await request("/simulation/layout"); }
    const location = demoIncidentLocation();
    activity.push(`Auto demo uses current parameters: ${state.title} · ${label(state.crowd_pattern)} · seed ${state.seed}`, `Incident received: critical medical emergency at ${label(location)}`); log(activity);
    $("decision").textContent = "Finding a reachable team and evaluating entry routes…";
    await pause(450);

    incident = await request("/incidents", { method:"POST", body:JSON.stringify({ location, priority:"critical", description:"Full emergency demo" }) });
    const dispatch = await request(`/incidents/${incident.id}/dispatch`, { method:"POST" });
    incident = dispatch.incident; decision = dispatch.decision; await refreshHistory(); render();
    activity.push(...decision.api_calls, `Route selected: ${label(decision.selected_gate)}`); log(activity);
    $("decision").textContent = decision.explanation;
    await pause(650);

    const disruption = decision.segments.find(segment => segment.zone) || decision.segments[0];
    const disruptionKey = disruption ? [disruption.source, disruption.destination].sort().join(" <-> ") : "";
    if (disruption && !state.closed_corridors.includes(disruptionKey)) {
      state = await request("/simulation/events/corridor", { method:"POST", body:JSON.stringify({ source:disruption.source, destination:disruption.destination, closed:true }) });
      temporaryClosure = disruption;
      render(); activity.push(`Live disruption: ${label(disruption.source)} ↔ ${label(disruption.destination)} closed`); log(activity);
      const reroute = await request(`/incidents/${incident.id}/recalculate-route`, { method:"POST" });
      incident = reroute.incident; decision = reroute.decision; await refreshHistory(); render();
      activity.push(`Route recalculated: ${label(decision.selected_gate)}`, ...decision.api_calls); log(activity);
      $("decision").textContent = decision.explanation;
      await pause(650);
    } else if (disruption) {
      activity.push(`Auto reroute skipped: ${label(disruption.source)} ↔ ${label(disruption.destination)} was already closed`); log(activity);
    }

    const gateProgress = await request(`/incidents/${incident.id}/events/geofence`, { method:"POST", body:JSON.stringify({ team_id:decision.team_id, location:decision.selected_gate, event_type:"entered_selected_gate" }) });
    activity.push(`Geofencing: ${decision.team_id} entered ${label(decision.selected_gate)}`); log(activity);
    await pause(450);
    const arrival = await request(`/incidents/${incident.id}/events/geofence`, { method:"POST", body:JSON.stringify({ team_id:decision.team_id, location:incident.location, event_type:"reached_patient" }) });
    incident.status = arrival.completed ? "resolved" : incident.status; render();
    activity.push("Geofencing: team reached patient", "Emergency demo completed"); log(activity);
    $("decision").textContent = "Emergency response completed. The selected team was tracked from dispatch through arrival.";
  } catch (error) { $("decision").textContent = error.message; activity.push(`Demo failed: ${error.message}`); log(activity); }
  finally {
    if (temporaryClosure) {
      try {
        state = await request("/simulation/events/corridor", { method:"POST", body:JSON.stringify({ source:temporaryClosure.source, destination:temporaryClosure.destination, closed:false }) });
        render(); addLiveActivity(`Auto demo cleanup: ${label(temporaryClosure.source)} ↔ ${label(temporaryClosure.destination)} reopened; your parameters were preserved`);
      } catch { addLiveActivity("Auto demo cleanup could not reopen the temporary corridor"); }
    }
    button.disabled = false; button.textContent = "Run full emergency demo";
  }
}
async function createIncident() {
  try {
    const location = layout.nodes.find(n => n.id === "main_stage")?.id
      || layout.nodes.find(n => n.id === "kaaba_tawaf")?.id
      || layout.nodes.find(n => n.id === "prayer_area")?.id
      || layout.nodes.at(-1).id;
    incident = await request("/incidents", { method:"POST", body:JSON.stringify({ location, priority:"critical", description:"Simulated medical emergency" }) });
    const result = await request(`/incidents/${incident.id}/dispatch`, { method:"POST" }); incident = result.incident; decision = result.decision; await refreshHistory(); render(); log(decision.api_calls); $("decision").textContent = decision.explanation;
  } catch (error) { $("decision").textContent = error.message; }
}
async function advanceTime() { try { state = await request("/simulation/advance", { method:"POST", body:JSON.stringify({ minutes:10 }) }); render(); if (incident) await reroute("Crowd conditions changed; route recalculated."); } catch (error) { $("decision").textContent = error.message; } }
async function reroute(message) { const result = await request(`/incidents/${incident.id}/recalculate-route`, { method:"POST" }); incident = result.incident; decision = result.decision; await refreshHistory(); render(); log([message, ...decision.api_calls]); $("decision").textContent = decision.explanation; }
async function toggleCorridor() { try { const [source,destination] = $("corridor").value.split("|"); state = await request("/simulation/events/corridor", { method:"POST", body:JSON.stringify({source,destination,closed:true}) }); render(); if (incident) await reroute(`${label(source)} corridor closed; agent rerouted the team.`); else $("decision").textContent = "Corridor closed. Create an emergency to see its routing impact."; } catch (error) { $("decision").textContent = error.message; } }
async function markGate() { if (!decision || !incident) return; try { const progress = await request(`/incidents/${incident.id}/events/geofence`, { method:"POST", body:JSON.stringify({team_id:decision.team_id,location:decision.selected_gate,event_type:"entered_selected_gate"}) }); log(["Geofencing: team entered selected gate", ...progress.events.map(e => `${e.event_type}: ${label(e.location)}`)]); } catch (error) { $("decision").textContent = error.message; } }
async function markArrival() { if (!decision || !incident) return; try { const progress = await request(`/incidents/${incident.id}/events/geofence`, { method:"POST", body:JSON.stringify({team_id:decision.team_id,location:incident.location,event_type:"reached_patient"}) }); incident.status = progress.completed ? "resolved" : incident.status; render(); log(["Geofencing: team reached patient", ...progress.events.map(e => `${e.event_type}: ${label(e.location)}`)]); } catch (error) { $("decision").textContent = error.message; } }
$("apply-scenario").addEventListener("click", applyRecordedScenario);
$("run-full-demo").addEventListener("click", runFullDemo);
$("load-scenario").addEventListener("click", loadScenario); $("create-incident").addEventListener("click", createIncident); $("advance-time").addEventListener("click", advanceTime); $("toggle-corridor").addEventListener("click", toggleCorridor);
$("mark-gate").addEventListener("click", markGate); $("mark-arrival").addEventListener("click", markArrival);
connectLiveUpdates();
loadScenario();
