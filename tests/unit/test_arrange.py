"""Placement for a wired drawing: short straight wires, upright grounds, packing."""

from __future__ import annotations

from itertools import pairwise

from kairo.kicad.arrange import (
    Piece,
    PinEnd,
    Shape,
    arrange_block,
    bends,
    pack,
    structure,
)
from kairo.kicad.geometry import Placement, Rotation, direction

TURNS: tuple[Rotation, ...] = (0, 90, 180, 270)


def two_pin(
    ref: str, top: str, bottom: str, ground: bool = False, supply: bool = False
) -> Piece:
    """A resistor or capacitor as KiCad draws one: upright, pin 1 on top.

    With ``ground``, its bottom pin ends in a ground symbol, which hangs
    below its pin; with ``supply``, its top pin ends in a supply symbol,
    which stands above.
    """
    shapes: list[Shape] = []
    for turn in TURNS:
        placement = Placement(0.0, 0.0, turn)
        pins: list[PinEnd] = []
        for net, (x, y) in ((top, (0, -3)), (bottom, (0, 3))):
            dx, dy = direction(placement, x, y)
            at = (round(dx), round(dy))
            out = (int(at[0] / 3), int(at[1] / 3))
            points = (0, 1) if ground and net == bottom else None
            if supply and net == top:
                points = (0, -1)
            pins.append(PinEnd(net, at, out, points))
        wide = turn in (90, 270)
        room = (-3, -1, 3, 1) if wide else (-1, -3, 1, 3)
        shapes.append(Shape(turn, room, tuple(pins)))
    return Piece(ref, tuple(shapes))


End = tuple[tuple[int, int], tuple[int, int]]


def ends(
    block_spots: dict[str, tuple[int, int, int]], pieces: list[Piece]
) -> dict[tuple[str, str], End]:
    found: dict[tuple[str, str], End] = {}
    for piece in pieces:
        x, y, s = block_spots[piece.ref]
        for pin in piece.shapes[s].pins:
            found[(piece.ref, pin.net)] = ((x + pin.at[0], y + pin.at[1]), pin.out)
    return found


def spots_of(pieces: list[Piece]) -> dict[str, tuple[int, int, int]]:
    block = arrange_block(pieces)
    return {ref: (s.x, s.y, s.shape) for ref, s in block.spots.items()}


def test_bends_are_counted_as_a_router_would_draw_them() -> None:
    # Facing each other on one line: straight.
    assert bends((0, 0), (1, 0), (5, 0), (-1, 0)) == 0
    # Facing each other off one line: a step, two bends.
    assert bends((0, 0), (1, 0), (5, 3), (-1, 0)) == 2
    # An L whose corner lies ahead of both pins.
    assert bends((0, 0), (1, 0), (5, 4), (0, -1)) == 1
    # Back to back: the wire must turn round.
    assert bends((0, 0), (-1, 0), (5, 0), (1, 0)) == 4


def test_two_parts_in_series_meet_on_one_straight_wire() -> None:
    pieces = [two_pin("R1", "A", "MID"), two_pin("R2", "MID", "B")]
    spots = spots_of(pieces)
    found = ends(spots, pieces)
    (p, o), (q, r) = found[("R1", "MID")], found[("R2", "MID")]
    assert bends(p, o, q, r) == 0
    assert abs(p[0] - q[0]) + abs(p[1] - q[1]) <= 6


def test_a_capacitor_to_ground_stands_with_its_ground_below() -> None:
    pieces = [
        two_pin("R1", "IN", "OUT"),
        two_pin("C1", "OUT", "GND", ground=True),
    ]
    spots = spots_of(pieces)
    shape = pieces[1].shapes[spots["C1"][2]]
    ground = next(pin for pin in shape.pins if pin.net == "GND")
    assert ground.out == (0, 1)


def test_no_two_parts_overlap() -> None:
    pieces = [two_pin(f"R{n}", f"N{n}", f"N{n + 1}") for n in range(12)]
    pieces += [two_pin(f"C{n}", f"N{n}", "GND", ground=True) for n in range(12)]
    block = arrange_block(pieces)
    taken: set[tuple[int, int]] = set()
    for piece in pieces:
        spot = block.spots[piece.ref]
        left, top, right, bottom = piece.shapes[spot.shape].room
        cells = {
            (x, y)
            for x in range(spot.x + left, spot.x + right + 1)
            for y in range(spot.y + top, spot.y + bottom + 1)
        }
        assert not taken & cells, piece.ref
        taken |= cells


