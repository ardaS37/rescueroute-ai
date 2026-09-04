"""Dynamic shortest-path calculation for emergency response routes.

The cost of an edge is the documented four-term ETA formula::

    ETA = distance + crowd penalty + network penalty + access penalty

Each term is independent: crowd tracks the density of the zone the corridor
runs through, network tracks cellular pressure reported by CAMARA Congestion
Insights (offset by an active QoD session), and access is the controlled-entry
time that is neither distance nor crowd.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.camara_simulator import CamaraSimulator
from app.venue import Edge

WALKING_SPEED_MPS = 1.5
CROWD_WEIGHT = 0.90
NETWORK_WEIGHT = 0.20
# Pedestrian flow collapses rather than degrading linearly: walking speed falls
# away as a zone approaches jam density (Fruin level of service E/F).  A linear
# term made an 85%-dense corridor only 1.8x slower, so the router was willing to
# send a team straight through a crush to save distance.
CROWD_HEADROOM_FLOOR = 0.15


@dataclass(frozen=True)
class EdgeCost:
    distance_seconds: float
    crowd_seconds: float
    network_seconds: float
    access_seconds: float

    @property
    def total(self) -> float:
        return self.distance_seconds + self.crowd_seconds + self.network_seconds + self.access_seconds


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


def _split_evenly(components: Sequence[float]) -> tuple[list[int], int]:
    """Round the components so that they add up to the rounded total.

    Rounding each term on its own made the dashboard show a breakdown that did
    not sum to the ETA it was explaining, on roughly a third of all routes.
    """
    total = round(sum(components))
    floors = [int(value) for value in components]
    order = sorted(range(len(components)), key=lambda index: components[index] - floors[index], reverse=True)
    for index in order[: total - sum(floors)]:
        floors[index] += 1
    return floors, total


class RoutingService:
    def __init__(self, camara: CamaraSimulator) -> None:
        self.camara = camara

    @staticmethod
    def crowd_multiplier(congestion: float) -> float:
        """Extra walking time per unit of free-flow time at a given density."""
        return CROWD_WEIGHT * congestion / max(1 - congestion, CROWD_HEADROOM_FLOOR)

    def _edge_cost(self, source: str, edge: Edge) -> EdgeCost:
        base_seconds = edge.distance_m / WALKING_SPEED_MPS
        congestion = self.camara.get_congestion(edge.zone)
        crowd_seconds = base_seconds * self.crowd_multiplier(congestion)
        # Cellular pressure is a venue-wide signal, not a restatement of this
        # corridor's density: a quiet corridor still costs coordination time
        # when the operator reports a loaded network.
        network_seconds = (
            (base_seconds + crowd_seconds)
            * self.camara.network_pressure(congestion)
            * NETWORK_WEIGHT
            * self.camara.qos_relief()
        )
        return EdgeCost(
            distance_seconds=base_seconds,
            crowd_seconds=crowd_seconds,
            network_seconds=network_seconds,
            access_seconds=self.camara.access_seconds(source, edge),
        )

    def _build_route(self, nodes: list[str], edges: list[Edge]) -> CalculatedRoute:
        costs = [self._edge_cost(nodes[index], edge) for index, edge in enumerate(edges)]
        (distance, crowd, network, access), total = _split_evenly((
            sum(cost.distance_seconds for cost in costs),
            sum(cost.crowd_seconds for cost in costs),
            sum(cost.network_seconds for cost in costs),
            sum(cost.access_seconds for cost in costs),
        ))
        return CalculatedRoute(
            nodes=nodes,
            edges=edges,
            eta_seconds=total,
            distance_m=sum(edge.distance_m for edge in edges),
            distance_seconds=distance,
            crowd_penalty_seconds=crowd,
            network_penalty_seconds=network,
            access_penalty_seconds=access,
        )

    def shortest_route(
        self, source: str, destination: str, *, excluded: frozenset[str] = frozenset()
    ) -> CalculatedRoute:
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
                if edge.destination in excluded:
                    continue
                candidate = current_cost + self._edge_cost(current, edge).total
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
        return self._build_route(nodes, edges)

    def route_via_gate(self, source: str, gate: str, destination: str) -> CalculatedRoute:
        """Score a candidate entry gate so the agent can explain its choice.

        Both legs exclude the other gates.  Without that, "via gate B" was two
        unconstrained shortest paths that could walk back out of the venue and
        enter through a different gate, which is not the option the dashboard
        claims to be comparing.
        """
        excluded = frozenset(self.camara.gates) - {gate, source, destination}
        to_gate = self.shortest_route(source, gate, excluded=excluded)
        from_gate = self.shortest_route(gate, destination, excluded=excluded)
        return self._build_route(
            to_gate.nodes + from_gate.nodes[1:], to_gate.edges + from_gate.edges
        )
