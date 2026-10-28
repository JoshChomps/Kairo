"""Where parts go on a drawing a person reads.

A labelled sheet may put parts anywhere: their names join them. A wired
one reads well only when parts that share a net sit close together with
the pins that meet facing each other, so the wire between them is short
and straight, and a capacitor to ground stands upright with its ground
below. This module chooses each part's position and turn inside its
block to that end by simulated annealing (Kirkpatrick, Gelatt and
Vecchi, "Optimization by Simulated Annealing", Science 220, 1983), the
method long used to place cells on a chip (Sechen and Sangiovanni-
Vincentelli, "The TimberWolf Placement and Routing Package", IEEE JSSC
20, 1985), and then packs the blocks onto the sheet in reading order,
each where it sits highest (the skyline method of Burke, Kendall and
Whitwell, "A New Placement Heuristic for the Orthogonal Stock-Cutting
Problem", Operations Research 52, 2004).

The cost of an arrangement, in grid steps, is the sum of:

- each wired net's estimated length: for two pins, the distance between
  their ends and the bends the way they face forces, counted as the
  router counts them; for more, the half-perimeter of their box and a
  bend for each pin facing away from the others;
- each power pin facing away from where its symbol points: a ground
  hangs below its pin and a supply stands above, so a pin facing up
  draws its ground back over its own part;
- each crossing of two nets' estimated wires, as the router charges
  for one: a net's wire is taken as an L from each pin to the first
  pin, or for a net of more pins to their middle, leaving each pin the
  way it faces;
- a light pull toward the block's middle, so a block stays compact.

Overlap is not a cost but forbidden: each part keeps the room its body,
stubs and symbols need, and a gap around it for wires.

Parts in parallel, such as a switch's FETs, are placed as one piece: a
row on the nets they share, each with its own gate resistor or the like
beside it in the same place (`_banked`).

Every random choice comes from a generator seeded with the block's
structure, its parts' shapes and how their pins share nets, and not with
any name. So the same design is always drawn the same way, and blocks
built alike, such as one channel repeated for each cell of a battery,
are drawn alike.
"""

from __future__ import annotations

import math
import random
from array import array
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType

from kairo.kicad.geometry import Rotation

try:
    from kairo import _fast as _compiled
except ImportError:  # built only where Rust is installed (the "fast" group)
    _compiled = None

#: The compiled pricing of moves, or None to price them in Python. Tests
#: set it to None to compare the two.
_FAST: ModuleType | None = _compiled

Step = tuple[int, int]
Cell = tuple[int, int]

#: What a bend costs beyond its length, in grid steps, as the router counts it.
BEND = 4
#: What one crossing of two nets' wires costs, in grid steps, as the router
#: charges for one.
CROSSING = 6
#: A power pin turned sideways from where its symbol points.
POWER_SIDEWAYS = 6
#: A power pin turned right round, its symbol drawn back over its part.
POWER_BACKWARDS = 24
#: How strongly a part is drawn toward its block's middle, per grid step:
#: lightly if wires already hold it near what it connects to, firmly if
#: nothing does, so it does not drift away from its block.
GRAVITY = 0.03
GRAVITY_UNWIRED = 0.3
#: Moves tried for each part of a block, at an effort of 1.
MOVES_PER_PART = 4000
#: Free spots tried for each of a part's turns when it is first placed.
FIRST_SPOTS = 40
#: Empty grid steps kept around every part's room, for wires to pass.
PAD = 1
#: Passes of single grid steps toward the middle after the anneal, at most.
COMPACT_PASSES = 200
# A heat at which only moves that cost nothing or less are kept.
_COLD = 1e-9


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


@dataclass(frozen=True, slots=True)
class PinEnd:
    """One pin of a part placed one way.

    Attributes:
        net: Its net.
        at: Its end, from the part's origin, in grid steps on the sheet.
        out: The step leading away from the part there.
        points: For a pin that ends in a power symbol, the way the symbol
            points: ``(0, 1)`` for a ground hanging below, ``(0, -1)`` for
            a supply standing above. ``None`` for a pin joined by wire.
    """

    net: str
    at: Cell
    out: Step
    points: Step | None = None


@dataclass(frozen=True, slots=True)
class Shape:
    """One way to place a part: its turn, its room and its pins.

    Attributes:
        rotation: The turn, as a placement gives it.
        room: The cells it needs about its origin, as left, top, right and
            bottom in grid steps, all included.
        pins: Its pins placed this way.
    """

    rotation: Rotation
    room: tuple[int, int, int, int]
    pins: tuple[PinEnd, ...]


@dataclass(frozen=True, slots=True)
class Piece:
    """A part to place, with every way it may be placed; the first is upright."""

    ref: str
    shapes: tuple[Shape, ...]


@dataclass(frozen=True, slots=True)
class Spot:
    """Where a part went: its origin in grid steps and which of its shapes."""

    x: int
    y: int
    shape: int


