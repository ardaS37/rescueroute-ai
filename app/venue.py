"""Venue templates for deterministic, privacy-preserving crowd simulations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Edge:
    destination: str
    distance_m: int
    zone: str | None = None


@dataclass(frozen=True)
class VenueTemplate:
    key: str
    title: str
    description: str
    gates: frozenset[str]
    graph: dict[str, tuple[Edge, ...]]
    zones: tuple[str, ...]
    positions: dict[str, tuple[int, int]]


def _graph(connections: list[tuple[str, str, int, str | None]]) -> dict[str, tuple[Edge, ...]]:
    mutable: dict[str, list[Edge]] = {}
    for left, right, distance, zone in connections:
        mutable.setdefault(left, []).append(Edge(right, distance, zone))
        mutable.setdefault(right, []).append(Edge(left, distance, zone))
    return {node: tuple(edges) for node, edges in mutable.items()}


TEMPLATES = {
    "stadium_match": VenueTemplate(
        key="stadium_match", title="Stadium match",
        description="Oval stadium with circulation zones and a main-stage incident area.",
        gates=frozenset({"gate_a", "gate_b", "gate_c", "gate_d"}),
        zones=("north_zone", "west_zone", "south_zone", "central_zone", "east_zone"),
        positions={
            # This coordinate system is shared with the free 2D arena.  The
            # ambulance approaches from the west; A/D are the north gates and
            # B/C are the south gates around the stage.
            "ambulance_bay": (40, 255), "north_access": (75, 70), "south_access": (75, 440),
            "north_outer_road": (755, 70), "south_outer_road": (755, 440),
            "gate_a": (180, 115), "gate_b": (180, 385), "gate_c": (660, 385), "gate_d": (660, 115), "north_corridor": (350, 115),
            "south_corridor": (360, 385), "central_plaza": (420, 250),
            "east_concourse": (600, 250), "first_aid": (520, 385), "main_stage": (420, 395),
        },
        graph=_graph([
            # External emergency road: teams enter from the west ambulance bay,
            # then travel around the stadium perimeter before each gate.
            ("ambulance_bay", "north_access", 90, None), ("ambulance_bay", "south_access", 90, None),
            ("north_access", "gate_a", 70, None), ("south_access", "gate_b", 70, None),
            ("north_access", "north_outer_road", 110, None), ("north_outer_road", "gate_d", 70, None),
            ("south_access", "south_outer_road", 110, None), ("south_outer_road", "gate_c", 70, None),
            ("gate_a", "north_corridor", 110, "north_zone"), ("north_corridor", "central_plaza", 170, "north_zone"),
            # Gate B is deliberately tied to west_zone: congestion right in
            # front of B now makes that entry costly instead of invisible.
            ("gate_b", "south_corridor", 130, "west_zone"), ("gate_c", "south_corridor", 130, "south_zone"),
            ("south_corridor", "central_plaza", 120, "south_zone"), ("central_plaza", "main_stage", 130, "central_zone"),
            ("gate_d", "east_concourse", 170, "east_zone"), ("east_concourse", "central_plaza", 140, "east_zone"),
            ("south_corridor", "first_aid", 100, "south_zone"), ("first_aid", "main_stage", 120, "central_zone"),
        ]),
    ),
    "music_festival": VenueTemplate(
        key="music_festival", title="Music festival",
        description="Open-air site with stage, food court, and several pedestrian flows.",
        gates=frozenset({"gate_a", "gate_b", "gate_c"}),
        zones=("stage_zone", "food_zone", "north_lane", "east_lane", "entry_zone"),
        positions={
            "ambulance_bay": (60, 255), "gate_a": (190, 100), "gate_b": (185, 255),
            "gate_c": (195, 410), "entry_plaza": (380, 110), "food_court": (405, 260),
            "east_path": (420, 420), "first_aid": (585, 340), "main_stage": (750, 255),
        },
        graph=_graph([
            ("ambulance_bay", "gate_a", 200, None), ("ambulance_bay", "gate_b", 270, None), ("ambulance_bay", "gate_c", 300, None),
            ("gate_a", "entry_plaza", 100, "entry_zone"), ("gate_b", "food_court", 110, "food_zone"),
            ("gate_c", "east_path", 120, "east_lane"), ("entry_plaza", "main_stage", 250, "stage_zone"),
            ("food_court", "main_stage", 160, "food_zone"), ("east_path", "main_stage", 190, "east_lane"),
            ("food_court", "first_aid", 90, "north_lane"), ("first_aid", "main_stage", 130, "stage_zone"),
        ]),
    ),
    "pilgrimage_flow": VenueTemplate(
        key="pilgrimage_flow", title="Masjid al-Haram Hajj flow",
        description="Simplified Hajj/Umrah emergency-routing model around the Kaaba, Mataf, and Mas'a.",
        gates=frozenset({"king_abdulaziz_gate", "king_fahd_gate", "king_abdullah_gate", "al_safa_gate"}),
        zones=("western_courtyard", "northern_expansion", "mataf_zone", "masaa_zone", "medical_lane"),
        positions={
            "ambulance_bay": (55, 420), "emergency_access": (130, 330),
            "king_abdulaziz_gate": (220, 405), "king_fahd_gate": (185, 225),
            "king_abdullah_gate": (405, 90), "al_safa_gate": (675, 390),
            "western_courtyard": (320, 285), "northern_expansion": (430, 185),
            "mataf_ring": (465, 290), "kaaba_tawaf": (465, 320),
            "masaa_corridor": (635, 285), "medical_post": (600, 430),
        },
        graph=_graph([
            ("ambulance_bay", "emergency_access", 105, None),
            ("emergency_access", "king_abdulaziz_gate", 110, None), ("emergency_access", "king_fahd_gate", 155, None),
            ("emergency_access", "king_abdullah_gate", 250, None), ("emergency_access", "al_safa_gate", 290, None),
            ("king_abdulaziz_gate", "western_courtyard", 150, "western_courtyard"),
            ("king_fahd_gate", "western_courtyard", 125, "western_courtyard"),
            ("king_abdullah_gate", "northern_expansion", 135, "northern_expansion"),
            ("northern_expansion", "mataf_ring", 160, "northern_expansion"),
            ("western_courtyard", "mataf_ring", 125, "mataf_zone"), ("mataf_ring", "kaaba_tawaf", 65, "mataf_zone"),
            ("al_safa_gate", "masaa_corridor", 115, "masaa_zone"), ("masaa_corridor", "mataf_ring", 180, "masaa_zone"),
            ("al_safa_gate", "medical_post", 110, "medical_lane"), ("medical_post", "kaaba_tawaf", 170, "medical_lane"),
        ]),
    ),
}


def get_template(key: str) -> VenueTemplate:
    try:
        return TEMPLATES[key]
    except KeyError as error:
        raise ValueError(f"Unknown venue template '{key}'.") from error
