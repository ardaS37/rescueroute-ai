# RescueRoute AI — 2–3 Minute Pitch and Live Demo

## 0:00–0:20 — Problem

“At a crowded concert or stadium match, the shortest path is often not the fastest or safest one. A gate may be close but blocked by crowd pressure, a corridor may be closed, or the response team may have poor network connectivity. Every delayed minute matters in an emergency.”

## 0:20–0:40 — Solution

“RescueRoute AI is a network-aware emergency routing agent. It combines venue topology, live crowd conditions, and Nokia Network as Code signals to choose the most feasible route — not simply the shortest one.”

Open the **Control Centre** at `/dashboard/`.

## 0:40–1:15 — Agent orchestration and explainability

Select **Gate A congested**, click **Apply demo scenario**, then click **Run full emergency demo**.

Say:

“When an incident is created, the agent first checks whether the primary medic is reachable. It verifies the team’s location, gets congestion signals, scores every gate, requests QoS for a critical response, and subscribes to geofencing.”

Point to:

- **Agent Activity** — ordered API orchestration trace.
- **CAMARA data sources** — Live Nokia, Fallback, or Simulation status.
- **Gate comparison** and **ETA cost breakdown**.

Read the explanation aloud:

“Gate C is selected even though Gate B is shorter, because crowd and network delays make Gate B slower overall.”

## 1:15–1:50 — Dynamic rerouting and geofencing

The full demo closes an active corridor and reroutes automatically.

Say:

“This is not a one-time route. When access or crowd conditions change, RescueRoute AI recalculates the route and preserves a decision history. Nokia Geofencing callbacks are linked to the incident, so entering the selected gate automatically updates the team’s progress in real time.”

Point to **Decision history** and **Geofence progress**.

## 1:50–2:25 — 2D operational view

Open the **Free 2D arena** at `/dashboard/arena/`, click inside the crowd to place an incident, then click **Stage surge**.

Say:

“The 2D arena is an operational view, not just an animation. It aggregates crowd density and sends it to the same backend routing engine. The turquoise line is the active ambulance route; route nodes and the chosen gate are visible. When a route changes, the old route flashes red and the new route takes over.”

Point to **Network feed**:

“This card shows whether the current network component comes from the live Nokia simulator or deterministic fallback data. QoS state is visible here as well.”

### Hajj variation

For the pilgrimage-focused version, select **Hajj: Tawaf surge** before opening the arena. Say:

“The same agent now works around the Kaaba. Mataf congestion, Mas'a flow, named access gates, and network conditions are scored together. A route may reject a physically closer gate if its crowd or cellular conditions would slow a critical team.”

Point to the **Kaaba**, **Mataf** rings, **Mas'a corridor**, and the selected named gate in the shared 2D view.

## 2:25–2:45 — Close

“RescueRoute AI gives event operators an explainable, resilient way to dispatch emergency teams through crowded venues. It is designed for stadiums, festivals, pilgrimage sites, and other high-density events across the MENA region — and can integrate with operator-grade CAMARA APIs as deployments scale.”

## Demo safety notes

- If Nokia simulator APIs are rate-limited, the UI shows **Fallback** and the full scenario continues deterministically.
- Use the **Gate A congested** recorded scenario for a reliable gate-comparison explanation.
- Keep the Control Centre and 2D arena in separate tabs so WebSocket updates are visibly simultaneous.
