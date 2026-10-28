"""Where a symbol's pins land on the sheet.

A library draws a symbol with y growing upward; a sheet has y growing
downward. A placed symbol has a position, a rotation of 0, 90, 180 or 270
degrees and optionally a mirror. KiCad connects a pin only to something
that touches its end point exactly, so this arithmetic decides whether a
written schematic is connected at all.

The transform is measured, not assumed. Across the real boards saved by
the KiCad 9 and 10 programs (22,491 pins in the eight placements KiCad
writes), turning the pin counter-clockwise as seen on screen and then
applying the mirror put 20,174 pins exactly on a wire end, label, junction
or no-connect mark. Each of the three other orders of the same steps put
fewer there, and for the placements that tell them apart (90 and 270
degrees with a mirror) the gap is wide: 254 of 269 pins against 163, and
361 of 477 against 135.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from kairo.kicad.sexpr import Atom, SList

#: KiCad's schematic grid, 50 mil, in mm.
GRID = 1.27

Rotation = Literal[0, 90, 180, 270]
Mirror = Literal["", "x", "y"]

#: The eight placements KiCad writes. Any other combination of rotation and
#: mirror is the same picture as one of these, and KiCad saves it as that.
PLACEMENTS: tuple[tuple[Rotation, Mirror], ...] = (
    (0, ""),
    (90, ""),
    (180, ""),
    (270, ""),
    (0, "x"),
    (0, "y"),
    (90, "x"),
    (270, "x"),
)

# Cosine and sine of each rotation, exact.
_TURN: dict[int, tuple[int, int]] = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


@dataclass(frozen=True, slots=True)
class Placement:
    """Where and how a symbol is placed on a sheet.

    Attributes:
        x: The symbol origin's x on the sheet, in mm.
        y: The symbol origin's y on the sheet, in mm, growing downward.
        rotation: Counter-clockwise as seen on screen, in degrees.
        mirror: ``x`` flips top and bottom, ``y`` flips left and right.
    """

    x: float
    y: float
    rotation: Rotation = 0
    mirror: Mirror = ""


def to_sheet(placement: Placement, x: float, y: float) -> tuple[float, float]:
    """Turn a point in a symbol's own coordinates into a point on the sheet.

    Args:
        placement: How the symbol is placed.
        x: The point's x in the library's coordinates, in mm.
        y: The point's y in the library's coordinates, growing upward.

    Returns:
        The point on the sheet, rounded to KiCad's 100 nm unit.
    """
    dx, dy = direction(placement, x, -y)
    return (round(placement.x + dx, 4), round(placement.y + dy, 4))


def direction(placement: Placement, dx: float, dy: float) -> tuple[float, float]:
    """Turn and mirror a sheet-frame offset the way the placement does.

    Args:
        placement: How the symbol is placed.
        dx: The offset's x with the symbol unrotated, in mm.
        dy: The offset's y with the symbol unrotated, growing downward.

    Returns:
        The offset as placed.
    """
    cos, sin = _TURN[placement.rotation]
    turned_x = dx * cos + dy * sin
    turned_y = -dx * sin + dy * cos
    if placement.mirror == "x":
        turned_y = -turned_y
    elif placement.mirror == "y":
        turned_x = -turned_x
    return (turned_x, turned_y)


def pin_outward(placement: Placement, angle: float) -> tuple[int, int]:
    """The direction leading away from a symbol at one of its pins.

    A library pin's angle says which way the pin is drawn from its
    connection point, which is toward the body; a wire leaves the other
    way.

    Args:
        placement: How the symbol is placed.
        angle: The pin's angle in the library, in degrees.

    Returns:
        A unit step on the sheet, such as ``(1, 0)`` for rightward or
        ``(0, 1)`` for downward.

    Raises:
        ValueError: If the angle is not a multiple of 90 degrees.
    """
    if angle % 90:
        raise ValueError(f"a pin at {angle} degrees is not on the grid's axes")
    cos, sin = _TURN[int(angle) % 360]
    # Toward the body in the library is (cos, sin) with y upward, which is
    # (cos, -sin) on the sheet; outward is the opposite.
    dx, dy = direction(placement, -cos, sin)
    return (round(dx), round(dy))


def snap(value: float) -> float:
    """The nearest point of KiCad's 1.27 mm grid.

    Args:
        value: A length in mm.

    Returns:
        The nearest multiple of 1.27 mm, rounded to KiCad's 100 nm unit.
    """
    return round(round(value / GRID) * GRID, 4)


@dataclass(frozen=True, slots=True)
class Box:
    """A rectangle with sides along the axes, in mm.

    Attributes:
        left: The smallest x.
        top: The smallest y.
        right: The largest x.
        bottom: The largest y.
    """

    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        """The box's extent in x."""
        return self.right - self.left

    @property
    def height(self) -> float:
        """The box's extent in y."""
        return self.bottom - self.top

    def overlaps(self, other: Box, gap: float = 0.0) -> bool:
        """Whether two boxes come within a gap of each other.

        Args:
            other: The other box.
            gap: How far apart they must be to count as clear.

        Returns:
            True if they overlap or are closer than the gap. Boxes that
            only touch along an edge, with no gap asked for, do not overlap.
        """
        return (
            self.left < other.right + gap
            and other.left < self.right + gap
            and self.top < other.bottom + gap
            and other.top < self.bottom + gap
        )


