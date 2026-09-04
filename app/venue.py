"""Venue templates for deterministic, privacy-preserving crowd simulations."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, radians

# The pixel canvas every template is drawn on, shared with both 2D views.
CANVAS_WIDTH, CANVAS_HEIGHT = 840, 510
METRES_PER_DEGREE_LATITUDE = 111_320.0


@dataclass(frozen=True)
class GeoAnchor:
    """Places the template's pixel canvas on the real map.

    Without this the graph carried pixels only, so a CAMARA Location Retrieval
    fix could not be mapped to a node and a Geofencing area could not be placed
    on the gate it was supposed to watch.
    """

    latitude: float
    longitude: float
    # Calibrated so the median corridor's straight-line length matches the
    # walking distance the graph declares for it. The canvas is a schematic, so
    # individual corridors still deviate.
    metres_per_pixel: float
    gate_radius_m: int = 60

    def coordinates(self, x: int, y: int) -> tuple[float, float]:
        """Real position of a canvas point; the anchor sits at the canvas centre."""
        east_m = (x - CANVAS_WIDTH / 2) * self.metres_per_pixel
        south_m = (y - CANVAS_HEIGHT / 2) * self.metres_per_pixel
        latitude = self.latitude - south_m / METRES_PER_DEGREE_LATITUDE
        longitude = self.longitude + east_m / (
            METRES_PER_DEGREE_LATITUDE * cos(radians(self.latitude))
        )
        return round(latitude, 6), round(longitude, 6)


def distance_metres(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Equirectangular distance; exact enough across a single venue."""
    mean_latitude = radians((first[0] + second[0]) / 2)
    north = (first[0] - second[0]) * METRES_PER_DEGREE_LATITUDE
    east = (first[1] - second[1]) * METRES_PER_DEGREE_LATITUDE * cos(mean_latitude)
    return (north**2 + east**2) ** 0.5


@dataclass(frozen=True)
class Edge:
    destination: str
    distance_m: int
    zone: str | None = None
    # Controlled-access time that is neither distance nor crowd: turnstiles, a
    # staffed entry, unlocking a service gate.  This is the fourth term of the
    # documented ETA formula.
    access_seconds: int = 0


@dataclass(frozen=True)
class ResponseTeam:
    """A dispatchable medical team and the venue node it is staged at."""

    id: str
    name: str
    phone_number: str
    home_base: str


# Numbers are Nokia Network-as-Code simulator handsets; +99999991002 is not
# provisioned on the platform and always answers NOT_CONNECTED.
# Every venue stages the same three teams.  Positional variety is not baked in:
# it emerges at runtime, because a team that resolves an incident stays at the
# scene and is therefore closer to whatever happens next nearby.
DEFAULT_TEAMS = (
    ResponseTeam("medic_alpha", "Medic Alpha", "+99999991000", "ambulance_bay"),
    ResponseTeam("medic_bravo", "Medic Bravo", "+99999991001", "ambulance_bay"),
    ResponseTeam("medic_charlie", "Medic Charlie", "+99999991003", "ambulance_bay"),
)


@dataclass(frozen=True)
class VenueTemplate:
    key: str
    title: str
    description: str
    gates: frozenset[str]
    graph: dict[str, tuple[Edge, ...]]
    zones: tuple[str, ...]
    positions: dict[str, tuple[int, int]]
    # Crowd patterns name the zones they load.  Deriving them from the position
    # of a zone in ``zones`` used to surge the wrong areas: "stage cluster" on
    # the festival map loaded the entry and east lanes and never the stage.
    crowd_bias: dict[str, dict[str, float]] = field(default_factory=dict)
    # Fixed densities applied after the seeded draw, for patterns that need a
    # reproducible contrast in the demo rather than a random one.
    crowd_overrides: dict[str, dict[str, float]] = field(default_factory=dict)
    teams: tuple[ResponseTeam, ...] = DEFAULT_TEAMS
    geo: GeoAnchor | None = None

    def coordinates(self, node: str) -> tuple[float, float] | None:
        position = self.positions.get(node)
        if position is None or self.geo is None:
            return None
        return self.geo.coordinates(*position)


