"use strict";
/**
 * RescueRoute AI - medical-team interface.
 *
 * The control centre answers "which gate should we send them through".  This
 * view answers the responder's question instead: what am I assigned to, where
 * do I go, and how do I report that I got there.  It is built for a phone held
 * in one hand while walking.
 */
const ON_SITE = "on_site";
const TEAM_STORAGE_KEY = "rescueroute.medic.team";
// ------------------------------------------------------------------- state
let teams = [];
let teamId = null;
let incident = null;
let decision = null;
let progress = null;
let simulation = null;
/** Keeps the arrival confirmation on screen after the team has been released. */
let completed = null;
let socket = null;
let reconnectTimer = 0;
// ------------------------------------------------------------------ helpers
function el(id) {
    const found = document.getElementById(id);
    if (!found)
        throw new Error(`missing element #${id}`);
    return found;
}
function label(value) {
    return value.replace(/_/g, " ");
}
function titleCase(value) {
    return label(value).replace(/\b\w/g, (character) => character.toUpperCase());
}
function minutes(seconds) {
    if (seconds < 60)
        return `${seconds} sec`;
    return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} sec`;
}
async function api(path, options = {}) {
    const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    if (!response.ok) {
        let detail = `Request failed (${response.status})`;
        try {
            const body = (await response.json());
            if (typeof body.detail === "string")
                detail = body.detail;
        }
        catch {
            /* a proxy error page is not JSON; keep the status text */
        }
        throw new Error(detail);
    }
    return (await response.json());
}
function setStatus(message, tone = "idle") {
    const banner = el("connection");
    banner.textContent = message;
    banner.className = `connection ${tone}`;
}
// -------------------------------------------------------------------- load
async function loadTeams() {
    teams = await api("/teams");
    const picker = el("team");
    const previous = teamId ?? localStorage.getItem(TEAM_STORAGE_KEY);
    picker.innerHTML = teams
        .map((team) => `<option value="${team.id}">${team.name}</option>`)
        .join("");
    teamId = teams.some((team) => team.id === previous) ? previous : (teams[0]?.id ?? null);
    if (teamId)
        picker.value = teamId;
}
/** Pull everything this responder needs for their current assignment. */
async function loadAssignment() {
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
        incident = await api(`/incidents/${team.incident_id}`);
        try {
            decision = await api(`/incidents/${team.incident_id}/decision`);
            progress = await api(`/incidents/${team.incident_id}/progress`);
        }
        catch {
            /* dispatched but not yet scored; the header still shows the assignment */
        }
    }
    else if (completed && completed.progress.team_id === teamId) {
        ({ incident, decision, progress } = completed);
    }
    render();
}
async function refreshAll() {
    try {
        [simulation] = await Promise.all([api("/simulation/state"), loadTeams()]);
        await loadAssignment();
    }
    catch (error) {
        setStatus(error.message, "warn");
    }
}
// ------------------------------------------------------------------ render
function renderRoute() {
    if (!decision)
        return "";
    const reached = new Set(progress?.events.map((event) => event.location) ?? []);
    const steps = decision.route.map((node, index) => {
        const segment = decision.segments[index - 1];
        const isGate = node === decision.selected_gate;
        const isTarget = index === decision.route.length - 1;
        const done = reached.has(node) || node === progress?.last_location;
        const zone = segment?.zone ? `${label(segment.zone)}` : "";
        const congestion = segment?.zone && simulation
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
function renderNetwork() {
    if (!simulation)
        return "";
    const source = simulation.network_source === "live_nokia"
        ? "Live Nokia"
        : simulation.network_source === "recorded_fallback"
            ? "Fallback"
            : "Simulation";
    const qos = simulation.qos_active ? " · QoS active" : "";
    return `<span class="chip ${simulation.network_source}">${source} · ${Math.round(simulation.network_load * 100)}% network load${qos}</span>`;
}
function render() {
    const team = teams.find((candidate) => candidate.id === teamId) ?? null;
    el("team-location").textContent = team ? titleCase(team.location) : "-";
    el("venue").textContent = simulation?.title ?? "";
    el("network").innerHTML = renderNetwork();
    const gateButton = el("mark-gate");
    const arrivalButton = el("mark-arrival");
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
    const enteredGate = progress?.events.some((event) => event.event_type === "entered_selected_gate") ?? false;
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
async function report(eventType) {
    if (!incident || !decision)
        return;
    const location = eventType === "entered_selected_gate" ? decision.selected_gate : incident.location;
    try {
        progress = await api(`/incidents/${incident.id}/events/geofence`, {
            method: "POST",
            body: JSON.stringify({ team_id: decision.team_id, location, event_type: eventType }),
        });
        if (progress.completed)
            incident = { ...incident, status: "resolved" };
        render();
        await refreshAll();
    }
    catch (error) {
        setStatus(error.message, "warn");
    }
}
// --------------------------------------------------------------------- live
function handleEvent(event) {
    if (event.type === "snapshot" || event.type === "simulation_state") {
        simulation = event.state;
        render();
        return;
    }
    if (event.type === "dispatch" || event.type === "reroute") {
        const payload = event.response;
        // A dispatch to this responder is the call-out; anything else may still have
        // freed or taken a team, so the roster is refreshed either way.
        if (payload.decision.team_id === teamId) {
            incident = payload.incident;
            decision = payload.decision;
            setStatus(event.type === "reroute" ? "Route updated by the control centre" : "New assignment received", "ok");
        }
        void refreshAll();
        return;
    }
    if (event.type === "geofence_progress") {
        const payload = event;
        if (payload.progress.team_id === teamId) {
            incident = payload.incident;
            progress = payload.progress;
            render();
        }
        return;
    }
    if (event.type === "incidents_cancelled") {
        const ids = event.incident_ids;
        if (incident && ids.includes(incident.id)) {
            setStatus("Assignment cancelled by the control centre", "warn");
            void refreshAll();
        }
    }
}
function connect() {
    window.clearTimeout(reconnectTimer);
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws/dashboard`);
    socket.onopen = () => setStatus("Connected to the control centre", "ok");
    socket.onmessage = (message) => {
        try {
            handleEvent(JSON.parse(message.data));
        }
        catch {
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
el("team").addEventListener("change", (event) => {
    teamId = event.target.value;
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
