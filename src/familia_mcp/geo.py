"""Small, dependency-free geometry for the locations module.

Coordinates are (lon, lat) degrees, like GeoJSON. Containment and crossing
tests run in degree space (exact enough for "is this point inside Segovia?").
Distances run in a local equirectangular projection around a reference
latitude: accurate to well under 1% within ~100 km, which is all a
"did I pass by X?" threshold of tens of metres needs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

LonLat = tuple[float, float]
Ring = list[LonLat]


def haversine_m(a: LonLat, b: LonLat) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def metres_to_degrees(metres: float, lat: float) -> tuple[float, float]:
    """(dlon, dlat) spanning `metres` at latitude `lat`."""
    dlat = math.degrees(metres / EARTH_RADIUS_M)
    dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
    return dlon, dlat


@dataclass(frozen=True)
class BBox:
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    @classmethod
    def of(cls, coords: Iterable[LonLat]) -> "BBox":
        lons, lats = zip(*coords)
        return cls(min(lons), min(lats), max(lons), max(lats))

    def expand(self, metres: float) -> "BBox":
        dlon, dlat = metres_to_degrees(metres, max(abs(self.min_lat), abs(self.max_lat)))
        return BBox(self.min_lon - dlon, self.min_lat - dlat, self.max_lon + dlon, self.max_lat + dlat)

    def contains(self, p: LonLat) -> bool:
        return self.min_lon <= p[0] <= self.max_lon and self.min_lat <= p[1] <= self.max_lat

    def intersects_segment(self, a: LonLat, b: LonLat) -> bool:
        return not (max(a[0], b[0]) < self.min_lon or min(a[0], b[0]) > self.max_lon
                    or max(a[1], b[1]) < self.min_lat or min(a[1], b[1]) > self.max_lat)

    @property
    def center(self) -> LonLat:
        return ((self.min_lon + self.max_lon) / 2, (self.min_lat + self.max_lat) / 2)


class _Projector:
    """Local equirectangular projection to metres around `lat0`."""

    def __init__(self, lat0: float):
        self.kx = math.radians(1) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * EARTH_RADIUS_M

    def __call__(self, p: LonLat) -> tuple[float, float]:
        return p[0] * self.kx, p[1] * self.ky


def _point_segment_m(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _orient(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a: Sequence[float], b: Sequence[float], c: Sequence[float], d: Sequence[float]) -> bool:
    d1, d2, d3, d4 = _orient(c, d, a), _orient(c, d, b), _orient(a, b, c), _orient(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return True

    def on(p, q, r):  # r collinear with p-q and inside its box
        return min(p[0], q[0]) <= r[0] <= max(p[0], q[0]) and min(p[1], q[1]) <= r[1] <= max(p[1], q[1])

    return ((d1 == 0 and on(c, d, a)) or (d2 == 0 and on(c, d, b))
            or (d3 == 0 and on(a, b, c)) or (d4 == 0 and on(a, b, d)))


def _segment_segment_m(a, b, c, d) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(_point_segment_m(a, c, d), _point_segment_m(b, c, d),
               _point_segment_m(c, a, b), _point_segment_m(d, a, b))


def _in_ring(p: LonLat, ring: Ring) -> bool:
    x, y = p
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


@dataclass
class Shape:
    """A geocoded place: polygons (with holes), polylines and/or points."""

    polygons: list[list[Ring]] = field(default_factory=list)  # [outer, hole, hole...]
    lines: list[list[LonLat]] = field(default_factory=list)
    points: list[LonLat] = field(default_factory=list)

    @classmethod
    def from_geojson(cls, geom: dict[str, Any]) -> "Shape":
        shape = cls()
        shape._add(geom)
        if not (shape.polygons or shape.lines or shape.points):
            raise ValueError("empty geometry")
        return shape

    def _add(self, g: dict[str, Any]) -> None:
        t, c = g.get("type"), g.get("coordinates")
        if t == "Point":
            self.points.append((float(c[0]), float(c[1])))
        elif t == "MultiPoint":
            self.points.extend((float(x), float(y)) for x, y, *_ in c)
        elif t == "LineString":
            self.lines.append([(float(x), float(y)) for x, y, *_ in c])
        elif t == "MultiLineString":
            self.lines.extend([(float(x), float(y)) for x, y, *_ in line] for line in c)
        elif t == "Polygon":
            self.polygons.append([[(float(x), float(y)) for x, y, *_ in ring] for ring in c])
        elif t == "MultiPolygon":
            self.polygons.extend([[(float(x), float(y)) for x, y, *_ in ring] for ring in poly] for poly in c)
        elif t == "GeometryCollection":
            for sub in g.get("geometries") or []:
                self._add(sub)

    @property
    def is_area(self) -> bool:
        return bool(self.polygons)

    def bbox(self) -> BBox:
        coords = [p for poly in self.polygons for p in poly[0]] + [p for l in self.lines for p in l] + self.points
        return BBox.of(coords)

    def _edges(self) -> Iterable[tuple[LonLat, LonLat]]:
        for poly in self.polygons:
            for ring in poly:
                for i in range(len(ring) - 1):
                    yield ring[i], ring[i + 1]
        for line in self.lines:
            for i in range(len(line) - 1):
                yield line[i], line[i + 1]

    def contains(self, p: LonLat) -> bool:
        for poly in self.polygons:
            if _in_ring(p, poly[0]) and not any(_in_ring(p, hole) for hole in poly[1:]):
                return True
        return False

    def distance_m(self, p: LonLat) -> float:
        """Metres from p to the shape (0 inside an area)."""
        if self.contains(p):
            return 0.0
        proj = _Projector(p[1])
        pp = proj(p)
        best = math.inf
        for a, b in self._edges():
            best = min(best, _point_segment_m(pp, proj(a), proj(b)))
        for q in self.points:
            best = min(best, math.hypot(*(u - v for u, v in zip(pp, proj(q)))))
        for line in self.lines:
            if len(line) == 1:
                best = min(best, math.hypot(*(u - v for u, v in zip(pp, proj(line[0])))))
        return best

    def segment_distance_m(self, a: LonLat, b: LonLat) -> float:
        """Metres from the segment a-b to the shape (0 if it enters an area)."""
        if self.contains(a) or self.contains(b):
            return 0.0
        proj = _Projector((a[1] + b[1]) / 2)
        pa, pb = proj(a), proj(b)
        best = math.inf
        for c, d in self._edges():
            best = min(best, _segment_segment_m(pa, pb, proj(c), proj(d)))
            if best == 0.0:
                return 0.0
        for q in self.points:
            best = min(best, _point_segment_m(proj(q), pa, pb))
        return best