@dataclass(frozen=True, slots=True)
class Block:
    """A block arranged on its own, with its corner at the origin.

    Attributes:
        spots: Each part's spot.
        width: Its width, in grid steps, with the gap around its parts.
        height: Its height, likewise.
    """

    spots: dict[str, Spot]
    width: int
    height: int


def arrange_block(
    pieces: Sequence[Piece], effort: float = 1.0, pad: int = PAD
) -> Block:
    """Place one block's parts for short, straight wires.

    Args:
        pieces: The block's parts, in the design's order.
        effort: A multiplier on the number of moves tried.
        pad: Empty grid steps kept around every part.

    Returns:
        The parts' spots, moved so the block's corner is at the origin.
    """
    if not pieces:
        return Block({}, 0, 0)
    units, layouts = _banked(pieces, pad)
    state = _State(units, pad)
    state.start()
    rng = random.Random(structure(units))
    state.anneal(rng, max(1, round(MOVES_PER_PART * effort * len(pieces))))
    state.compact(rng)
    block = state.result()
    spots: dict[str, Spot] = {}
    for ref, spot in block.spots.items():
        if ref not in layouts:
            spots[ref] = spot
            continue
        for part, dx, dy, shape in layouts[ref][spot.shape]:
            spots[part] = Spot(spot.x + dx, spot.y + dy, shape)
    return Block(spots, block.width, block.height)


# Where each part of a bank sits within one way of placing the whole bank:
# the part, its offset from the bank's origin and which of its own shapes.
_Layout = tuple[tuple[str, int, int, int], ...]


def _banked(
    pieces: Sequence[Piece], pad: int
) -> tuple[list[Piece], dict[str, tuple[_Layout, ...]]]:
    """A block's parts with each bank of parallel parts made one piece.

    A bank is two or more parts drawn alike whose pins, position by
    position, either all meet one net or each meet a net of their own, with
    at least one net met by all: the FETs of a switch in parallel, their
    drains on one net, their sources on another and each gate on its own.
    A person draws them as a row, side by side on the nets they share, so
    the bank is placed as one piece in that shape, its members at a fixed
    pitch and each one's follower (the part on the far end of its own net,
    such as its gate resistor) in the same place beside it.

    A net is shared when another part drawn the same way meets it on the
    same pin, and is a part's own otherwise when at most one other part
    meets it: that part is the member's follower.
    The followers of a bank must be distinct parts drawn alike, on the same
    pin of each member; a bank whose own nets lead to one shared part, such
    as capacitors each on a different pin of one chip, is no bank, since
    each belongs beside its pin. A bank with followers on more than one of
    its pins is left to the annealing.

    Returns:
        The pieces to arrange, a bank's piece standing first where its
        first member stood, and for each bank's piece, by its shapes, where
        its parts go.
    """
    on_net: dict[str, list[int]] = {}
    for i, piece in enumerate(pieces):
        for pin in piece.shapes[0].pins:
            if i not in on_net.setdefault(pin.net, []):
                on_net[pin.net].append(i)
    looks = [_look(piece) for piece in pieces]
    # How many parts drawn alike meet each net on the same pin.
    alike: dict[tuple[tuple[object, ...], int, str], int] = {}
    for i, piece in enumerate(pieces):
        for p, pin in enumerate(piece.shapes[0].pins):
            key = (looks[i], p, pin.net)
            alike[key] = alike.get(key, 0) + 1

    def own(i: int, net: str) -> int | None:
        """The one other part on a net of a part's own, -1 for none, else None."""
        others = [j for j in on_net[net] if j != i]
        if len(others) > 1:
            return None
        return others[0] if others else -1

    groups: dict[tuple[tuple[object, ...], tuple[str, ...]], list[int]] = {}
    for i, piece in enumerate(pieces):
        key = tuple(
            "*"
            if alike[(looks[i], p, pin.net)] == 1 and own(i, pin.net) is not None
            else pin.net
            for p, pin in enumerate(piece.shapes[0].pins)
        )
        if any(k != "*" for k in key):
            groups.setdefault((looks[i], key), []).append(i)

    used: set[int] = set()
    banks: list[tuple[list[int], list[int], tuple[int, int], tuple[int, ...]]] = []
    for (_, key), members in groups.items():
        if len(members) < 2 or used & set(members):
            continue
        followers: list[int] = []
        link = (-1, -1)
        ok = True
        for p, word in enumerate(key):
            if word != "*":
                continue
            found = [own(i, pieces[i].shapes[0].pins[p].net) for i in members]
            if all(f == -1 for f in found):
                continue
            chosen = [f for f in found if f is not None and f >= 0]
            # Each follower's one pin on its member's own net, the same pin
            # on every follower.
            theirs = {
                tuple(
                    q
                    for q, pin in enumerate(pieces[f].shapes[0].pins)
                    if pin.net == pieces[i].shapes[0].pins[p].net
                )
                for i, f in zip(members, chosen, strict=False)
            }
            if (
                link[0] >= 0
                or len(chosen) != len(members)
                or len(theirs) != 1
                or len(next(iter(theirs))) != 1
                or len(set(chosen)) != len(chosen)
                or len({looks[f] for f in chosen}) != 1
                or used & set(chosen)
                or set(chosen) & set(members)
            ):
                ok = False
                break
            followers, link = chosen, (p, next(iter(theirs))[0])
        if not ok:
            continue
        used |= set(members) | set(followers)
        shared = tuple(p for p, word in enumerate(key) if word != "*")
        banks.append((members, followers, link, shared))

    units: list[Piece] = []
    layouts: dict[str, tuple[_Layout, ...]] = {}
    lead = {bank[0][0]: n for n, bank in enumerate(banks)}
    for i, piece in enumerate(pieces):
        if i in lead:
            members, followers, link, shared = banks[lead[i]]
            bank, layout = _row(
                [pieces[m] for m in members],
                [pieces[f] for f in followers],
                link,
                shared,
                pad,
            )
            units.append(bank)
            layouts[bank.ref] = layout
        elif i not in used:
            units.append(piece)
    return units, layouts


