/* Guided emergency demo.
 *
 * The control centre used to hide this behind one "run everything" button that
 * chose the incident location itself and closed whichever corridor happened to
 * be first on the route.  A presenter needs the opposite: pick the scene, pick
 * the disruption, and stop between steps to explain what just happened.
 *
 * Every step reads the decision back from the API instead of waiting for the
 * dashboard WebSocket.  The old demo raced that broadcast and, when it lost,
 * reported a gate the reroute had already replaced. */

const $ = (id) => document.getElementById(id);
const { draw, label } = window.RescueRouteMap;

let layout = null, state = null, incident = null, decision = null;
let history = [], teams = [], activity = [];
let stepIndex = 0, closedCorridor = null, busy = false;

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error((await response.json()).detail || "Request failed");
  return response.json();
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}
function apiSource(message) {
  if (message.startsWith("AI Agent")) return "agent";
  if (message.includes("Nokia NaC") && !message.includes("unavailable") && !message.includes("fallback")) return "live";
  if (/fallback|unavailable|recorded|budget/i.test(message)) return "fallback";
  return "simulation";
}
const sourceLabel = (source) => source === "live" ? "Live Nokia"
  : source === "fallback" ? "Fallback" : source === "agent" ? "AI Agent" : "Simulation";

function log(...messages) {
  activity.push(...messages);
  $("activity").innerHTML = activity.map((message) => {
    const source = apiSource(message);
    return `<li><em class="api-badge ${source}">${sourceLabel(source)}</em> ${escapeHtml(message)}</li>`;
  }).join("");
}

/* ----------------------------------------------------------------- steps */

const STEPS = [
  { id: "incident", title: "Create the emergency", run: createIncident },
  { id: "dispatch", title: "Agent selects team, gate and route", run: dispatch },
  { id: "disruption", title: "Close the chosen corridor and reroute", run: closeCorridor },
  { id: "gate", title: "Geofencing: team enters the selected gate", run: enterGate },
  { id: "arrival", title: "Geofencing: team reaches the patient", run: reachPatient },
  { id: "cleanup", title: "Reopen the corridor", run: reopenCorridor },
];
const status = {};

function renderSteps() {
  $("steps").innerHTML = STEPS.map((step, index) => {
    const stepStatus = status[step.id] || (index === stepIndex ? "next" : "pending");
    const note = status[`${step.id}:note`];
    return `<li class="step ${stepStatus}"><b>${escapeHtml(step.title)}</b>`
      + (note ? `<i>${escapeHtml(note)}</i>` : "") + `</li>`;
  }).join("");
}
function mark(step, state_, note) {
  status[step.id] = state_;
  if (note !== undefined) status[`${step.id}:note`] = note;
  renderSteps();
}

async function createIncident(step) {
  const location = $("incident-location").value;
  incident = await request("/incidents", {
    method: "POST",
    body: JSON.stringify({
      location, priority: $("priority").value, description: "Guided demo emergency",
    }),
  });
  log(`Incident created: ${$("priority").value} emergency at ${label(location)}`);
  mark(step, "done", `at ${label(location)}`);
  $("decision").textContent = "Incident received. Dispatch to see which team and entry the agent selects.";
  render();
}

async function dispatch(step) {
  const response = await request(`/incidents/${incident.id}/dispatch`, { method: "POST" });
  incident = response.incident;
  decision = response.decision;
  log(...decision.api_calls);
  await refreshAfterDecision();
  mark(step, "done", `${decision.team_id} via ${label(decision.selected_gate)}`);
  $("decision").textContent = decision.explanation;
}