def _graph(
    connections: list[tuple[str, str, int, str | None] | tuple[str, str, int, str | None, int]],
) -> dict[str, tuple[Edge, ...]]:
    mutable: dict[str, list[Edge]] = {}
    for connection in connections:
        left, right, distance, zone = connection[:4]
        access = connection[4] if len(connection) > 4 else 0
        mutable.setdefault(left, []).append(Edge(right, distance, zone, access))
        mutable.setdefault(right, []).append(Edge(left, distance, zone, access))
    return {node: tuple(edges) for node, edges in mutable.items()}


TEMPLATES = {
    "stadium_match": VenueTemplate(
        key="stadium_match", title="Stadium match",
        # Lusail Stadium, Qatar: a real MENA mega-event venue.
        geo=GeoAnchor(latitude=25.420560, longitude=51.490280, metres_per_pixel=0.64, gate_radius_m=55),
        description="Oval stadium with circulation zones and a main-stage incident area.",
        gates=frozenset({"gate_a", "gate_b", "gate_c", "gate_d"}),
        zones=("north_zone", "west_zone", "south_zone", "central_zone", "east_zone"),
        crowd_bias={
            "balanced": {},
            # Spectators queue at the two western gates.
            "gate_surge": {"north_zone": 0.40, "west_zone": 0.35},
            # The crowd packs the bowl around the stage instead of the entries.
            "stage_cluster": {"central_zone": 0.45, "south_zone": 0.30},
        },
        crowd_overrides={
            # Deterministic demo contrast: both northern approaches are jammed
            # while the southern ring stays clear, so the gate comparison has a
            # visibly correct answer rather than a seeded coincidence.
            "gate_surge": {"north_zone": 0.85, "west_zone": 0.85, "south_zone": 0.15},
        },
        positions={
            # This coordinate system is shared with the free 2D arena.  The
            # ambulance approaches from the west; A/D are the north gates and
            # B/C are the south gates around the stage.
            "ambulance_bay": (40, 255), "north_access": (75, 70), "south_access": (75, 440),
            "north_outer_road": (755, 70), "south_outer_road": (755, 440),
            "gate_a": (180, 115), "gate_b": (180, 385), "gate_c": (660, 385), "gate_d": (660, 115), "north_corridor": (350, 115),
            "south_corridor": (360, 385), "central_plaza": (420, 250),
            "east_concourse": (600, 250), "first_aid": (520, 385),
            # Off the southern row and out from under the plaza, so the
            # stage is not crowded by its neighbours or by their corridor.
            "main_stage": (440, 335),
        },
        graph=_graph([
            # External emergency road: teams enter from the west ambulance bay,
            # then travel around the stadium perimeter before each gate.
            ("ambulance_bay", "north_access", 90, None), ("ambulance_bay", "south_access", 90, None),
            ("north_access", "gate_a", 70, None), ("south_access", "gate_b", 70, None),
            ("north_access", "north_outer_road", 110, None), ("north_outer_road", "gate_d", 70, None),
            ("south_access", "south_outer_road", 110, None), ("south_outer_road", "gate_c", 70, None),
            # Passing through a gate costs controlled-access time: A is the main
            # public entry, C is the service gate, D is staffed for the away end.
            ("gate_a", "north_corridor", 110, "north_zone", 25), ("north_corridor", "central_plaza", 170, "north_zone"),
            # Gate B is deliberately tied to west_zone: congestion right in
            # front of B now makes that entry costly instead of invisible.
            ("gate_b", "south_corridor", 130, "west_zone", 20), ("gate_c", "south_corridor", 130, "south_zone", 15),
            ("south_corridor", "central_plaza", 120, "south_zone"), ("central_plaza", "main_stage", 130, "central_zone"),
            ("gate_d", "east_concourse", 170, "east_zone", 30), ("east_concourse", "central_plaza", 140, "east_zone"),
            ("south_corridor", "first_aid", 100, "south_zone"), ("first_aid", "main_stage", 120, "central_zone"),
        ]),
    ),
    "music_festival": VenueTemplate(
        key="music_festival", title="Music festival",
        # Expo City Dubai festival grounds.
        geo=GeoAnchor(latitude=24.960000, longitude=55.150000, metres_per_pixel=0.53, gate_radius_m=35),
        description="Open-air site with stage, food court, and several pedestrian flows.",
        gates=frozenset({"gate_a", "gate_b", "gate_c"}),
        zones=("stage_zone", "food_zone", "north_lane", "east_lane", "entry_zone"),
        crowd_bias={
            "balanced": {},
            # Doors open: the entry plaza and the eastern path carry the queue.
            "gate_surge": {"entry_zone": 0.40, "east_lane": 0.32},
            # Headline act: the stage apron and the food route feeding it fill up.
            "stage_cluster": {"stage_zone": 0.45, "food_zone": 0.30},
        },
        positions={
            "ambulance_bay": (60, 255), "gate_a": (190, 100), "gate_b": (185, 255),
            "gate_c": (195, 410), "entry_plaza": (380, 110), "food_court": (405, 260),
            "east_path": (420, 420), "first_aid": (585, 340), "main_stage": (750, 255),
        },
        graph=_graph([
            ("ambulance_bay", "gate_a", 200, None), ("ambulance_bay", "gate_b", 270, None), ("ambulance_bay", "gate_c", 300, None),
            ("gate_a", "entry_plaza", 100, "entry_zone", 20), ("gate_b", "food_court", 110, "food_zone", 15),
            ("gate_c", "east_path", 120, "east_lane", 15), ("entry_plaza", "main_stage", 250, "stage_zone"),
            ("food_court", "main_stage", 160, "food_zone"), ("east_path", "main_stage", 190, "east_lane"),
            ("food_court", "first_aid", 90, "north_lane"), ("first_aid", "main_stage", 130, "stage_zone"),
        ]),
    ),
    "pilgrimage_flow": VenueTemplate(
        key="pilgrimage_flow", title="Masjid al-Haram Hajj flow",
        # Masjid al-Haram, Mecca. Demonstration model only, not an
        # official operational map.
        geo=GeoAnchor(latitude=21.422510, longitude=39.826160, metres_per_pixel=0.98, gate_radius_m=45),
        description="Simplified Hajj/Umrah emergency-routing model around the Kaaba, Mataf, and Mas'a.",
        gates=frozenset({"king_abdulaziz_gate", "king_fahd_gate", "king_abdullah_gate", "al_safa_gate"}),
        zones=("western_courtyard", "northern_expansion", "mataf_zone", "masaa_zone", "medical_lane"),
        crowd_bias={
            "balanced": {},
            # Arrival waves press on the western courtyard and northern expansion.
            "gate_surge": {"western_courtyard": 0.40, "northern_expansion": 0.32},
            # Peak Tawaf: the Mataf ring and the Mas'a corridor carry the density.
            "stage_cluster": {"mataf_zone": 0.45, "masaa_zone": 0.30},
        },
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
            ("king_abdulaziz_gate", "western_courtyard", 150, "western_courtyard", 20),
            ("king_fahd_gate", "western_courtyard", 125, "western_courtyard", 20),
            ("king_abdullah_gate", "northern_expansion", 135, "northern_expansion", 25),
            ("northern_expansion", "mataf_ring", 160, "northern_expansion"),
            ("western_courtyard", "mataf_ring", 125, "mataf_zone"), ("mataf_ring", "kaaba_tawaf", 65, "mataf_zone"),
            ("al_safa_gate", "masaa_corridor", 115, "masaa_zone", 20), ("masaa_corridor", "mataf_ring", 180, "masaa_zone"),
            # The medical lane is a controlled but prioritised emergency route.
            ("al_safa_gate", "medical_post", 110, "medical_lane", 10), ("medical_post", "kaaba_tawaf", 170, "medical_lane"),
        ]),
    ),
}


def get_template(key: str) -> VenueTemplate:
    try:
        return TEMPLATES[key]
    except KeyError as error:
        raise ValueError(f"Unknown venue template '{key}'.") from error