def _look(piece: Piece) -> tuple[object, ...]:
    """How a part is drawn, its nets and its room aside."""
    return tuple(
        (shape.rotation, tuple((p.at, p.out, p.points is None) for p in shape.pins))
        for shape in piece.shapes
    )


def _row(
    members: list[Piece],
    followers: list[Piece],
    link: tuple[int, int],
    shared: tuple[int, ...],
    pad: int,
) -> tuple[Piece, tuple[_Layout, ...]]:
    """A bank as one piece: its members in a row, each with its follower beside it.

    Each way of placing the bank is one shape of the members, one of the
    followers whose pin faces the member's pin on their own net, on one
    line as near as the gap around parts allows. The row runs across the
    way the shared pins face, as a person draws parallel parts: side by
    side when those pins face up and down, so one wire above and one below
    join them all, and stacked when they face the sides; both ways when as
    many face each. Members are spaced so the gaps around neighbours do not
    touch.
    A member's room is the largest any member needs that way, so a label's
    room kept on one of them is kept on all.

    Args:
        members: The bank's members, in the design's order.
        followers: Each member's follower, or none.
        link: The member's pin on its own net and the follower's pin on it,
            as positions in their pins.
        shared: The members' pins on nets they all share, as positions.
        pad: Empty grid steps kept around every part.
    """
    shapes: list[Shape] = []
    layouts: list[_Layout] = []
    for s in range(len(members[0].shapes)):
        room = _union(m.shapes[s].room for m in members)
        upright = sum(members[0].shapes[s].pins[p].out[0] == 0 for p in shared)
        sideways = len(shared) - upright
        ways = [
            w for w, n in ((True, upright), (False, sideways)) if n >= len(shared) - n
        ]
        # Where the follower goes for this shape: its shape and offset.
        options: list[tuple[int, int, int] | None] = []
        if followers:
            mine = members[0].shapes[s].pins[link[0]]
            for t in range(len(followers[0].shapes)):
                theirs = followers[0].shapes[t].pins[link[1]]
                if theirs.out != (-mine.out[0], -mine.out[1]):
                    continue
                beside = _union(f.shapes[t].room for f in followers)
                for gap in range(1, 60):
                    dx = mine.at[0] + mine.out[0] * gap - theirs.at[0]
                    dy = mine.at[1] + mine.out[1] * gap - theirs.at[1]
                    if _apart(room, _moved(beside, dx, dy), pad):
                        options.append((t, dx, dy))
                        break
        for option in options or [None]:
            unit = room
            if option is not None:
                t, dx, dy = option
                beside = _union(f.shapes[t].room for f in followers)
                unit = _union([room, _moved(beside, dx, dy)])
            for across in ways:
                if across:
                    pitch = unit[2] - unit[0] + 1 + 2 * pad
                else:
                    pitch = unit[3] - unit[1] + 1 + 2 * pad
                step = (pitch, 0) if across else (0, pitch)
                pins: list[PinEnd] = []
                layout: list[tuple[str, int, int, int]] = []
                for n, member in enumerate(members):
                    ox, oy = n * step[0], n * step[1]
                    layout.append((member.ref, ox, oy, s))
                    pins += [_shifted(p, ox, oy) for p in member.shapes[s].pins]
                    if option is not None:
                        t, dx, dy = option
                        follower = followers[n]
                        layout.append((follower.ref, ox + dx, oy + dy, t))
                        pins += [
                            _shifted(p, ox + dx, oy + dy)
                            for p in follower.shapes[t].pins
                        ]
                last = len(members) - 1
                whole = _union([unit, _moved(unit, last * step[0], last * step[1])])
                shapes.append(Shape(members[0].shapes[s].rotation, whole, tuple(pins)))
                layouts.append(tuple(layout))
    return Piece(f"bank {members[0].ref}", tuple(shapes)), tuple(layouts)