async function closeCorridor(step) {
  const chosen = $("close-corridor").value;
  if (!chosen) {
    mark(step, "skipped", "no corridor selected");
    return;
  }
  const [source, destination] = chosen.split("|");
  const before = decision.route.join(" → ");
  state = await request("/simulation/events/corridor", {
    method: "POST",
    body: JSON.stringify({ source, destination, closed: true }),
  });
  closedCorridor = { source, destination };
  log(`Live disruption: ${label(source)} ↔ ${label(destination)} closed`);
  // Read the decision back rather than waiting on the dashboard broadcast:
  // the reroute has already run inside the request above.
  decision = await request(`/incidents/${incident.id}/decision`);
  await refreshAfterDecision();
  const changed = decision.route.join(" → ") !== before;
  log(changed
    ? `Automatic reroute: now entering via ${label(decision.selected_gate)}`
    : "Corridor closed, but it was not on the active route — no reroute needed");
  mark(step, "done", changed ? `rerouted via ${label(decision.selected_gate)}` : "route unaffected");
  $("decision").textContent = decision.explanation;
}

async function enterGate(step) {
  if (decision.selected_gate === "on_site") {
    log(`Geofencing: ${decision.team_id} was already inside the venue; no gate crossing to record`);
    mark(step, "skipped", "team started inside the venue");
    return;
  }
  await request(`/incidents/${incident.id}/events/geofence`, {
    method: "POST",
    body: JSON.stringify({
      team_id: decision.team_id, location: decision.selected_gate,
      event_type: "entered_selected_gate",
    }),
  });
  log(`Geofencing: ${decision.team_id} entered ${label(decision.selected_gate)}`);
  mark(step, "done", label(decision.selected_gate));
  render();
}

async function reachPatient(step) {
  const progress = await request(`/incidents/${incident.id}/events/geofence`, {
    method: "POST",
    body: JSON.stringify({
      team_id: decision.team_id, location: incident.location, event_type: "reached_patient",
    }),
  });
  incident = await request(`/incidents/${incident.id}`);
  log("Geofencing: team reached patient", `Incident ${incident.status}`);
  mark(step, "done", progress.completed ? "resolved" : incident.status);
  await refreshTeams();
  render();
  $("decision").textContent = "Emergency response completed. The selected team was tracked from dispatch through arrival.";
}

async function reopenCorridor(step) {
  if (!closedCorridor) {
    mark(step, "skipped", "nothing was closed");
    return;
  }
  if (!$("restore-corridor").checked) {
    mark(step, "skipped", "left closed on purpose");
    return;
  }
  state = await request("/simulation/events/corridor", {
    method: "POST",
    body: JSON.stringify({ ...closedCorridor, closed: false }),
  });
  log(`Cleanup: ${label(closedCorridor.source)} ↔ ${label(closedCorridor.destination)} reopened`);
  mark(step, "done", "reopened");
  closedCorridor = null;
  render();
}

/* ------------------------------------------------------------- rendering */

async function refreshAfterDecision() {
  history = (await request(`/incidents/${incident.id}/history`)).entries;
  state = await request("/simulation/state");
  await refreshTeams();
  render();
}

async function refreshTeams() {
  try { teams = await request("/teams"); } catch { teams = []; }
}

