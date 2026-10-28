"""The board's shared geometry, on cases worked by hand."""

from __future__ import annotations

import math

import pytest

from kairo.pcb.geometry import (
    edge_distance,
    inside,
    point_box,
    point_segment,
    rotate,
    segment_box,
    segment_polygon,
    segment_segment,
)

SQUARE = ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0))


def test_a_turn_is_counter_clockwise_as_kicad_draws_with_y_down() -> None:
    # A quarter turn takes the library's top (0, -1) to the left (-1, 0).
    x, y = rotate(0.0, -1.0, 90.0)
    assert (x, y) == pytest.approx((-1.0, 0.0))


def test_distances_from_a_point() -> None:
    assert point_segment((0.0, 3.0), (-1.0, 0.0), (1.0, 0.0)) == pytest.approx(3.0)
    # Past the segment's end, to the end point.
    assert point_segment((4.0, 4.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(5.0)
    assert point_box((5.0, 2.0), (0.0, 0.0, 4.0, 4.0)) == pytest.approx(1.0)
    assert point_box((2.0, 2.0), (0.0, 0.0, 4.0, 4.0)) == 0.0


def test_distances_from_a_segment() -> None:
    # Crossing segments touch.
    assert segment_segment((0, 0), (2, 2), (0, 2), (2, 0)) == 0.0
    # Parallel, 1.5 apart.
    assert segment_segment((0, 0), (4, 0), (0, 1.5), (4, 1.5)) == pytest.approx(1.5)
    # Passing a box's corner diagonally: the corner (2, 2) is sqrt(2) from x + y = 2.
    gap = segment_box((0.0, 2.0), (2.0, 0.0), (2.0, 2.0, 3.0, 3.0))
    assert gap == pytest.approx(math.sqrt(2))
    # Running into a box.
    assert segment_box((-1.0, 2.0), (1.0, 2.0), (0.0, 0.0, 4.0, 4.0)) == 0.0


def test_a_polygon_inside_and_its_edge() -> None:
    assert inside((2.0, 2.0), SQUARE) and not inside((5.0, 2.0), SQUARE)
    assert edge_distance((2.0, 1.0), SQUARE) == pytest.approx(1.0)
    assert segment_polygon((5.0, 1.0), (5.0, 3.0), SQUARE) == pytest.approx(1.0)
    assert segment_polygon((5.0, 2.0), (2.0, 2.0), SQUARE) == 0.0