Room = tuple[int, int, int, int]


def _union(rooms: Iterable[Room]) -> Room:
    """The smallest room holding all of some rooms."""
    a, b, c, d = zip(*rooms, strict=True)
    return (min(a), min(b), max(c), max(d))


def _moved(room: Room, dx: int, dy: int) -> Room:
    return (room[0] + dx, room[1] + dy, room[2] + dx, room[3] + dy)


def _apart(a: Room, b: Room, pad: int) -> bool:
    """Whether two rooms, each with its gap, do not touch."""
    return (
        a[2] + pad < b[0] - pad
        or b[2] + pad < a[0] - pad
        or a[3] + pad < b[1] - pad
        or b[3] + pad < a[1] - pad
    )


def _shifted(pin: PinEnd, dx: int, dy: int) -> PinEnd:
    return PinEnd(pin.net, (pin.at[0] + dx, pin.at[1] + dy), pin.out, pin.points)


def pack(
    sizes: Sequence[tuple[float, float]], width: float
) -> list[tuple[float, float]]:
    """Put boxes in a strip of one width, each as high as it can go.

    Boxes are taken in the order given, which is reading order, and each
    goes where its top is highest, leftmost of equals: the skyline method.
    A box wider than the strip starts at its left edge.

    Args:
        sizes: Each box's width and height.
        width: The strip's width.

    Returns:
        Each box's top-left corner, in the order given.
    """
    # The skyline: (left, right, height) runs across the strip, left to right.
    sky: list[tuple[float, float, float]] = [(0.0, max(width, 0.0), 0.0)]
    corners: list[tuple[float, float]] = []
    for w, h in sizes:
        best: tuple[float, float] | None = None
        lefts = sorted({run[0] for run in sky})
        for left in lefts:
            right = left + w
            if right > width + 1e-9 and left > 0:
                continue
            top = max(
                (z for a, b, z in sky if a < right - 1e-9 and b > left + 1e-9),
                default=0.0,
            )
            if best is None or (top, left) < (best[1], best[0]):
                best = (left, top)
        assert best is not None
        left, top = best
        corners.append(best)
        sky = _raise(sky, left, left + w, top + h)
    return corners


def _raise(
    sky: list[tuple[float, float, float]], left: float, right: float, height: float
) -> list[tuple[float, float, float]]:
    """The skyline with one span raised to a height."""
    runs: list[tuple[float, float, float]] = []
    for a, b, z in sky:
        if b <= left or a >= right:
            runs.append((a, b, z))
            continue
        if a < left:
            runs.append((a, left, z))
        if b > right:
            runs.append((right, b, z))
    runs.append((left, right, height))
    runs.sort()
    merged: list[tuple[float, float, float]] = []
    for run in runs:
        if merged and merged[-1][2] == run[2] and abs(merged[-1][1] - run[0]) < 1e-9:
            merged[-1] = (merged[-1][0], run[1], run[2])
        else:
            merged.append(run)
    return merged


def bends(p: Cell, o: Step, q: Cell, r: Step) -> int:
    """How many bends a wire between two pin ends needs, given their facing.

    Straight between two pins facing each other on one line is none; an
    L whose corner lies ahead of both is one; two pins facing each other
    off one line, or facing the same way, are two; anything that must
    turn back is three or four.

    Args:
        p: One pin's end.
        o: The step leading away from its part.
        q: The other pin's end.
        r: The step leading away from its part.
    """
    vx, vy = q[0] - p[0], q[1] - p[1]
    if vx == 0 and vy == 0:
        return 0
    ahead_p = o[0] * vx + o[1] * vy
    ahead_q = -(r[0] * vx + r[1] * vy)
    if o[0] == -r[0] and o[1] == -r[1]:
        if ahead_p > 0 and ahead_q > 0:
            across = o[1] * vx - o[0] * vy
            return 0 if across == 0 else 2
        return 4
    if o == r:
        return 2
    # Facing across each other: an L turns where the two lines meet.
    corner = (q[0], p[1]) if o[1] == 0 else (p[0], q[1])
    to_corner_p = o[0] * (corner[0] - p[0]) + o[1] * (corner[1] - p[1])
    to_corner_q = r[0] * (corner[0] - q[0]) + r[1] * (corner[1] - q[1])
    return 1 if to_corner_p > 0 and to_corner_q > 0 else 3


