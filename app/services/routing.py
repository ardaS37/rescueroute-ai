"""Dynamic shortest-path calculation for emergency response routes."""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from app.services.camara_simulator import CamaraSimulator
from app.venue import Edge

WALKING_SPEED_MPS = 1.5


@dataclass(frozen=True)
class CalculatedRoute:
    nodes: list[str]
    edges: list[Edge]
    eta_seconds: int
    distance_m: int
    distance_seconds: int
    crowd_penalty_seconds: int
    network_penalty_seconds: int
    access_penalty_seconds: int = 0


class RouteNotFoundError(ValueError):
    pass


class RoutingService:
    def __init__(self, camara: CamaraSimulator) -> None:
        self.camara = camara

    def _edge_cost_components(self, edge: Edge) -> tuple[float, float, float]:
        base_seconds = edge.distance_m / WALKING_SPEED_MPS
        congestion = self.camara.get_congestion(edge.zone)
        crowd_penalty = base_seconds * congestion * 0.90
        # Network congestion affects dispatch coordination after pedestrian delay.
        network_penalty = (
            (base_seconds + crowd_penalty)
            * congestion
            * 0.20
            * self.camara.network_penalty_multiplier()
        )
        return base_seconds, crowd_penalty, network_penalty

    def _edge_cost_seconds(self, edge: Edge) -> float:
        return sum(self._edge_cost_components(edge))

    def shortest_route(self, source: str, destination: str) -> CalculatedRoute:
        queue: list[tuple[float, str]] = [(0.0, source)]
        costs = {source: 0.0}
        previous: dict[str, tuple[str, Edge]] = {}

        while queue:
            current_cost, current = heapq.heappop(queue)
            if current == destination:
                break
            if current_cost != costs[current]:
                continue
            for edge in self.camara.neighbors(current):
                candidate = current_cost + self._edge_cost_seconds(edge)
                if candidate < costs.get(edge.destination, float("inf")):
                    costs[edge.destination] = candidate
                    previous[edge.destination] = (current, edge)
                    heapq.heappush(queue, (candidate, edge.destination))

        if destination not in costs:
            raise RouteNotFoundError(f"No route from {source} to {destination}")

        nodes = [destination]
        edges: list[Edge] = []
        cursor = destination
        while cursor != source:
            parent, edge = previous[cursor]
            edges.append(edge)
            nodes.append(parent)
            cursor = parent
        nodes.reverse()
        edges.reverse()
        return CalculatedRoute(
            nodes=nodes,
            edges=edges,
            eta_seconds=round(costs[destination]),
            distance_m=sum(edge.distance_m for edge in edges),
            distance_seconds=round(sum(self._edge_cost_components(edge)[0] for edge in edges)),
            crowd_penalty_seconds=round(sum(self._edge_cost_components(edge)[1] for edge in edges)),
            network_penalty_seconds=round(sum(self._edge_cost_components(edge)[2] for edge in edges)),
        )

    def route_via_gate(self, source: str, gate: str, destination: str) -> CalculatedRoute:
        """Score a candidate entry gate so the agent can explain its choice."""
        to_gate = self.shortest_route(source, gate)
        from_gate = self.shortest_route(gate, destination)
        return CalculatedRoute(
            nodes=to_gate.nodes + from_gate.nodes[1:],
            edges=to_gate.edges + from_gate.edges,
            eta_seconds=to_gate.eta_seconds + from_gate.eta_seconds,
            distance_m=to_gate.distance_m + from_gate.distance_m,
            distance_seconds=to_gate.distance_seconds + from_gate.distance_seconds,
            crowd_penalty_seconds=to_gate.crowd_penalty_seconds + from_gate.crowd_penalty_seconds,
            network_penalty_seconds=to_gate.network_penalty_seconds + from_gate.network_penalty_seconds,
        )