def test_blocks_built_alike_are_drawn_alike_whatever_their_names() -> None:
    first = [two_pin("R1", "CELL1", "VC1"), two_pin("C1", "VC1", "VC0")]
    second = [two_pin("R7", "CELL7", "VC7"), two_pin("C7", "VC7", "VC6")]
    assert structure(first) == structure(second)
    a, b = spots_of(first), spots_of(second)
    assert [a["R1"], a["C1"]] == [b["R7"], b["C7"]]


def test_the_same_block_is_always_arranged_the_same_way() -> None:
    pieces = [two_pin(f"R{n}", f"N{n}", f"N{n + 1}") for n in range(6)]
    assert spots_of(pieces) == spots_of(pieces)


def test_blocks_pack_in_reading_order_without_overlap() -> None:
    sizes = [(40.0, 30.0), (40.0, 10.0), (40.0, 20.0), (60.0, 10.0)]
    corners = pack(sizes, 100.0)
    # The first two share the top row; the third goes under the shorter.
    assert corners[0] == (0.0, 0.0)
    assert corners[1] == (40.0, 0.0)
    assert corners[2] == (40.0, 10.0)
    boxes = [
        (x, y, x + w, y + h) for (x, y), (w, h) in zip(corners, sizes, strict=True)
    ]
    for i, a in enumerate(boxes):
        assert a[2] <= 100.0
        for b in boxes[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def in_a_row(spots: list[tuple[int, int, int]]) -> bool:
    """Whether spots share one shape and lie on one line at one pitch."""
    if len({s for _, _, s in spots}) != 1:
        return False
    steps = {(b[0] - a[0], b[1] - a[1]) for a, b in pairwise(spots)}
    if len(steps) != 1:
        return False
    dx, dy = steps.pop()
    return (dx == 0) != (dy == 0)


def test_parts_in_parallel_are_drawn_in_a_row() -> None:
    pieces = [two_pin(f"R{n}", "A", "B") for n in range(4)]
    pieces += [two_pin("R9", "IN", "A"), two_pin("R8", "B", "OUT")]
    spots = spots_of(pieces)
    assert in_a_row([spots[f"R{n}"] for n in range(4)])


def test_each_part_of_a_bank_keeps_its_own_part_beside_it_alike() -> None:
    # Four FETs in parallel, each with its own gate resistor from one drive.
    fets = [two_pin(f"Q{n}", "DRAIN", f"G{n}") for n in range(4)]
    gates = [two_pin(f"R{n}", f"G{n}", "DRIVE") for n in range(4)]
    pieces = [*fets, *gates, two_pin("R9", "DRIVE", "IN")]
    spots = spots_of(pieces)
    assert in_a_row([spots[f"Q{n}"] for n in range(4)])
    offsets = {
        (spots[f"R{n}"][0] - spots[f"Q{n}"][0], spots[f"R{n}"][1] - spots[f"Q{n}"][1])
        for n in range(4)
    }
    assert len(offsets) == 1
    found = ends(spots, pieces)
    for n in range(4):
        (p, o), (q, r) = found[(f"Q{n}", f"G{n}")], found[(f"R{n}", f"G{n}")]
        assert bends(p, o, q, r) == 0


def test_parts_each_on_a_pin_of_one_chip_are_no_bank() -> None:
    # Each capacitor's net is its own, but all lead to the same chip, so
    # each belongs beside its pin. The pins face three ways, which no row
    # of capacitors could sit beside.
    sides = (((-3, 0), (-1, 0)), ((3, 0), (1, 0)), ((0, 7), (0, 1)))
    pins = tuple(PinEnd(f"P{n}", at, out) for n, (at, out) in enumerate(sides))
    chip = Piece("U1", (Shape(0, (-2, -6, 2, 6), pins),))
    caps = [two_pin(f"C{n}", f"P{n}", "GND", ground=True) for n in range(3)]
    pieces = [chip, *caps]
    found = ends(spots_of(pieces), pieces)
    for n in range(3):
        (p, _), (q, _) = found[("U1", f"P{n}")], found[(f"C{n}", f"P{n}")]
        assert abs(p[0] - q[0]) + abs(p[1] - q[1]) <= 6, n


def test_a_bank_runs_across_the_way_its_shared_pins_face() -> None:
    # Clamps from a supply to ground, both ends power symbols, so no wire
    # says which way the row should run: their pins face up and down, so
    # they stand side by side.
    pieces = [
        two_pin(f"D{n}", "RAIL", "GND", ground=True, supply=True) for n in range(8)
    ]
    spots = spots_of(pieces)
    assert in_a_row([spots[f"D{n}"] for n in range(8)])
    assert len({spots[f"D{n}"][1] for n in range(8)}) == 1
