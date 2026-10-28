"""Plane geometry the board modules share: turns, distances, insides.

One home for each measure, so placement, pours, fanout, routing and the
audit measure copper the same way. Every distance is exact (no sampling),
in the board's millimetres, with y pointing down as KiCad draws.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = tuple[float, float]
Rect = tuple[float, float, float, float]


def rotate(x: float, y: float, turn: float) -> Point:
    """A point about a footprint's origin, turned as KiCad turns the footprint.

    KiCad turns counter-clockwise as seen, with y pointing down.
    """
    r = math.radians(turn)
    return (x * math.cos(r) + y * math.sin(r), -x * math.sin(r) + y * math.cos(r))


def point_segment(p: Point, a: Point, b: Point) -> float:
    """The least distance from a point to a segment."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = (
        0.0
        if length == 0
        else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    )
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def segment_segment(a: Point, b: Point, c: Point, d: Point) -> float:
    """The least distance between two segments: 0 where they cross."""

    def side(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = side(c, d, a), side(c, d, b)
    d3, d4 = side(a, b, c), side(a, b, d)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0) and d1 and d2 and d3 and d4:
        return 0.0
    return min(
        point_segment(a, c, d),
        point_segment(b, c, d),
        point_segment(c, a, b),
        point_segment(d, a, b),
    )


def point_box(p: Point, box: Rect) -> float:
    """The least distance from a point to a box: 0 inside it."""
    nx = min(max(p[0], box[0]), box[2])
    ny = min(max(p[1], box[1]), box[3])
    return math.hypot(p[0] - nx, p[1] - ny)


def segment_box(a: Point, b: Point, box: Rect) -> float:
    """The least distance from a segment to a box: 0 where it enters it."""
    left, top, right, bottom = box
    if any(left <= p[0] <= right and top <= p[1] <= bottom for p in (a, b)):
        return 0.0
    corners = ((left, top), (right, top), (right, bottom), (left, bottom))
    return min(
        segment_segment(a, b, corners[k], corners[(k + 1) % 4]) for k in range(4)
    )


def inside(p: Point, polygon: Sequence[Point]) -> bool:
    """Whether a point is inside a polygon, by counting edge crossings."""
    x, y = p
    within = False
    n = len(polygon)
    for k in range(n):
        (ax, ay), (bx, by) = polygon[k], polygon[(k + 1) % n]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            within = not within
    return within


def edge_distance(p: Point, polygon: Sequence[Point]) -> float:
    """The least distance from a point to a polygon's edge."""
    n = len(polygon)
    return min(point_segment(p, polygon[k], polygon[(k + 1) % n]) for k in range(n))


def segment_polygon(a: Point, b: Point, polygon: Sequence[Point]) -> float:
    """The least distance from a segment to a polygon: 0 where it reaches inside."""
    if inside(a, polygon) or inside(b, polygon):
        return 0.0
    n = len(polygon)
    return min(
        segment_segment(a, b, polygon[k], polygon[(k + 1) % n]) for k in range(n)
    )
