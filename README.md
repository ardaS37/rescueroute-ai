# RescueRoute AI

RescueRoute AI helps emergency teams reach incidents inside crowded stadiums, concerts, festivals, and Hajj sites. Instead of choosing the shortest route, it selects the most feasible route using venue access, crowd density, and mobile-network conditions.

**Live demo:** https://tech-mate.tech/dashboard/
**Free 2D arena:** https://tech-mate.tech/dashboard/arena/
**API documentation:** https://tech-mate.tech/docs

## Why it matters

At mass events, a route that is physically shorter can be slower or unsafe because of crowd pressure, a closed corridor, or unreliable emergency-team connectivity. RescueRoute AI turns those signals into a transparent routing decision, for example:

> Gate C selected: Gate B is 110 m shorter, but crowd and network delays make its estimated arrival 0 min 5 sec slower.

## What the prototype demonstrates

- 12–20 node venue graphs for stadium, festival, and Hajj layouts.
- Explainable ETA scoring: `ETA = distance + crowd penalty + network penalty + access penalty`.
- Deterministic fallback scenarios: normal operation, Gate A congestion, corridor closure, and primary-team unavailability.
- A simplified Masjid al-Haram Hajj/Umrah graph with King Abdulaziz, King Fahd, King Abdullah, and Al-Safa gates, Mataf, Kaaba/Tawaf, and Mas'a flow zones.
- Agentic orchestration: incident → reachable team → location → congestion → route scoring → QoS → geofence progress → reroute.
- A LangGraph + Gemini decision agent that creates a bounded CAMARA tool plan; the routing engine validates every action and retains a deterministic fallback.
- A shared live 2D crowd arena that sends aggregated density to the backend routing engine, including a Mataf/Kaaba/Mas'a view for the Hajj venue.
- WebSocket updates for dispatch, automatic reroute, simulation state, and geofence progress.
- SQLite-backed incident, route-history, geofence-progress, and simulation-state recovery across Docker restarts.

## Nokia Network as Code / CAMARA integration

The Nokia Network as Code simulator is used when `NAC_LIVE_ENABLED=true` and a valid RapidAPI key is present. The demo remains operational when a provider call fails or is rate-limited.

| CAMARA capability | Decision impact in RescueRoute AI |
| --- | --- |
| Device Status | Chooses `medic_alpha` when reachable; otherwise assigns backup `medic_bravo`. |
| Location Retrieval | Verifies the authorised response team and maps it to the known venue starting point. |
| Congestion Insights | Adds cellular-load cost to the network component of ETA. |
| QoS on Demand | Creates a critical-response QoS session and reduces network penalty. |
| Geofencing | Subscribes the selected team; an `area-entered` callback automatically records arrival at the selected gate and broadcasts it to both UIs. |

The dashboard labels every API activity as **Live Nokia**, **Fallback**, or **Simulation**. This makes the live integration and resilient behaviour visible to evaluators.

## AI agent layer

The emergency agent does not make arbitrary network calls. It observes the incident priority, venue conditions, network load, and reroute trigger; Gemini selects from a fixed CAMARA tool set, and the backend validates that plan before execution. The dashboard shows **AI Agent** observation, plan, and reasoning rows before the resulting CAMARA calls. If Gemini is unavailable, a recorded policy produces the same safe tool sequence so the demo continues.

## Demo script

Use [PITCH_DEMO.md](PITCH_DEMO.md) for the 2–3 minute live presentation sequence.

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

`GET /agent/status` reports whether the deployed instance has a Gemini key configured, without exposing the key. If it reports `deterministic_fallback`, add `GEMINI_API_KEY` and `GEMINI_MODEL=gemini-flash-lite-latest` to the VPS `.env`, then recreate the app container.

Nokia Geofencing and QoD callbacks require `NAC_WEBHOOK_TOKEN`. RescueRoute sends this as the Nokia sink credential and accepts callback requests only when they carry `Authorization: Bearer <token>`.

## API highlights

- `POST /incidents` — create an incident.
- `POST /incidents/{id}/dispatch` — execute the orchestration and receive an explainable route decision.
- `POST /incidents/{id}/recalculate-route` — reroute after a network, crowd, or corridor change.
- `GET /incidents/{id}/agent-trace` — inspect the agent's model source, tool plan, and concise reasoning.
- `GET /incidents/{id}/history` — inspect dispatch/reroute decisions.
- `POST /simulation/scenarios` — load a recorded fallback scenario.
- `POST /simulation/events/congestion` — update a zone from the 2D arena or a manual demo action.
- `POST /simulation/events/corridor` — close/open a corridor.
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
