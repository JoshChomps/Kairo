from __future__ import annotations

import re
from pathlib import Path

import pytest

from kairo.kicad.geometry import Box, Placement, placed_box, symbol_box
from kairo.kicad.sexpr import Atom, SList, parse

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"


def symbol(*shapes: str, unit: str = "S_1_1") -> SList:
    return parse(f'(symbol "S" (symbol "{unit}" {" ".join(shapes)}))').root


def test_a_box_holds_the_drawing_and_the_whole_of_each_pin() -> None:
    box = symbol_box(
        symbol(
            "(rectangle (start -5.08 5.08) (end 5.08 -5.08))",
            '(pin input line (at -7.62 0 0) (length 2.54) (name "A") (number "1"))',
            '(pin input line (at 0 -10.16 90) (length 5.08) (name "B") (number "2"))',
        )
    )
    assert box == Box(-7.62, -10.16, 5.08, 5.08)
    assert (box.width, box.height) == (12.7, 15.24)


def test_circles_lines_and_curves_are_counted() -> None:
    assert symbol_box(symbol("(circle (center 1 2) (radius 3))")) == Box(-2, -1, 4, 5)
    assert symbol_box(symbol("(polyline (pts (xy -1 0) (xy 4 2) (xy 0 -3)))")) == Box(
        -1, -3, 4, 2
    )
    assert symbol_box(
        symbol("(bezier (pts (xy 0 0) (xy 1 5) (xy 2 -5) (xy 3 0)))")
    ) == Box(0, -5, 3, 5)
    assert symbol_box(symbol()) == Box(0, 0, 0, 0)


def test_an_arc_reaches_as_far_as_it_bulges() -> None:
    # A half circle of radius 2 about the origin, over the top: it reaches
    # y = 2 at its middle, which is one of the three points given.
    top = symbol_box(symbol("(arc (start -2 0) (mid 0 2) (end 2 0))"))
    assert top == Box(-2, 0, 2, 2)
    # Three quarters of a circle from (2, 0) round through (-2, 0) to
    # (0, -2), going over the top: it also reaches (0, 2), which is none of
    # the three points.
    wide = symbol_box(symbol("(arc (start 2 0) (mid -2 0) (end 0 -2))"))
    assert wide == Box(-2, -2, 2, 2)
    # The short way between the same ends does not reach the top or left.
    short = symbol_box(symbol("(arc (start 2 0) (mid 1.4142 -1.4142) (end 0 -2))"))
    assert short == Box(0, -2, 2, 0)
    # Three points in a line are a line.
    assert symbol_box(symbol("(arc (start 0 0) (mid 1 1) (end 2 2))")) == Box(
        0, 0, 2, 2
    )


def test_only_the_asked_unit_and_style_are_measured() -> None:
    root = parse(
        '(symbol "S" (symbol "S_0_1" (rectangle (start -1 1) (end 1 -1)))'
        ' (symbol "S_1_1" (circle (center 5 0) (radius 1)))'
        ' (symbol "S_2_1" (circle (center -9 0) (radius 1)))'
        ' (symbol "S_1_2" (circle (center 0 20) (radius 1))))'
    ).root
    assert symbol_box(root, unit=1) == Box(-1, -1, 6, 1)
    assert symbol_box(root, unit=2) == Box(-10, -1, 1, 1)
    # The rectangle is drawn for style 1 only, so style 2 is the circle alone.
    assert symbol_box(root, unit=1, body_style=2) == Box(-1, 19, 1, 21)
    with pytest.raises(ValueError, match="unit"):
        symbol_box(parse('(symbol "S" (symbol "oddly named"))').root)


def test_a_placed_box_follows_the_placement() -> None:
    box = Box(-2.54, -1.27, 5.08, 3.81)
    assert placed_box(Placement(100, 50), box) == Box(97.46, 46.19, 105.08, 51.27)
    assert placed_box(Placement(100, 50, 90), box) == Box(96.19, 44.92, 101.27, 52.54)
    assert placed_box(Placement(100, 50, 0, "y"), box) == Box(
        94.92, 46.19, 102.54, 51.27
    )


def test_boxes_overlap_only_when_they_share_area_or_come_within_the_gap() -> None:
    a = Box(0, 0, 10, 10)
    assert a.overlaps(Box(5, 5, 15, 15))
    assert not a.overlaps(Box(10, 0, 20, 10))
    assert a.overlaps(Box(10, 0, 20, 10), gap=1.27)
    assert not a.overlaps(Box(12, 0, 20, 10), gap=1.27)


def test_every_pin_of_every_real_symbol_is_inside_its_box() -> None:
    # A check that needs no opinion on what the right box is: wherever a
    # symbol is placed, each of its pins must end inside its box.
    unit_name = re.compile(r"_(\d+)_(\d+)$")
    checked = 0
    for sheet in sorted(REAL.rglob("*.kicad_sch")):
        data = sheet.read_bytes()
        if b"(version 2026" not in data[:200]:
            continue
        libraries = parse(data.decode("utf-8")).root.first("lib_symbols")
        assert libraries is not None
        for embedded in libraries.lists("symbol"):
            units = {
                (int(m.group(1)), int(m.group(2)))
                for child in embedded.lists("symbol")
                if isinstance(child.items[1], Atom)
                and (m := unit_name.search(child.items[1].value))
            }
            for unit, style in units:
                box = symbol_box(embedded, unit or 1, style or 1)
                for child in embedded.lists("symbol"):
                    name = child.items[1]
                    assert isinstance(name, Atom)
                    found = unit_name.search(name.value)
                    assert found is not None
                    if (int(found.group(1)), int(found.group(2))) != (unit, style):
                        continue
                    for pin in child.lists("pin"):
                        at = pin.first("at")
                        assert at is not None
                        x, y = (float(v) for v in at.values()[:2])
                        assert box.left <= x <= box.right
                        assert box.top <= y <= box.bottom
                        checked += 1
    assert checked > 1000
