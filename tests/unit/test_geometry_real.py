"""The pin transform, measured on the real boards.

Every placed symbol in the sheets saved by the KiCad 9 and 10 programs is
run through `to_sheet`, using the symbol copy embedded in its own sheet,
and a pin counts as a hit when a wire end, label, junction or no-connect
mark sits exactly on its end point. Not every pin is connected, so the
hit rate is below 100 percent; what the test holds is that it is high for
each of the eight placements KiCad writes, and that the other ways of
ordering the same steps do worse where they differ.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from kairo.kicad.geometry import PLACEMENTS, Mirror, Placement, Rotation, to_sheet
from kairo.kicad.sexpr import Atom, SList, parse

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
UNIT = re.compile(r"_(\d+)_(\d+)$")
VERSION = re.compile(rb"\(version (\d+)\)")


def number(node: SList, index: int) -> float:
    item = node.items[index]
    assert isinstance(item, Atom)
    return float(item.raw)


def text(node: SList, index: int) -> str:
    item = node.items[index]
    assert isinstance(item, Atom)
    return item.value


def child(node: SList, head: str) -> SList:
    found = node.first(head)
    assert found is not None, head
    return found


def key(x: float, y: float) -> tuple[int, int]:
    return (round(x * 10000), round(y * 10000))


def wrong_order(placement: Placement, x: float, y: float) -> tuple[float, float]:
    """Mirror first and then turn: the order the measurement rules out."""
    dx, dy = x, -y
    if placement.mirror == "x":
        dy = -dy
    elif placement.mirror == "y":
        dx = -dx
    turned = to_sheet(Placement(0, 0, placement.rotation), dx, -dy)
    return (round(placement.x + turned[0], 4), round(placement.y + turned[1], 4))


def test_pins_land_on_connections_in_all_eight_placements() -> None:
    hits: Counter[tuple[int, str]] = Counter()
    wrong: Counter[tuple[int, str]] = Counter()
    totals: Counter[tuple[int, str]] = Counter()
    for sheet in sorted(REAL.rglob("*.kicad_sch")):
        data = sheet.read_bytes()
        found = VERSION.search(data[:200])
        if found is None or int(found.group(1)) < 20250114:
            continue
        root = parse(data.decode("utf-8")).root
        points: set[tuple[int, int]] = set()
        for node in root.lists():
            if node.head in ("wire", "bus"):
                for xy in child(node, "pts").lists("xy"):
                    points.add(key(number(xy, 1), number(xy, 2)))
            elif node.head in (
                "label",
                "global_label",
                "hierarchical_label",
                "junction",
                "no_connect",
            ):
                at = child(node, "at")
                points.add(key(number(at, 1), number(at, 2)))
        libraries = root.first("lib_symbols")
        pins_of: dict[str, list[tuple[int, int, float, float]]] = {}
        for embedded in libraries.lists("symbol") if libraries else []:
            pins: list[tuple[int, int, float, float]] = []
            for unit in embedded.lists("symbol"):
                suffix = UNIT.search(text(unit, 1))
                assert suffix is not None
                for pin in unit.lists("pin"):
                    at = child(pin, "at")
                    pins.append(
                        (
                            int(suffix.group(1)),
                            int(suffix.group(2)),
                            number(at, 1),
                            number(at, 2),
                        )
                    )
            pins_of[text(embedded, 1)] = pins
        for instance in root.lists("symbol"):
            name = instance.first("lib_name") or child(instance, "lib_id")
            known = pins_of.get(text(name, 1))
            if known is None:
                continue
            at = child(instance, "at")
            mirror_node = instance.first("mirror")
            mirror = text(mirror_node, 1) if mirror_node else ""
            rotation = int(number(at, 3))
            assert (rotation, mirror) in PLACEMENTS, (sheet.name, rotation, mirror)
            placement = Placement(
                number(at, 1),
                number(at, 2),
                rotation,  # pyright: ignore[reportArgumentType]
                mirror,  # pyright: ignore[reportArgumentType]
            )
            unit_number = int(number(child(instance, "unit"), 1))
            style_node = instance.first("body_style") or instance.first("convert")
            style = int(number(style_node, 1)) if style_node else 1
            for unit, pin_style, x, y in known:
                if unit not in (0, unit_number) or pin_style not in (0, style):
                    continue
                totals[(rotation, mirror)] += 1
                if key(*to_sheet(placement, x, y)) in points:
                    hits[(rotation, mirror)] += 1
                if key(*wrong_order(placement, x, y)) in points:
                    wrong[(rotation, mirror)] += 1

    expected: tuple[tuple[Rotation, Mirror], ...] = PLACEMENTS
    for placement_kind in expected:
        # Every placement KiCad writes is in the fixtures, in number.
        assert totals[placement_kind] >= 250, placement_kind
        assert hits[placement_kind] / totals[placement_kind] > 0.75, placement_kind
    assert sum(totals.values()) > 22000
    # Where the order of turning and mirroring matters, only one order fits.
    for placement_kind in ((90, "x"), (270, "x")):
        assert hits[placement_kind] > 1.5 * wrong[placement_kind]
    assert hits[(90, "x")] / totals[(90, "x")] > 0.9