// "Stage cluster" is festival vocabulary, and it was still on screen while the
// map rendered Masjid al-Haram. The option values the API receives are
// unchanged; only the words follow the venue.
const CROWD_PATTERN_LABELS = {
  stadium_match: { gate_surge: "Gate surge", stage_cluster: "Pitch-side cluster", balanced: "Balanced" },
  music_festival: { gate_surge: "Gate surge", stage_cluster: "Stage cluster", balanced: "Balanced" },
  pilgrimage_flow: { gate_surge: "Arrival surge", stage_cluster: "Peak Tawaf", balanced: "Balanced" },
};
function relabelCrowdPatterns(template) {
  const labels = CROWD_PATTERN_LABELS[template] || CROWD_PATTERN_LABELS.stadium_match;
  for (const option of $("crowd-pattern").options) {
    if (labels[option.value]) option.textContent = labels[option.value];
  }
}
function render() {
  if (!layout || !state) return;
  $("venue-title").textContent = layout.title;
  relabelCrowdPatterns(state.template);
  $("simulated-time").textContent = `T+${state.simulated_minutes} min`;
  $("incident-status").textContent = incident ? `${incident.status} · ${label(incident.location)}` : "Not created";
  $("team-status").textContent = decision ? decision.team_id : "-";
  $("selected-gate").textContent = decision
    ? (decision.selected_gate === "on_site" ? "on site (no gate crossing)" : label(decision.selected_gate)) : "-";
  $("eta").textContent = decision ? `${Math.ceil(decision.estimated_arrival_seconds / 60)} min` : "-";
  $("distance-cost").textContent = decision ? `${decision.cost_breakdown.distance_seconds} sec` : "-";
  $("crowd-cost").textContent = decision ? `+${decision.cost_breakdown.crowd_penalty_seconds} sec` : "-";
  $("network-cost").textContent = decision ? `+${decision.cost_breakdown.network_penalty_seconds} sec` : "-";
  $("access-cost").textContent = decision ? `+${decision.cost_breakdown.access_penalty_seconds} sec` : "-";

  const calls = decision?.api_calls || [];
  const sources = ["agent", "live", "fallback", "simulation"]
    .filter((source) => calls.some((call) => apiSource(call) === source));
  $("api-status").innerHTML = (sources.length ? sources : ["simulation"])
    .map((source) => `<em class="api-badge ${source}">${sourceLabel(source)}</em>`).join("");

  $("gate-options").innerHTML = decision
    ? decision.gate_options.map((option) =>
        `<span class="gate-option ${option.gate === decision.selected_gate ? "selected" : ""}">`
        + `${label(option.gate)}: ${option.available ? `${option.route_distance_m} m · ${option.eta_seconds} sec` : escapeHtml(option.reason)}</span>`
      ).join("")
    : "Run the dispatch step to compare entries.";

  $("decision-history").innerHTML = history.length
    ? history.map((entry) =>
        `<li><b>${entry.event_type === "reroute" ? "Reroute" : "Dispatch"}</b> · `
        + `${new Date(entry.occurred_at).toLocaleTimeString()} · ${escapeHtml(entry.trigger)}<br>`
        + `${entry.route.map(label).join(" → ")} (${entry.eta_seconds} sec)</li>`
      ).join("")
    : "<li>No route decisions recorded.</li>";

  $("zone-list").innerHTML = Object.entries(state.zone_congestion).map(([zone, value]) =>
    `<div class="zone"><span>${label(zone)}</span><b>${Math.round(value * 100)}%</b>`
    + `<div class="bar"><i style="width:${value * 100}%;background:${window.RescueRouteMap.densityColor(value)}"></i></div></div>`
  ).join("");

  $("team-list").innerHTML = teams.length
    ? teams.map((team) =>
        `<div class="team ${team.status}"><b>${escapeHtml(team.name)}</b><span>${team.status}</span>`
        + `<i>${label(team.location)}</i></div>`).join("")
    : "Roster unavailable.";

  draw($("venue-map"), {
    layout, state,
    route: decision?.route || [],
    incidentLocation: incident?.location,
    selectedGate: decision?.selected_gate,
  });
}

/* ---------------------------------------------------------------- wiring */

function setBusy(value) {
  busy = value;
  const finished = stepIndex >= STEPS.length;
  $("next-step").disabled = value || finished || !layout;
  $("run-rest").disabled = value || finished || !layout;
  $("load-venue").disabled = value;
}

async function runStep() {
  const step = STEPS[stepIndex];
  if (!step) return false;
  mark(step, "running");
  try {
    await step.run(step);
  } catch (error) {
    mark(step, "failed", error.message);
    log(`Step failed: ${error.message}`);
    $("decision").textContent = error.message;
    return false;
  }
  stepIndex += 1;
  renderSteps();
  return true;
}

async function nextStep() {
  if (busy) return;
  setBusy(true);
  await runStep();
  setBusy(false);
  if (stepIndex >= STEPS.length) $("connection-status").textContent = "Demo complete";
}

