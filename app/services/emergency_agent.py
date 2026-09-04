"""LangGraph-powered emergency agent for CAMARA tool orchestration.

The LLM only proposes an allowed CAMARA tool plan.  The deterministic routing
engine validates the plan and remains the final authority for emergency actions.
"""

from __future__ import annotations

import json
import os
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from app.models import AgentRuntimeStatus, AgentTrace, DispatchResponse, Priority
from app.services.incident_service import IncidentService


ALLOWED_TOOLS = (
    "device_status",
    "location_retrieval",
    "congestion_insights",
    "route_score",
    "qos_on_demand",
    "geofencing",
)
BASELINE_TOOLS = ("device_status", "location_retrieval", "congestion_insights", "route_score")


class AgentState(TypedDict, total=False):
    incident_id: str
    trigger: str
    plan: dict[str, object]
    response: DispatchResponse
    trace: AgentTrace


class EmergencyAgent:
    """A bounded agent: it selects trusted network tools, never arbitrary HTTP calls."""

    def __init__(self, incidents: IncidentService) -> None:
        self.incidents = incidents
        self._traces: dict[str, AgentTrace] = {}
        graph = StateGraph(AgentState)
        graph.add_node("plan_network_tools", self._plan_network_tools)
        graph.add_node("execute_validated_plan", self._execute_validated_plan)
        graph.add_edge(START, "plan_network_tools")
        graph.add_edge("plan_network_tools", "execute_validated_plan")
        graph.add_edge("execute_validated_plan", END)
        self.graph = graph.compile()

    def dispatch(self, incident_id: str, trigger: str = "Initial emergency dispatch") -> DispatchResponse:
        result = self.graph.invoke({"incident_id": incident_id, "trigger": trigger})
        return result["response"]

    def trace(self, incident_id: str) -> AgentTrace:
        return self._traces[incident_id]

    @staticmethod
    def runtime_status() -> AgentRuntimeStatus:
        configured = bool(os.getenv("GEMINI_API_KEY", "").strip())
        return AgentRuntimeStatus(
            provider="gemini" if configured else "deterministic_fallback",
            model=os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest") if configured else None,
            configured=configured,
        )

    def _plan_network_tools(self, state: AgentState) -> dict[str, object]:
        incident = self.incidents.get(state["incident_id"])
        simulation = self.incidents.camara.state()
        fallback = self._fallback_plan(incident.priority, simulation.network_load)
        source = "deterministic_fallback"
        plan = fallback
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if key:
            try:
                proposed = self._gemini_plan(key, incident.priority, incident.location, state["trigger"], simulation)
                plan = self._validated_plan(proposed, incident.priority, simulation.network_load)
                source = "gemini"
            except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                plan = fallback
        trace = AgentTrace(
            incident_id=incident.id,
            trigger=state["trigger"],
            model_source=source,
            tool_plan=plan["tools"],
            reasoning=plan["reasoning"],
        )
        return {"plan": plan, "trace": trace}

    def _execute_validated_plan(self, state: AgentState) -> dict[str, object]:
        trace = state["trace"]
        incident, decision = self.incidents.dispatch(
            state["incident_id"], trigger=state["trigger"], agent_tools=trace.tool_plan
        )
        agent_mode = "Gemini" if trace.model_source == "gemini" else "recorded fallback"
        decision.api_calls[0:0] = [
            f"AI Agent observation: {incident.priority.value.title()} incident at {incident.location.replace('_', ' ')} - trigger: {trace.trigger}",
            f"AI Agent plan ({agent_mode}): {' -> '.join(tool.replace('_', ' ').title() for tool in trace.tool_plan)}",
            f"AI Agent reasoning ({agent_mode}): {trace.reasoning}",
        ]
        response = DispatchResponse(incident=incident, decision=decision)
        self._traces[incident.id] = trace
        return {"response": response}

    @staticmethod
    def _fallback_plan(priority: Priority, network_load: float) -> dict[str, object]:
        tools = list(BASELINE_TOOLS)
        if priority in (Priority.CRITICAL, Priority.HIGH) and (priority == Priority.CRITICAL or network_load >= 0.45):
            tools.append("qos_on_demand")
        if priority in (Priority.CRITICAL, Priority.HIGH):
            tools.append("geofencing")
        return {
            "tools": tools,
            "reasoning": "Validated emergency policy selected team reachability, location, congestion and route scoring; QoS/geofencing were enabled only when priority justified them.",
        }

    def _gemini_plan(self, key: str, priority: Priority, location: str, trigger: str, simulation: object) -> dict[str, object]:
        prompt = {
            "task": "Choose CAMARA tools for one emergency response. Return JSON only.",
            "allowed_tools": list(ALLOWED_TOOLS),
            "required_tools": list(BASELINE_TOOLS),
            "incident": {"priority": priority.value, "location": location, "trigger": trigger},
            "network": {
                "load": getattr(simulation, "network_load"),
                "source": getattr(simulation, "network_source"),
                "closed_corridors": getattr(simulation, "closed_corridors"),
            },
            "rules": [
                "Never omit required_tools.",
                "Use qos_on_demand only for high/critical urgency or meaningful network load.",
                "Use geofencing for high/critical urgency.",
                "Return {tools: string[], reasoning: string} with a concise reasoning trace.",
            ],
        }
        model = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt)}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 160,
                "responseMimeType": "application/json",
            },
        }
        response = httpx.post(url, headers={"x-goog-api-key": key}, json=payload, timeout=12)
        response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

    @staticmethod
    def _validated_plan(proposed: dict[str, object], priority: Priority, network_load: float) -> dict[str, object]:
        raw_tools = proposed.get("tools", [])
        selected = [tool for tool in raw_tools if isinstance(tool, str) and tool in ALLOWED_TOOLS]
        tools = list(dict.fromkeys([*BASELINE_TOOLS, *selected]))
        if priority not in (Priority.CRITICAL, Priority.HIGH):
            tools = [tool for tool in tools if tool not in {"qos_on_demand", "geofencing"}]
        if priority == Priority.CRITICAL and "geofencing" not in tools:
            tools.append("geofencing")
        if priority == Priority.CRITICAL and network_load >= 0.45 and "qos_on_demand" not in tools:
            tools.append("qos_on_demand")
        reasoning = str(proposed.get("reasoning") or "Gemini selected a validated network-tool plan.")[:420]
        return {"tools": tools, "reasoning": reasoning}