_UNIT_NAME = re.compile(r"_(\d+)_(\d+)$")


def symbol_box(symbol: SList, unit: int = 1, body_style: int = 1) -> Box:
    """The box around a symbol's drawing and pins, in the library's frame.

    The drawing's rectangles, lines, circles, arcs and curves and the full
    length of every pin are counted. Text is not: its size depends on the
    font, which the text placer measures.

    Args:
        symbol: A symbol as a schematic embeds it or a library holds it,
            with its ``(symbol "<name>_<unit>_<style>" ...)`` children.
        unit: The unit to measure; shapes of unit 0 belong to every unit.
        body_style: The body style to measure; 0 belongs to every style.

    Returns:
        The box, with y growing upward as libraries draw. A symbol with
        nothing drawn gives an empty box at the origin.

    Raises:
        ValueError: If a shape is not written the way KiCad writes it.
    """
    xs: list[float] = []
    ys: list[float] = []

    def add(x: float, y: float) -> None:
        xs.append(x)
        ys.append(y)

    for child in symbol.lists("symbol"):
        name = child.items[1]
        found = _UNIT_NAME.search(name.value) if isinstance(name, Atom) else None
        if found is None:
            raise ValueError("a symbol unit is not named <name>_<unit>_<style>")
        if int(found.group(1)) not in (0, unit) or int(found.group(2)) not in (
            0,
            body_style,
        ):
            continue
        for shape in child.lists():
            if shape.head == "rectangle":
                add(*_xy(shape, "start"))
                add(*_xy(shape, "end"))
            elif shape.head in ("polyline", "bezier"):
                # A curve stays inside the hull of its control points.
                points = shape.first("pts")
                for point in points.lists("xy") if points else []:
                    add(_num(point, 1), _num(point, 2))
            elif shape.head == "circle":
                cx, cy = _xy(shape, "center")
                radius = _num(_need(shape, "radius"), 1)
                add(cx - radius, cy - radius)
                add(cx + radius, cy + radius)
            elif shape.head == "arc":
                for x, y in _arc_extent(
                    _xy(shape, "start"), _xy(shape, "mid"), _xy(shape, "end")
                ):
                    add(x, y)
            elif shape.head == "pin":
                at = _need(shape, "at")
                x, y, angle = _num(at, 1), _num(at, 2), _num(at, 3)
                length = _num(_need(shape, "length"), 1)
                add(x, y)
                radians = math.radians(angle)
                add(
                    round(x + length * math.cos(radians), 4),
                    round(y + length * math.sin(radians), 4),
                )
    if not xs:
        return Box(0.0, 0.0, 0.0, 0.0)
    return Box(min(xs), min(ys), max(xs), max(ys))


def placed_box(placement: Placement, box: Box) -> Box:
    """A library-frame box as it lies on the sheet once placed.

    Args:
        placement: How the symbol is placed.
        box: The box in the library's frame, y upward.

    Returns:
        The box on the sheet, y downward.
    """
    corners = [
        to_sheet(placement, x, y)
        for x in (box.left, box.right)
        for y in (box.top, box.bottom)
    ]
    return Box(
        min(x for x, _ in corners),
        min(y for _, y in corners),
        max(x for x, _ in corners),
        max(y for _, y in corners),
    )


def _need(node: SList, head: str) -> SList:
    found = node.first(head)
    if found is None:
        raise ValueError(f"a {node.head} has no ({head} ...)")
    return found


def _num(node: SList, index: int) -> float:
    item = node.items[index] if index < len(node.items) else None
    if not isinstance(item, Atom):
        raise ValueError(f"a {node.head} is missing a number")
    return float(item.raw)


def _xy(node: SList, head: str) -> tuple[float, float]:
    point = _need(node, head)
    return (_num(point, 1), _num(point, 2))


def _arc_extent(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float]]:
    """Points whose box is the box of the arc from start through mid to end."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    twice_area = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(twice_area) < 1e-9:
        # The three points are in a line: KiCad draws the chord.
        return [start, mid, end]
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / twice_area
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / twice_area
    radius = math.hypot(ax - ux, ay - uy)

    def angle(x: float, y: float) -> float:
        return math.atan2(y - uy, x - ux) % math.tau

    a0, a1, a2 = angle(ax, ay), angle(bx, by), angle(cx, cy)
    # The arc runs from a0 to a2 in whichever sense passes a1.
    forward = (a1 - a0) % math.tau <= (a2 - a0) % math.tau
    sweep = (a2 - a0) % math.tau if forward else (a0 - a2) % math.tau
    first = a0 if forward else a2
    points = [start, mid, end]
    for quarter in range(4):
        axis = quarter * math.pi / 2
        if (axis - first) % math.tau <= sweep:
            points.append(
                (
                    round(ux + radius * math.cos(axis), 4),
                    round(uy + radius * math.sin(axis), 4),
                )
            )
    return points