async function runRest() {
  if (busy) return;
  setBusy(true);
  while (stepIndex < STEPS.length) {
    if (!await runStep()) break;
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  setBusy(false);
  if (stepIndex >= STEPS.length) $("connection-status").textContent = "Demo complete";
}

function populateChoices() {
  const locations = [...layout.nodes]
    .map((node) => node.id)
    .sort((a, b) => a.localeCompare(b));
  const preferred = ["main_stage", "kaaba_tawaf", "first_aid"].find((id) => locations.includes(id));
  $("incident-location").innerHTML = locations
    .map((id) => `<option value="${id}" ${id === preferred ? "selected" : ""}>${label(id)}</option>`).join("");
  $("close-corridor").innerHTML = `<option value="">No closure</option>`
    + layout.edges.map((edge) =>
        `<option value="${edge.source}|${edge.destination}">${label(edge.source)} ↔ ${label(edge.destination)}</option>`
      ).join("");
  $("incident-location").disabled = false;
  $("close-corridor").disabled = false;
}

async function loadVenue() {
  setBusy(true);
  try {
    resetRun();
    const scenario = $("recorded-scenario").value;
    state = scenario
      ? await request("/simulation/scenarios", { method: "POST", body: JSON.stringify({ scenario }) })
      : await request("/simulation/configure", {
          method: "POST",
          body: JSON.stringify({
            template: $("template").value,
            crowd_pattern: $("crowd-pattern").value,
            seed: Number($("seed").value),
          }),
        });
    layout = await request("/simulation/layout");
    $("template").value = state.template;
    $("crowd-pattern").value = state.crowd_pattern;
    $("seed").value = state.seed;
    populateChoices();
    await refreshTeams();
    render();
    log(`Venue loaded: ${state.title} · ${label(state.crowd_pattern)} · seed ${state.seed}`);
    $("connection-status").textContent = "Venue ready — choose the scene, then step through";
    $("decision").textContent = "Pick where the emergency happens and which corridor to close, then step through the response.";
  } catch (error) {
    $("decision").textContent = error.message;
  } finally {
    setBusy(false);
  }
}

function resetRun() {
  incident = decision = null;
  history = []; activity = []; closedCorridor = null; stepIndex = 0;
  for (const key of Object.keys(status)) delete status[key];
  $("activity").innerHTML = "<li>Awaiting the first step</li>";
  renderSteps();
  render();
}

async function resetDemo() {
  // Hand every medic back before clearing local state. A run stopped halfway
  // used to hold its team until the process restarted, so a few abandoned
  // rehearsals left the roster empty and the next dispatch could only queue.
  // This closes runs this page never saw too, such as one lost to a reload.
  let released;
  try {
    released = (await request("/incidents/cancel-open", { method: "POST" })).cancelled.length;
  } catch { released = null; }
  if (closedCorridor && $("restore-corridor").checked) {
    try {
      state = await request("/simulation/events/corridor", {
        method: "POST", body: JSON.stringify({ ...closedCorridor, closed: false }),
      });
    } catch { /* the venue reload below restores it anyway */ }
  }
  await refreshTeams();
  resetRun();
  // After resetRun, which clears the activity list this would otherwise land in.
  log(released === null ? "Reset: the roster could not be cleared — check the team list"
    : released ? `Reset: ${released} open incident(s) cancelled, every team back on the roster`
    : "Reset: no incident was open, the roster was already clear");
  setBusy(false);
  $("connection-status").textContent = "Choose the scenario, then walk through it";
}

// A recorded scenario fixes its own venue, pattern and seed, so the manual
// venue fields would otherwise claim settings that are not in effect.
function syncScenarioMode() {
  const scenario = Boolean($("recorded-scenario").value);
  for (const id of ["template", "crowd-pattern", "seed"]) $(id).disabled = scenario;
}

$("load-venue").addEventListener("click", loadVenue);
$("next-step").addEventListener("click", nextStep);
$("run-rest").addEventListener("click", runRest);
$("reset-demo").addEventListener("click", resetDemo);
$("recorded-scenario").addEventListener("change", syncScenarioMode);
syncScenarioMode();
renderSteps();
loadVenue();
