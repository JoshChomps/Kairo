"""Axis-aligned boxes, the only shape the text placer needs.

Schematic symbols, footprint courtyards and text all reduce to rectangles
whose sides run along the x and y axes, which keeps every overlap and
distance test exact and cheap. Units are millimetres, with y pointing up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Box:
    """A rectangle with sides parallel to the axes.

    Attributes:
        x0: Left edge, in mm.
        y0: Bottom edge, in mm.
        x1: Right edge, in mm. Must not be less than ``x0``.
        y1: Top edge, in mm. Must not be less than ``y0``.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        """Reject boxes whose edges are the wrong way round.

        Raises:
            ValueError: If ``x1 < x0`` or ``y1 < y0``.
        """
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError(f"inverted box: {self}")

    @classmethod
    def centred(cls, cx: float, cy: float, width: float, height: float) -> Box:
        """Build a box from its centre and size.

        Args:
            cx: Centre x, in mm.
            cy: Centre y, in mm.
            width: Width, in mm.
            height: Height, in mm.

        Returns:
            The box.
        """
        return cls(cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)

    @property
    def width(self) -> float:
        """Width in mm."""
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        """Height in mm."""
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        """Centre x in mm."""
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        """Centre y in mm."""
        return (self.y0 + self.y1) / 2

    def overlaps(self, other: Box) -> bool:
        """Say whether two boxes share any area.

        Boxes that only touch along an edge do not overlap; the clearance rule
        is what keeps them apart.

        Args:
            other: The other box.

        Returns:
            True if the interiors intersect.
        """
        return (
            self.x0 < other.x1
            and other.x0 < self.x1
            and self.y0 < other.y1
            and other.y0 < self.y1
        )

    def gap(self, other: Box) -> float:
        """Shortest distance between two boxes.

        Args:
            other: The other box.

        Returns:
            The distance in mm, or 0 if they touch or overlap.
        """
        dx = max(other.x0 - self.x1, self.x0 - other.x1, 0.0)
        dy = max(other.y0 - self.y1, self.y0 - other.y1, 0.0)
        return math.hypot(dx, dy)

    def contains(self, other: Box) -> bool:
        """Say whether another box lies entirely inside this one.

        Args:
            other: The box that should be inside.

        Returns:
            True if every edge of ``other`` is within this box.
        """
        return (
            self.x0 <= other.x0
            and other.x1 <= self.x1
            and self.y0 <= other.y0
            and other.y1 <= self.y1
        )

    def translated(self, dx: float, dy: float) -> Box:
        """Return the same box moved by an offset.

        Args:
            dx: Shift in x, in mm.
            dy: Shift in y, in mm.

        Returns:
            The moved box.
        """
        return Box(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)
