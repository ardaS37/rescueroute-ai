# RescueRoute AI

RescueRoute AI helps emergency teams reach incidents inside crowded stadiums, concerts, festivals, and Hajj sites. Instead of choosing the shortest route, it selects the most feasible route using venue access, crowd density, and mobile-network conditions.

**Live demo:** https://tech-mate.tech/dashboard/
**Free 2D arena:** https://tech-mate.tech/dashboard/arena/
**API documentation:** https://tech-mate.tech/docs

## Why it matters

At mass events, a route that is physically shorter can be slower or unsafe because of crowd pressure, a closed corridor, or unreliable emergency-team connectivity. RescueRoute AI turns those signals into a transparent routing decision, for example:

> Gate C selected: Gate B is 110 m shorter, but crowd and network delays make its estimated arrival 6 min 23 sec slower.

## What the prototype demonstrates

- 12–20 node venue graphs for stadium, festival, and Hajj layouts.
- Explainable ETA scoring: `ETA = distance + crowd penalty + network penalty + access penalty`, with every term independent and the four parts guaranteed to sum to the quoted ETA.
  - **Crowd** models flow collapse rather than linear slowdown: walking time scales with `density / (1 - density)`, so a corridor at 85% density costs about 6x its free-flow time instead of 1.8x. A shorter route through a crush is rejected.
  - **Network** combines the operator-reported cellular load (70%) with local device contention in the zone (30%). A quiet corridor in a loaded venue still pays a coordination cost.
  - **Access** is controlled-entry time declared per corridor in the venue graph (turnstiles, staffed gates), plus a runtime penalty for a corridor that is restricted rather than closed.
  - An active **QoD** session multiplies the network term by 0.4, and is opened before the route is scored so the relief reaches the decision that requested it.
- Team allocation across a three-team roster: each incident goes to the reachable, uncommitted team that can arrive soonest, and a team that resolves a call stays at the scene so it is closer to whatever happens next nearby.
- A priority queue for mass-casualty load: when every team is committed, the incident is queued instead of refused, and is dispatched automatically — most urgent first — as soon as a team frees up.
- Deterministic fallback scenarios: normal operation, Gate A congestion, corridor closure, and primary-team unavailability.
- A simplified Masjid al-Haram Hajj/Umrah graph with King Abdulaziz, King Fahd, King Abdullah, and Al-Safa gates, Mataf, Kaaba/Tawaf, and Mas'a flow zones.
- Agentic orchestration: incident → reachable team → location → congestion → route scoring → QoS → geofence progress → reroute.
- A LangGraph + Gemini decision agent that creates a bounded CAMARA tool plan; the routing engine validates every action and retains a deterministic fallback.
- A shared live 2D crowd arena that sends aggregated density to the backend routing engine, including a Mataf/Kaaba/Mas'a view for the Hajj venue.
- WebSocket updates for dispatch, automatic reroute, simulation state, and geofence progress.
- SQLite-backed incident, route-history, geofence-progress, and simulation-state recovery across Docker restarts.

## Team allocation

Three teams are staged per venue. Allocation runs on the same routing engine that scores gates:

1. Filter to teams that are reachable (CAMARA Device Status) and not already committed to another incident.
2. Score each candidate by the full ETA from where it actually is to the incident.
3. Assign the lowest ETA; the roster order breaks ties, so a fresh venue is deterministic.

A reroute changes the route, not the responder — the assigned team keeps the call unless its handset becomes unreachable, in which case the incident moves to another team and the progress record follows it.

A team that is already inside the venue reaches the next call without crossing a gate; that decision reports `selected_gate: on_site`, registers no geofence subscription, and the gate-entry step is refused with an explanation.

When every team is committed, the incident becomes `queued` and the dispatch endpoint answers `409` explaining why. Resolving or cancelling any incident releases its team at the location it finished, and the highest-priority queued incident is dispatched automatically and broadcast to every dashboard.

## Geographic anchoring

Every venue template is anchored on the real map, so the CAMARA APIs act on real coordinates rather than on drawing pixels:

| Template | Anchor | Scale |
| --- | --- | --- |
| `stadium_match` | Lusail Stadium, Qatar | 0.64 m/px |
| `music_festival` | Expo City Dubai | 0.53 m/px |
| `pilgrimage_flow` | Masjid al-Haram, Mecca | 0.98 m/px |

The canvas is a schematic, so each scale is calibrated to make the median corridor's straight-line length match the walking distance the routing graph declares for it. Gate geofence radii are chosen so no two gates overlap. The Hajj layout is a demonstration model only, not an official operational map.

## Interfaces