class _State:
    """A block being arranged: every part's spot, and the cost kept up to date."""

    def __init__(self, pieces: Sequence[Piece], pad: int = PAD) -> None:
        self.pieces = list(pieces)
        self.pad = pad
        n = len(self.pieces)
        # Typed arrays, so the compiled pricing reads the spots in place.
        self.x = array("q", bytes(8 * n))
        self.y = array("q", bytes(8 * n))
        self.shape = array("q", bytes(8 * n))
        self.placed = array("b", bytes(n))
        # Each placed part's room with its gap, as left, top, right, bottom.
        self.rects: dict[int, tuple[int, int, int, int]] = {}
        # Wired nets with two or more pins in the block, and each part's.
        pins_on: dict[str, int] = {}
        for piece in self.pieces:
            for pin in piece.shapes[0].pins:
                if pin.points is None:
                    pins_on[pin.net] = pins_on.get(pin.net, 0) + 1
        # In order of first appearance, not by name, so that blocks built
        # alike number their nets alike.
        self.nets = [net for net, count in pins_on.items() if count >= 2]
        number = {net: k for k, net in enumerate(self.nets)}
        self.members: list[list[int]] = [[] for _ in self.nets]
        self.nets_of: list[list[int]] = []
        for i, piece in enumerate(self.pieces):
            mine = sorted(
                {number[p.net] for p in piece.shapes[0].pins if p.net in number}
            )
            self.nets_of.append(mine)
            for k in mine:
                self.members[k].append(i)
        self.net_cost = [0.0] * len(self.nets)
        # Each net's estimated wire, as horizontal and vertical segments
        # (x0, y0, x1, y1) with x0 <= x1 and y0 <= y1, and their box.
        self.segments: list[list[tuple[int, int, int, int]]] = [[] for _ in self.nets]
        self.boxes: list[tuple[int, int, int, int] | None] = [None] * len(self.nets)
        self.part_cost = [0.0] * n
        self.total = 0.0
        # The compiled pricing keeps its own rooms, costs and wires; the
        # ones above are then left unused, but for the rooms.
        self.core = (
            _FAST.Arrangement(
                [
                    [
                        (
                            shape.room,
                            [
                                (number.get(p.net, -1), p.at, p.out, p.points)
                                for p in shape.pins
                            ],
                        )
                        for shape in piece.shapes
                    ]
                    for piece in self.pieces
                ],
                self.members,
                self.nets_of,
                self.x,
                self.y,
                self.shape,
                self.placed,
                pad,
                BEND,
                CROSSING,
                POWER_SIDEWAYS,
                POWER_BACKWARDS,
                GRAVITY,
                GRAVITY_UNWIRED,
            )
            if _FAST is not None
            else None
        )

    # -- geometry -------------------------------------------------------

    def _rect(self, i: int, x: int, y: int, s: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = self.pieces[i].shapes[s].room
        pad = self.pad
        return (x + left - pad, y + top - pad, x + right + pad, y + bottom + pad)

    def _fits(
        self, i: int, x: int, y: int, s: int, ignore: tuple[int, ...] = ()
    ) -> bool:
        if self.core is not None:
            return self.core.fits(i, x, y, s, ignore)
        left, top, right, bottom = self._rect(i, x, y, s)
        for j, (a, b, c, d) in self.rects.items():
            if j == i or j in ignore:
                continue
            if left <= c and a <= right and top <= d and b <= bottom:
                return False
        return True

    def _take(self, i: int) -> None:
        self.rects[i] = self._rect(i, self.x[i], self.y[i], self.shape[i])
        if self.core is not None:
            self.core.take(i)

    def _free(self, i: int) -> None:
        self.rects.pop(i, None)
        if self.core is not None:
            self.core.free(i)

    # -- cost -----------------------------------------------------------

    def _pins(self, i: int, net: str) -> list[tuple[Cell, Step]]:
        x, y = self.x[i], self.y[i]
        return [
            ((x + p.at[0], y + p.at[1]), p.out)
            for p in self.pieces[i].shapes[self.shape[i]].pins
            if p.net == net and p.points is None
        ]

    def _net(self, k: int) -> float:
        ends: list[tuple[Cell, Step]] = []
        for i in self.members[k]:
            if self.placed[i]:
                ends += self._pins(i, self.nets[k])
        if len(ends) < 2:
            return 0.0
        if len(ends) == 2:
            (p, o), (q, r) = ends
            return abs(p[0] - q[0]) + abs(p[1] - q[1]) + BEND * bends(p, o, q, r)
        xs = [e[0][0] for e in ends]
        ys = [e[0][1] for e in ends]
        cost = float(max(xs) - min(xs) + max(ys) - min(ys))
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        for (px, py), (ox, oy) in ends:
            if ox * (mx - px) + oy * (my - py) < 0:
                cost += BEND
        return cost

    def _part(self, i: int) -> float:
        shape = self.pieces[i].shapes[self.shape[i]]
        cost = 0.0
        for pin in shape.pins:
            if pin.points is None:
                continue
            if pin.out == pin.points:
                continue
            if pin.out == (-pin.points[0], -pin.points[1]):
                cost += POWER_BACKWARDS
            else:
                cost += POWER_SIDEWAYS
        left, top, right, bottom = shape.room
        cx = self.x[i] + (left + right) / 2
        cy = self.y[i] + (top + bottom) / 2
        pull = GRAVITY if self.nets_of[i] else GRAVITY_UNWIRED
        return cost + pull * (abs(cx) + abs(cy))

    def _wire(self, k: int) -> None:
        """Estimate a net's wire as segments, from its pins as now placed."""
        ends: list[tuple[Cell, Step]] = []
        for i in self.members[k]:
            if self.placed[i]:
                ends += self._pins(i, self.nets[k])
        segments: list[tuple[int, int, int, int]] = []
        if len(ends) >= 2:
            if len(ends) == 2:
                hub = ends[1][0]
            else:
                hub = (
                    round(sum(e[0][0] for e in ends) / len(ends)),
                    round(sum(e[0][1] for e in ends) / len(ends)),
                )
            for (px, py), (ox, _) in ends[: 1 if len(ends) == 2 else len(ends)]:
                corner = (hub[0], py) if ox else (px, hub[1])
                for (ax, ay), (bx, by) in (((px, py), corner), (corner, hub)):
                    if (ax, ay) != (bx, by):
                        segments.append(
                            (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
                        )
        self.segments[k] = segments
        self.boxes[k] = (
            (
                min(g[0] for g in segments),
                min(g[1] for g in segments),
                max(g[2] for g in segments),
                max(g[3] for g in segments),
            )
            if segments
            else None
        )

    def _crossings(self, k: int, others: Iterable[int]) -> int:
        """How often net k's estimated wire crosses the others'."""
        box = self.boxes[k]
        if box is None:
            return 0
        count = 0
        for j in others:
            other = self.boxes[j]
            if (
                other is None
                or other[0] > box[2]
                or box[0] > other[2]
                or other[1] > box[3]
                or box[1] > other[3]
            ):
                continue
            for a in self.segments[k]:
                for b in self.segments[j]:
                    count += _cross(a, b)
        return count

    def recount(self, parts: Iterable[int]) -> float:
        """Recompute the costs touching some parts; return the change."""
        before = 0.0
        after = 0.0
        parts = list(parts)
        if self.core is not None:
            change = self.core.recount(parts)
            self.total += change
            return change
        nets = sorted({k for i in parts for k in self.nets_of[i]})
        touched = set(nets)
        rest = [j for j in range(len(self.nets)) if j not in touched]

        def crossings() -> int:
            total = 0
            for n, k in enumerate(nets):
                total += self._crossings(k, rest) + self._crossings(k, nets[n + 1 :])
            return total

        before += CROSSING * crossings()
        for k in nets:
            before += self.net_cost[k]
            self.net_cost[k] = self._net(k)
            after += self.net_cost[k]
            self._wire(k)
        after += CROSSING * crossings()
        for i in parts:
            before += self.part_cost[i]
            self.part_cost[i] = self._part(i)
            after += self.part_cost[i]
        self.total += after - before
        return after - before

    # -- building -------------------------------------------------------

    def start(self) -> None:
        """Place every part once: the largest first, then each beside its neighbours."""
        n = len(self.pieces)

        def area(i: int) -> int:
            left, top, right, bottom = self.pieces[i].shapes[0].room
            return (right - left + 1) * (bottom - top + 1)

        first = max(range(n), key=lambda i: (area(i), -i))
        order = [first]
        seen = {first}
        # Breadth first through shared nets, the design's order breaking ties.
        head = 0
        while len(order) < n:
            if head == len(order):
                rest = next(i for i in range(n) if i not in seen)
                order.append(rest)
                seen.add(rest)
            i = order[head]
            head += 1
            for k in self.nets_of[i]:
                for j in self.members[k]:
                    if j not in seen:
                        seen.add(j)
                        order.append(j)
        for i in order:
            self._first_spot(i)

    def _first_spot(self, i: int) -> None:
        """The free spot, of the cheapest shape, nearest its placed neighbours."""
        targets: list[Cell] = []
        for k in self.nets_of[i]:
            for j in self.members[k]:
                if self.placed[j]:
                    targets += [end for end, _ in self._pins(j, self.nets[k])]
        if targets:
            tx = round(sum(t[0] for t in targets) / len(targets))
            ty = round(sum(t[1] for t in targets) / len(targets))
        else:
            tx = ty = 0
        shared = {self.nets[k] for k in self.nets_of[i]}
        best: tuple[float, int, int, int] | None = None
        for s, shape in enumerate(self.pieces[i].shapes):
            # Centred where this shape's own pins on those nets would land on
            # the neighbours' ends, not where its origin would.
            mine = [p.at for p in shape.pins if p.net in shared and p.points is None]
            cx = tx - (round(sum(m[0] for m in mine) / len(mine)) if mine else 0)
            cy = ty - (round(sum(m[1] for m in mine) / len(mine)) if mine else 0)
            found = 0
            for x, y in _spiral(cx, cy):
                if not self._fits(i, x, y, s):
                    continue
                self.x[i], self.y[i], self.shape[i] = x, y, s
                self.placed[i] = True
                cost = self._local(i)
                self.placed[i] = False
                if best is None or cost < best[0]:
                    best = (cost, x, y, s)
                found += 1
                if found >= FIRST_SPOTS:
                    break
        assert best is not None
        _, self.x[i], self.y[i], self.shape[i] = best
        self.placed[i] = True
        self._take(i)
        self.recount([i])

    def _local(self, i: int) -> float:
        if self.core is not None:
            return self.core.local(i)
        return sum(self._net(k) for k in self.nets_of[i]) + self._part(i)

    def anneal(self, rng: random.Random, moves: int) -> None:
        """Improve the arrangement by moves that are kept if they pay, or by chance."""
        n = len(self.pieces)
        hot, cold = 8.0, 0.01
        cooling = (cold / hot) ** (1 / moves)
        heat = hot
        area = sum((c - a + 1) * (d - b + 1) for a, b, c, d in self.rects.values())
        span = max(4, round(math.sqrt(area) / 2))
        for _ in range(moves):
            heat *= cooling
            kind = rng.random()
            i = rng.randrange(n)
            if kind < 0.2:
                found = self._facing(rng, i)
                if found is not None:
                    self._try(rng, heat, [(i, *found)])
            elif kind < 0.65:
                reach = max(1, round(span * heat / hot))
                x = self.x[i] + rng.randint(-reach, reach)
                y = self.y[i] + rng.randint(-reach, reach)
                self._try(rng, heat, [(i, x, y, self.shape[i])])
            elif kind < 0.85 and len(self.pieces[i].shapes) > 1:
                s = rng.randrange(len(self.pieces[i].shapes))
                if s != self.shape[i]:
                    self._try(rng, heat, [(i, self.x[i], self.y[i], s)])
            elif n > 1:
                j = rng.randrange(n)
                if j != i:
                    self._try(
                        rng,
                        heat,
                        [
                            (i, self.x[j], self.y[j], self.shape[i]),
                            (j, self.x[i], self.y[i], self.shape[j]),
                        ],
                    )

    def compact(self, rng: random.Random, passes: int = COMPACT_PASSES) -> None:
        """Step each wired cluster toward the block's middle while no cost rises.

        The anneal ends cold, when its moves are short, so a cluster held
        by nothing but the light pull to the middle (parts joined to the
        rest of their block only through power symbols) can be left far
        out. A part moved alone would stretch the wires to its cluster, so
        each cluster, the parts its wired nets join, moves as one: a grid
        step at a time, kept only if the arrangement costs no more.
        """
        clusters = self._clusters()
        for _ in range(passes):
            moved = False
            for cluster in clusters:
                cx = sum(self._middle(i)[0] for i in cluster) / len(cluster)
                cy = sum(self._middle(i)[1] for i in cluster) / len(cluster)
                for dx, dy in ((-_sign(cx), 0), (0, -_sign(cy))):
                    if (dx, dy) == (0, 0):
                        continue
                    before = [(self.x[i], self.y[i]) for i in cluster]
                    self._try(
                        rng,
                        _COLD,
                        [
                            (i, self.x[i] + dx, self.y[i] + dy, self.shape[i])
                            for i in cluster
                        ],
                    )
                    moved = moved or [(self.x[i], self.y[i]) for i in cluster] != before
            if not moved:
                return

    def _middle(self, i: int) -> tuple[float, float]:
        left, top, right, bottom = self.pieces[i].shapes[self.shape[i]].room
        return self.x[i] + (left + right) / 2, self.y[i] + (top + bottom) / 2

    def _clusters(self) -> list[list[int]]:
        """The parts joined to one another by wired nets, each group once."""
        seen: set[int] = set()
        clusters: list[list[int]] = []
        for first in range(len(self.pieces)):
            if first in seen:
                continue
            cluster, frontier = [first], [first]
            seen.add(first)
            while frontier:
                i = frontier.pop()
                for k in self.nets_of[i]:
                    for j in self.members[k]:
                        if j not in seen:
                            seen.add(j)
                            cluster.append(j)
                            frontier.append(j)
            clusters.append(cluster)
        return clusters

    def _facing(self, rng: random.Random, i: int) -> tuple[int, int, int] | None:
        """A spot and shape that put one of a part's pins facing a neighbour's.

        A net of the part and another pin on it are chosen at random; the
        part is turned so a pin of its own on that net faces that pin, a
        few steps away on one line, which a straight wire then joins. Two
        such pins need a turn and a move at once, which the other moves,
        one at a time, rarely find.
        """
        if not self.nets_of[i]:
            return None
        k = rng.choice(self.nets_of[i])
        others = [j for j in self.members[k] if j != i]
        if not others:
            return None
        j = rng.choice(others)
        theirs = self._pins(j, self.nets[k])
        if not theirs:
            return None
        (qx, qy), (ox, oy) = rng.choice(theirs)
        shapes = list(range(len(self.pieces[i].shapes)))
        rng.shuffle(shapes)
        for shape in shapes:
            mine = [
                pin
                for pin in self.pieces[i].shapes[shape].pins
                if pin.net == self.nets[k] and pin.out == (-ox, -oy)
            ]
            if mine:
                pin = rng.choice(mine)
                gap = rng.randint(2, 10)
                return (qx + ox * gap - pin.at[0], qy + oy * gap - pin.at[1], shape)
        return None

    def _try(
        self, rng: random.Random, heat: float, moves: list[tuple[int, int, int, int]]
    ) -> None:
        parts = tuple(m[0] for m in moves)
        for i, x, y, s in moves:
            if not self._fits(i, x, y, s, ignore=parts):
                return
        old = [(i, self.x[i], self.y[i], self.shape[i]) for i in parts]
        for i in parts:
            self._free(i)
        for i, x, y, s in moves:
            self.x[i], self.y[i], self.shape[i] = x, y, s
        # Two parts swapped may still overlap each other.
        if len(moves) > 1 and not self._apart(parts):
            self._put(old)
            return
        for i in parts:
            self._take(i)
        change = self.recount(parts)
        if change <= 0 or rng.random() < math.exp(-change / heat):
            return
        for i in parts:
            self._free(i)
        self._put(old)
        self.recount(parts)

    def _apart(self, parts: tuple[int, ...]) -> bool:
        rects = [self._rect(i, self.x[i], self.y[i], self.shape[i]) for i in parts]
        for k, (a, b, c, d) in enumerate(rects):
            for left, top, right, bottom in rects[k + 1 :]:
                if left <= c and a <= right and top <= d and b <= bottom:
                    return False
        return True

    def _put(self, spots: list[tuple[int, int, int, int]]) -> None:
        for i, x, y, s in spots:
            self.x[i], self.y[i], self.shape[i] = x, y, s
        for i, *_ in spots:
            self._take(i)

    def result(self) -> Block:
        """The spots, moved so the block's corner, gap included, is at the origin."""
        left = min(r[0] for r in self.rects.values())
        top = min(r[1] for r in self.rects.values())
        right = max(r[2] for r in self.rects.values())
        bottom = max(r[3] for r in self.rects.values())
        spots = {
            piece.ref: Spot(self.x[i] - left, self.y[i] - top, self.shape[i])
            for i, piece in enumerate(self.pieces)
        }
        return Block(spots, right - left + 1, bottom - top + 1)


def _cross(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    """1 if a horizontal and a vertical segment cross inside both, else 0."""
    if a[1] == a[3] and b[0] == b[2]:
        h, v = a, b
    elif a[0] == a[2] and b[1] == b[3]:
        h, v = b, a
    else:
        return 0
    return int(h[0] < v[0] < h[2] and v[1] < h[1] < v[3])


def structure(pieces: Sequence[Piece]) -> str:
    """A block's structure as text: its parts' shapes, with nets numbered in order.

    Two blocks built alike, whatever their parts and nets are called, give
    the same text.
    """
    numbers: dict[str, int] = {}
    words: list[str] = []
    for piece in pieces:
        for shape in piece.shapes:
            pins: list[str] = []
            for pin in shape.pins:
                k = numbers.setdefault(pin.net, len(numbers))
                pins.append(f"{k}@{pin.at}{pin.out}{pin.points}")
            words.append(f"{shape.rotation}{shape.room}[{' '.join(pins)}]")
        words.append("|")
    return " ".join(words)


def _spiral(x: int, y: int) -> Iterator[Cell]:
    """Cells outward from one, ring by ring."""
    yield (x, y)
    radius = 1
    while True:
        for dx in range(-radius, radius + 1):
            yield (x + dx, y - radius)
            yield (x + dx, y + radius)
        for dy in range(-radius + 1, radius):
            yield (x - radius, y + dy)
            yield (x + radius, y + dy)
        radius += 1


def wire_cost(pieces: Sequence[Piece], spots: Mapping[str, Spot]) -> float:
    """The cost of an arrangement, as the annealing counts it, for comparison."""
    state = _State(pieces)
    for i, piece in enumerate(state.pieces):
        spot = spots[piece.ref]
        state.x[i], state.y[i], state.shape[i] = spot.x, spot.y, spot.shape
        state.placed[i] = True
    state.recount(range(len(state.pieces)))
    return state.total