| Page | Audience | Purpose |
| --- | --- | --- |
| `/dashboard/` | Event control centre | Venue map, incidents, routing, gate comparison, live disruption controls, team roster. |
| `/dashboard/medic/` | Responding medical team | Phone-first responder view: current assignment, entry gate, step-by-step route with crowd levels, and arrival reporting. Written in TypeScript. |
| `/dashboard/arena/` | Demonstration | Free 2D crowd simulation that feeds aggregated density back to the routing engine. |
| `/dashboard/live/` | Demonstration | Animated regional flow through the emergency access graph. |

## Scalability and commercial viability

**Deployment shape.** Each signed-in visitor already drives an isolated workspace: its own venue state, incidents, team roster and dashboard sockets, persisted under its own key and released when it goes idle. The same boundary is what a production deployment uses per event, so scaling from one demo visitor to many concurrent venues is a matter of capacity, not of architecture. One process holds `RESCUEROUTE_MAX_WORKSPACES` of them; beyond that they shard by workspace key.

**Cost profile.** One decision costs at most five CAMARA calls, and the agent filters them: a crowd change in a zone no active route uses triggers nothing. Routing is Dijkstra over a 12–20 node graph, so the compute cost per venue is negligible and the operating cost is dominated by network-API calls, which scale with incidents rather than with attendees.

**Who buys it.** Stadium and arena operators, event organisers and their medical contractors, municipalities running mega-projects, and pilgrimage authorities. The natural commercial shape is a per-event or per-venue subscription with the operator's own MNO supplying the Open Gateway credentials, since the value depends on live network signals the operator already sells.

**What makes it defensible.** The venue graph, the calibrated cost model and the recorded fallback scenarios are the asset; the CAMARA APIs are a commodity input available to anyone. An operator that has modelled its venue once keeps that model across every event.

## Nokia Network as Code / CAMARA integration

The Nokia Network as Code simulator is used when `NAC_LIVE_ENABLED=true` and a valid RapidAPI key is present. The demo remains operational when a provider call fails or is rate-limited.

| CAMARA capability | Decision impact in RescueRoute AI |
| --- | --- |
| Device Status | Filters the roster to reachable teams before allocation, and moves a call to another team if the assigned handset drops off the network. |
| Location Retrieval | Resolves the reported fix against the venue's geographic anchor and routes from the nearest graph node. A fix outside the venue (a simulator handset) is reported as such and the recorded position is used. |
| Congestion Insights | Adds cellular-load cost to the network component of ETA. |
| QoS on Demand | Opens a critical-response QoS session before the route is scored; it multiplies the network term by 0.4 for that decision. |
| Geofencing | Subscribes a circle on the real coordinates of the gate the team was actually routed to, so an `area-entered` callback confirms the route it belongs to. Arrival is recorded and broadcast to every interface. |

The dashboard labels every API activity as **Live Nokia**, **Fallback**, or **Simulation**. This makes the live integration and resilient behaviour visible to evaluators.

## AI agent layer

The emergency agent does not make arbitrary network calls. It observes the incident priority, venue conditions, network load, and reroute trigger; Gemini selects from a fixed CAMARA tool set, and the backend validates that plan before execution. The dashboard shows **AI Agent** observation, plan, and reasoning rows before the resulting CAMARA calls. If Gemini is unavailable, a recorded policy produces the same safe tool sequence so the demo continues.

## Demo script

Recommended screen flow:

1. Open the Control Centre, choose **Gate A congested**, and click **Apply demo scenario**.
2. Run **Full emergency demo** to show the agent trace, gate comparison, QoS, and decision explanation.
3. Open the free 2D arena, click an incident location, then choose **Stage surge** or **Evacuate to gates**.
4. Point out the turquoise active route, selected-gate ring, and red previous route when a reroute occurs.
5. Show the **Network feed** card: live Nokia data when available, otherwise the recorded fallback label.

For the Hajj flow, choose **Hajj: Tawaf surge**, then open the arena. The shared Hajj view renders the Kaaba, Mataf circulation rings, Mas'a corridor, and named Masjid al-Haram gates while the same routing engine evaluates crowd and network penalties. It is a demonstration model only, not an official operational map or emergency-routing instruction.

## Run locally

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/dashboard/` or `http://127.0.0.1:8000/docs`.

### Environment variables

```env
RAPIDAPI_KEY=your_key_here
RAPIDAPI_HOST=network-as-code.nokia.rapidapi.com
NAC_LIVE_ENABLED=false
NAC_APPLICATION_SERVER_IP=your_vps_public_ipv4
NAC_WEBHOOK_BASE_URL=https://tech-mate.tech
NAC_WEBHOOK_TOKEN=replace-with-a-random-secret
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-flash-lite-latest
```

Keep `NAC_LIVE_ENABLED=false` for an entirely deterministic demo. Enable it to use Nokia simulator signals; fallback data will be used automatically for unavailable or rate-limited calls.

### Access gate and visitor isolation

```env
RESCUEROUTE_ACCESS_CODE=            # empty = open demo; set it to require a code
RESCUEROUTE_SESSION_SECRET=         # keeps sessions valid across restarts
RESCUEROUTE_MAX_WORKSPACES=40
RESCUEROUTE_WORKSPACE_TTL_SECONDS=21600
```

With a code configured, every page redirects to `/access` until the visitor signs in, and the API answers `401` instead of redirecting. `/health` and the Nokia callbacks stay reachable, because the callbacks carry their own sink credential. Guessing the code is rate limited.

Signing in issues a signed, expiring session cookie, and that cookie is also the key to the visitor's **own simulation**. Tabs in one browser - control centre, medic view, arena, corridor flow - share state with each other and with nobody else, so two evaluators can drive the demo at the same time without changing each other's venue, scenario, incidents or team roster. Rotating the access code invalidates every existing session. Idle workspaces are released from memory but their audit records stay in the database, so returning with the same cookie restores the simulation.

### Protecting the write path

Read endpoints are always open so the dashboards work without credentials. Every state-changing endpoint is guarded by two independent controls:

```env
RESCUEROUTE_API_TOKEN=            # empty = open demo; set it to require Authorization: Bearer <token>
RESCUEROUTE_RATE_LIMIT_ENABLED=true
RESCUEROUTE_WRITE_PER_MINUTE=60   # scenario loads, incident creation, geofence reports
RESCUEROUTE_DISPATCH_PER_MINUTE=30 # anything that can fan out into Gemini/Nokia calls
RESCUEROUTE_MAX_INCIDENTS=500     # oldest closed incidents are pruned beyond this
```

Rate limits are per client address. The container runs uvicorn with `--forwarded-allow-ips=*` because it is only reachable through Caddy on the compose network, so the limiter sees the real caller rather than the proxy.

`NAC_SIMULATOR_ALLOW_UNSIGNED_CALLBACKS=true` accepts Nokia simulator callbacks without the sink credential. It disables authentication on a public webhook that changes incident state, so leave it off outside local simulator testing.

`GET /agent/status` reports whether the deployed instance has a Gemini key configured, without exposing the key. If it reports `deterministic_fallback`, add `GEMINI_API_KEY` and `GEMINI_MODEL=gemini-flash-lite-latest` to the VPS `.env`, then recreate the app container.

Nokia Geofencing and QoD callbacks require `NAC_WEBHOOK_TOKEN`. RescueRoute sends this as the Nokia sink credential and accepts callback requests only when they carry `Authorization: Bearer <token>`.

## API highlights

- `GET /teams` — response-team roster: location, status, and the incident each team holds.
- `POST /incidents` — create an incident.
- `POST /incidents/{id}/dispatch` — execute the orchestration and receive an explainable route decision.
- `POST /incidents/{id}/recalculate-route` — reroute after a network, crowd, or corridor change.
- `GET /incidents/{id}/agent-trace` — inspect the agent's model source, tool plan, and concise reasoning.
- `GET /incidents/{id}/history` — inspect dispatch/reroute decisions.
- `POST /simulation/scenarios` — load a recorded fallback scenario.
- `POST /simulation/events/congestion` — update a zone from the 2D arena or a manual demo action.
- `POST /simulation/events/corridor` — close, restrict, or reopen a corridor. `restricted: true` keeps it walkable but adds access delay.
- `POST /webhooks/nokia/geofence` — Nokia Geofencing callback receiver.
- `GET /simulation/state` — current crowd, network load/source, and QoS state.
- `GET /ws/dashboard` — WebSocket stream for both user interfaces.
- `GET /agent/status` — safe Gemini/fallback runtime-readiness status.

## Deployment

The project runs with Docker Compose and Caddy. Both the application and reverse proxy use `restart: unless-stopped`; Docker is enabled on boot, so the project starts automatically after a VPS restart. A named Docker volume keeps the SQLite operational audit data across app-container recreation.

```bash
docker compose up -d --build
docker compose ps
```

## Validation

```powershell
py -m unittest discover -s tests -v
```

The test suite covers routing, deterministic scenarios, fallback-team assignment, automatic-reroute filtering, persistent route history, WebSocket delivery, and Nokia Geofencing callback-to-incident progress. GitHub Actions runs this suite for every push and pull request.
