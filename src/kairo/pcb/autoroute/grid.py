"""Grids, shapes and the maps a search reads: what is in the way.

Part of Kairo's router; the method is in the package's docstring.
"""

# The package is one module in layers: its private names are shared.
# pyright: reportUnusedFunction=false

from __future__ import annotations

import math
from array import array
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from types import ModuleType
from typing import TYPE_CHECKING

from kairo.pcb.geometry import (
    Rect,
    point_segment,
    segment_box,
    segment_polygon,
    segment_segment,
)
from kairo.pcb.specctra import NetClass, between

if TYPE_CHECKING:
    from kairo._fast import Grazes, Search

try:
    from kairo import _fast as _compiled
except ImportError:  # built only where Rust is installed (the "fast" group)
    _compiled = None

#: The router's compiled loops, or None to run them in Python. Tests set it
#: to None to compare the two.
_FAST: ModuleType | None = _compiled

#: The eight moves, as (dx, dy) in cells.
_MOVES = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


#: Extra cost of a turn, by the change of direction in eighths of a turn:
#: none, 45 degrees, 90 degrees; more is acute and never taken.
_TURN = (0.0, 0.2, 1.0)


#: A move's length in cells: straight or diagonal.
_STEP = (1.0, math.sqrt(2.0))


class AutorouteError(ValueError):
    """The board cannot be put on the router's grid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """How hard the router works and how it prices things.

    Attributes:
        pitch: The grid's cell, in mm; the board's least track or finer.
        via_cost: A layer change's cost, in cells of track.
        iterations: The most negotiation rounds.
        present_growth: How much dearer a shared cell gets each round.
        history_step: What each round a cell stays shared adds to it.
        margin: How far beyond a connection's pins its search may go, in
            mm, before it is widened to the whole board.
        greed: The heuristic's weight. Above 1 the search is weighted A*
            (Pohl, 1970): a path at most this many times the cheapest,
            found expanding far fewer cells.
        edge: Copper's least distance to the board's edge, in mm.
        edge_band: How far in from that a cell still costs twice as much,
            in mm: a track runs along the edge only where nothing else
            is free, as a designer keeps copper off a board's edge.
        tidy: Pull each detour tight once everything is routed (1), or
            not (0) (`_tidy`).
        slack: Room added to every clearance on the grid, in mm, against
            the rounding of a straightened line between cell centres.
        hole_gap: The least distance between two drill holes, in mm,
            whatever their nets (JLCPCB: 0.2 mm via to via). A pad's hole
            is taken as its copper, which holds it.
        min_track: The narrowest track the maker builds, in mm (JLCPCB,
            2 oz outer copper: 0.15 mm); a track necks down no further
            leaving a fine-pitch pad.
        wrong_way: What a step against its layer's preferred direction
            costs, as a multiple of a step along it. Layers alternate,
            the first across and the next down, so one layer's runs do
            not wall in another's; 1 turns the preference off. Diagonal
            steps are neither.
        complete: Whether to run the completion pass (1) or not (0): each
            net still in pieces tries once more with other nets' routes as
            walls and vias at half price.
        rescue: Try each connection still missing after completion once
            more (1): route it as if other nets were only dear, lift the
            other nets' connections its path crosses, then route those
            again round it with every route a wall; undone unless every
            lifted connection is made again.
        granular: Rip up only a net's contested connections (1), keeping
            the rest of its tree, or the whole net (0), as PathFinder does.
    """

    pitch: float = 0.25
    via_cost: float = 8.0
    iterations: int = 30
    present_growth: float = 1.6
    history_step: float = 0.5
    margin: float = 10.0
    greed: float = 1.3
    edge: float = 0.5
    edge_band: float = 1.5
    tidy: float = 1.0
    slack: float = 0.02
    min_track: float = 0.15
    hole_gap: float = 0.2
    wrong_way: float = 1.5
    complete: float = 1.0
    granular: float = 1.0
    rescue: float = 1.0


@dataclass(slots=True)
class _Net:
    """A net to route.

    ``pieces`` is its copper already on the board, as grid cells, one set
    per piece not yet joined; ``anchors`` holds, for each piece, the cells
    a track may start or end at: a pad's centre, as a designer runs a
    track into the middle of a pad, or anywhere on a track or an island.
    ``escape`` holds each pad's way out (`_escape`), open to this net
    whatever lies near, but not copper: it joins nothing. ``exact`` gives
    the true point for a pad's centre cell and its escape cells: on the
    pad's axis, not the nearest grid centre, which may lie up to half a
    cell off it, too near a fine-pitch neighbour. ``neck`` gives the
    widest track each escape cell allows, where that is narrower than the
    class's: between fine-pitch pins a track necks down, as a designer
    draws it.
    """

    name: str
    klass: int
    pieces: list[set[int]] = field(default_factory=list[set[int]])
    anchors: list[set[int]] = field(default_factory=list[set[int]])
    unreachable: set[int] = field(default_factory=set[int])
    escape: set[int] = field(default_factory=set[int])
    exact: dict[int, tuple[float, float]] = field(
        default_factory=dict[int, tuple[float, float]]
    )
    neck: dict[int, float] = field(default_factory=dict[int, float])


@dataclass(slots=True)
class Grid:
    """The board as cells on each routing layer.

    Attributes:
        left: The board's left edge, in mm.
        top: Its top edge, in mm.
        pitch: A cell's side, in mm.
        nx: Cells across.
        ny: Cells down.
        layers: The routing layers' names, top first.
        scratch: The search's arrays, made once per board.
        via_ring: The cells round a via that another net's routing must
            not claim: a via is wider than a track, so it needs room past
            the track claims (`_footprint`) by its half width less a
            track's, and on every layer, since it passes through them all.
        narrow: The narrowest class's half width, in mm: claims
            (`_footprint`) leave room for a neighbour this wide.
        rings: For each class, the cells round a step it must find free
            of other nets' claims: as far as it is wider, and its
            clearance larger, than the narrowest class's.
        far_of: For each class, the classes it must keep a pair's larger
            spacing from (the voltage between them): checked through
            ``far_use``, not the rings, so a class keeps only its own
            clearance from the rest.
        far_class: For each class some other must keep far from, the class
            whose claim (`_footprint`) reaches that far: its widest pair,
            and the widest track or via that could come near.
        far_use: Those wider claims on each cell, by class.
        far_cells: Each net's far claim, as its class and cells.
        grazes: For each net's number, its class's grazing diagonal steps
            (`_grazes`): a step's key is its cell times 8 plus its move,
            the value the nets whose copper it passes too near.
    """

    left: float
    top: float
    pitch: float
    nx: int
    ny: int
    layers: tuple[str, ...]
    scratch: _Scratch | None = None
    via_ring: tuple[tuple[int, int], ...] = ((0, 0),)
    narrow: float = 0.125
    rings: tuple[tuple[tuple[int, int], ...], ...] = ()
    far_of: tuple[tuple[int, ...], ...] = ()
    far_class: dict[int, NetClass] = field(default_factory=dict[int, NetClass])
    far_use: dict[int, array[int]] = field(default_factory=dict[int, "array[int]"])
    far_cells: dict[str, tuple[int, set[int]]] = field(
        default_factory=dict[str, tuple[int, set[int]]]
    )
    grazes: dict[int, dict[int, tuple[int, ...]]] = field(
        default_factory=dict[int, dict[int, tuple[int, ...]]]
    )

    def cell(self, x: float, y: float) -> tuple[int, int]:
        """The cell holding a point, clamped to the grid."""
        i = min(max(round((x - self.left) / self.pitch), 0), self.nx - 1)
        j = min(max(round((y - self.top) / self.pitch), 0), self.ny - 1)
        return i, j

    def at(self, i: int, j: int) -> tuple[float, float]:
        """A cell's centre, in mm."""
        return (
            round(self.left + i * self.pitch, 4),
            round(self.top + j * self.pitch, 4),
        )

    def index(self, i: int, j: int, layer: int) -> int:
        """One number for a cell on a layer."""
        return (layer * self.ny + j) * self.nx + i

    def split(self, node: int) -> tuple[int, int, int]:
        """A cell number back to its column, row and layer."""
        layer, rest = divmod(node, self.nx * self.ny)
        j, i = divmod(rest, self.nx)
        return i, j, layer


class _Scratch:
    """A search's per-cell arrays, kept between searches.

    A cell's cost, parent and heading are valid only where its stamp is the
    current search's, so nothing is cleared between searches: each search
    costs what it expands, not the board's size.
    """

    __slots__ = (
        "cost",
        "drilled",
        "far",
        "fast",
        "grazes",
        "head",
        "hole_ring",
        "pad_of",
        "parent",
        "ring",
        "run",
        "stamp",
    )

    def __init__(self, size: int) -> None:
        self.cost = [0.0] * size
        self.parent = [0] * size
        self.head = [8] * size
        self.stamp = [0] * size
        self.run = 0
        self.ring: tuple[tuple[int, int], ...] = ((0, 0),)
        self.drilled: set[int] = set()
        self.hole_ring: tuple[tuple[int, int], ...] = ((0, 0),)
        self.pad_of: dict[int, int] = {}
        self.far: list[array[int]] = []
        # The compiled search's own arrays, and each class's grazing steps
        # converted for it, keyed by the table's id (with the table, so a
        # reused id is caught).
        self.fast: Search | None = None
        self.grazes: dict[int, tuple[dict[int, tuple[int, ...]], Grazes]] = {}


@dataclass(frozen=True, slots=True)
class _Shape:
    """Copper of one net: a box, or a capsule (a track) or a disc (a via)."""

    net: str
    layer: str  # a layer's name, or "*" for every layer
    box: tuple[float, float, float, float] | None = None
    start: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    radius: float = 0.0
    polygon: tuple[tuple[float, float], ...] | None = None


def _bounds(shape: _Shape) -> tuple[float, float, float, float]:
    if shape.box is not None:
        return shape.box
    if shape.polygon is not None:
        pxs = [x for x, _ in shape.polygon]
        pys = [y for _, y in shape.polygon]
        return (min(pxs), min(pys), max(pxs), max(pys))
    assert shape.start is not None and shape.end is not None
    r = shape.radius
    return (
        min(shape.start[0], shape.end[0]) - r,
        min(shape.start[1], shape.end[1]) - r,
        max(shape.start[0], shape.end[0]) + r,
        max(shape.start[1], shape.end[1]) + r,
    )


def _distance(a: _Shape, b: _Shape) -> float:
    """The gap between two shapes' copper (0 or less where they touch).

    ``a`` is a track or a via; ``b`` any shape.
    """
    assert a.start is not None and a.end is not None
    if b.box is not None:
        return segment_box(a.start, a.end, b.box) - a.radius
    if b.polygon is not None:
        return segment_polygon(a.start, a.end, b.polygon) - a.radius
    assert b.start is not None and b.end is not None
    return segment_segment(a.start, a.end, b.start, b.end) - a.radius - b.radius


def _cells(grid: Grid, shape: _Shape, grow: float) -> list[tuple[int, int]]:
    """The cells whose centres lie within ``grow`` of a shape's copper.

    A box or a capsule is tested cell by cell over its own bounds. A
    polygon (a filled pour, thousands of corners) is filled row by row
    from its edges' crossings, O(rows x edges), then grown by stamping a
    disc of cells on its boundary cells, rather than measuring every cell
    against every edge.
    """
    if shape.polygon is not None:
        return _polygon_cells(grid, shape.polygon, grow)
    p = grid.pitch
    if shape.box is not None:
        left, top, right, bottom = shape.box
        box = (left - grow, top - grow, right + grow, bottom + grow)
    else:
        assert shape.start is not None and shape.end is not None
        r = shape.radius + grow
        box = (
            min(shape.start[0], shape.end[0]) - r,
            min(shape.start[1], shape.end[1]) - r,
            max(shape.start[0], shape.end[0]) + r,
            max(shape.start[1], shape.end[1]) + r,
        )
    i0 = max(math.ceil((box[0] - grid.left) / p), 0)
    i1 = min(math.floor((box[2] - grid.left) / p), grid.nx - 1)
    j0 = max(math.ceil((box[1] - grid.top) / p), 0)
    j1 = min(math.floor((box[3] - grid.top) / p), grid.ny - 1)
    out: list[tuple[int, int]] = []
    for j in range(j0, j1 + 1):
        y = grid.top + j * p
        for i in range(i0, i1 + 1):
            x = grid.left + i * p
            if shape.box is not None:
                left, top, right, bottom = shape.box
                dx = max(left - x, 0.0, x - right)
                dy = max(top - y, 0.0, y - bottom)
                inside = dx * dx + dy * dy <= grow * grow
            else:
                assert shape.start is not None and shape.end is not None
                inside = point_segment((x, y), shape.start, shape.end) <= (
                    shape.radius + grow
                )
            if inside:
                out.append((i, j))
    return out


def _polygon_cells(
    grid: Grid, polygon: Sequence[tuple[float, float]], grow: float
) -> list[tuple[int, int]]:
    """A polygon's cells by scanline (even-odd), grown by a disc of cells."""
    p = grid.pitch
    edges = list(zip(polygon, [*polygon[1:], polygon[0]], strict=True))
    ys = [y for _, y in polygon]
    j0 = max(math.ceil((min(ys) - grid.top) / p), 0)
    j1 = min(math.floor((max(ys) - grid.top) / p), grid.ny - 1)
    filled: set[tuple[int, int]] = set()
    for j in range(j0, j1 + 1):
        y = grid.top + j * p
        xs = sorted(
            ax + (y - ay) * (bx - ax) / (by - ay)
            for (ax, ay), (bx, by) in edges
            if (ay > y) != (by > y)
        )
        for a, b in zip(xs[::2], xs[1::2], strict=False):
            i0 = max(math.ceil((a - grid.left) / p), 0)
            i1 = min(math.floor((b - grid.left) / p), grid.nx - 1)
            for i in range(i0, i1 + 1):
                filled.add((i, j))
    if grow <= 0 or not filled:
        return sorted(filled)
    # Grow from the boundary only: an inner cell's disc adds nothing new.
    edge = [
        (i, j)
        for i, j in filled
        if not {(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)} <= filled
    ]
    grown = set(filled)
    for di, dj in _within(math.ceil(grow / p) + 1e-9):
        for i, j in edge:
            ii, jj = i + di, j + dj
            if 0 <= ii < grid.nx and 0 <= jj < grid.ny:
                grown.add((ii, jj))
    return sorted(grown)


def _foes(
    grid: Grid,
    shapes: Sequence[_Shape],
    classes: Sequence[NetClass],
    klass: int,
    net_id: Mapping[str, int],
    class_of: Mapping[str, int],
    via: tuple[float, float],
    slack: float = 0.0,
    hole_gap: float = 0.0,
) -> list[array[int]]:
    """For one class, each layer's cells and the net whose grown copper is there.

    0 is free, a net's number is that net's alone, -1 is two nets'. A
    last map, after the layers, is for vias: copper grown by a via's
    radius rather than a track's half width, on any layer. There,
    through copper (a pin's pad, a via) also bars every via, its own net's
    too, within a drill's spacing: holes keep apart whatever their nets.
    """
    own = classes[klass]
    size = grid.nx * grid.ny
    maps = [array("i", bytes(4 * size)) for _ in range(len(grid.layers) + 1)]
    for shape in shapes:
        number = net_id.get(shape.net, -1)
        other = classes[class_of[shape.net]] if shape.net in class_of else own
        gap = between(own, other) + slack
        on = (
            range(len(grid.layers))
            if shape.layer == "*"
            else [grid.layers.index(shape.layer)]
            if shape.layer in grid.layers
            else []
        )
        if not on:
            continue
        steps = [(k, gap + own.width / 2) for k in on]
        steps.append((len(grid.layers), gap + via[0] / 2))
        for layer, grow in steps:
            target = maps[layer]
            for i, j in _cells(grid, shape, grow):
                cell = j * grid.nx + i
                held = target[cell]
                target[cell] = number if held in (0, number) else -1
        if shape.layer == "*" and hole_gap > 0:
            holes = maps[len(grid.layers)]
            for i, j in _cells(grid, shape, via[1] / 2 + hole_gap):
                holes[j * grid.nx + i] = -1
    return maps


def _escape(
    grid: Grid,
    box: tuple[float, float, float, float],
    reach: float,
    room: Callable[[float, float], float],
    floor: float,
) -> list[tuple[int, int, float]]:
    """A pad's way out: the cells on its long axis, out past its ends.

    On a grid, the cells between two fine-pitch pads all lie within a
    neighbour's clearance, so a pad's only way out is straight off its
    end, along its axis, as a designer leaves it. The strip runs from the
    pad's centre up to ``reach`` beyond each end, one cell wide. At each
    cell ``room`` gives the widest half track there, on the pad's true
    axis, that keeps its clearance; the strip stops where that falls below
    ``floor`` (half the narrowest track the maker builds), or at the
    board's edge.

    Returns:
        Each cell, with the half width a track may have there.
    """
    left, top, right, bottom = box
    cx, cy = (left + right) / 2, (top + bottom) / 2
    ci, cj = grid.cell(cx, cy)
    if right - left >= bottom - top:
        ends = (grid.cell(left - reach, cy)[0], grid.cell(right + reach, cy)[0])
        lines = [range(ci, ends[0] - 1, -1), range(ci, ends[1] + 1)]
        along = [[(i, cj) for i in line] for line in lines]
    else:
        ends = (grid.cell(cx, top - reach)[1], grid.cell(cx, bottom + reach)[1])
        lines = [range(cj, ends[0] - 1, -1), range(cj, ends[1] + 1)]
        along = [[(ci, j) for j in line] for line in lines]
    across = right - left >= bottom - top
    strip: list[tuple[int, int, float]] = []
    for line in along:
        for i, j in line:
            if not (0 < i < grid.nx - 1 and 0 < j < grid.ny - 1):
                break
            x, y = grid.at(i, j)
            half = room(x if across else cx, cy if across else y)
            if half < floor:
                break
            strip.append((i, j, half))
    return strip


class _Near:
    """Copper by 1 mm buckets, to measure a point against its neighbours only."""

    def __init__(self, shapes: Sequence[_Shape]) -> None:
        """Index the shapes by the buckets their bounds touch."""
        self.shapes = list(shapes)
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for k, shape in enumerate(self.shapes):
            left, top, right, bottom = _bounds(shape)
            for bx in range(math.floor(left), math.floor(right) + 1):
                for by in range(math.floor(top), math.floor(bottom) + 1):
                    self.buckets.setdefault((bx, by), []).append(k)

    def around(self, bounds: Rect, reach: float) -> list[int]:
        """The shapes whose buckets lie within ``reach`` of a box, once each."""
        left, top, right, bottom = bounds
        found: dict[int, None] = {}
        for bx in range(math.floor(left - reach), math.floor(right + reach) + 1):
            for by in range(math.floor(top - reach), math.floor(bottom + reach) + 1):
                found.update(dict.fromkeys(self.buckets.get((bx, by), ())))
        return list(found)

    def room_for(self, line: _Shape, cap: float, need: Callable[[str], float]) -> float:
        """The widest half track along a line (or at a point) clear of other nets.

        Measured exactly against every other net's copper within reach,
        keeping ``need(that net)`` from it, and never more than ``cap``.
        """
        best = cap
        for k in self.around(_bounds(line), cap + 2.0):
            other = self.shapes[k]
            if other.net == line.net and other.net:
                continue
            if line.layer != "*" and other.layer not in ("*", line.layer):
                continue
            best = min(best, _distance(line, other) - need(other.net))
        return best

    def room(
        self,
        point: tuple[float, float],
        cap: float,
        net: str,
        layer: str,
        need: Callable[[str], float],
    ) -> float:
        """`room_for` at a point."""
        probe = _Shape(net, layer, start=point, end=point, radius=0.0)
        return self.room_for(probe, cap, need)


def _grazes(
    grid: Grid,
    maps: list[array[int]],
    classes: Sequence[NetClass],
    klass: int,
    near: _Near,
    net_id: Mapping[str, int],
    class_of: Mapping[str, int],
    min_track: float,
    open_cells: set[int],
    memo: dict[int, list[tuple[int, float, int]]],
) -> dict[int, tuple[int, ...]]:
    """The diagonal steps a class's tracks may not take past fixed copper.

    Copper is measured at cell centres, but a 45 degree step passes between
    two side cells, up to p/(2 sqrt 2) nearer a pad's corner than either
    centre (router-log rule 13). Each diagonal step that passes a blocked
    side cell is measured once, exactly: if even a track necked to the
    maker's least would come nearer a net's copper than their clearance,
    that net is noted, and the step is barred to every other net. Only
    such steps are barred, so the passages a necked track can take stay
    open. A step no net can stand at both ends of is not measured, and
    each step's distances are measured once for every class (``memo``):
    to each net's copper near it, with that net's clearance.

    Returns:
        Grazing steps, by cell times 8 plus the move (both ways): the
        numbers of the nets they pass too near (-1 for copper of no net).
    """
    own = classes[klass]
    nx, ny = grid.nx, grid.ny
    plane = nx * ny
    half = min_track / 2
    # Copper further than the widest clearance and a least track cannot
    # bar a step.
    reach = max(between(a, b) for a in classes for b in classes) + half
    need_of = [between(own, c) for c in classes]
    out: dict[int, tuple[int, ...]] = {}
    for layer in range(len(grid.layers)):
        foe = maps[layer]
        name = grid.layers[layer]
        base = layer * plane
        for j in range(1, ny - 1):
            for i in range(1, nx - 1):
                cell = j * nx + i
                # Two of the four diagonals from each cell; the others are
                # the same steps taken from the far end.
                held = foe[cell]
                if held == -1 and base + cell not in open_cells:
                    continue
                for d in (1, 3):
                    di, dj = _MOVES[d]
                    if foe[cell + di] == 0 and foe[cell + dj * nx] == 0:
                        continue
                    far = cell + di + dj * nx
                    there = foe[far]
                    if base + far not in open_cells and (
                        there == -1 or (held > 0 and there > 0 and held != there)
                    ):
                        continue
                    key = (base + cell) * 8 + d
                    if key not in memo:
                        start, end = grid.at(i, j), grid.at(i + di, j + dj)
                        line = _Shape("", name, start=start, end=end, radius=0.0)
                        memo[key] = [
                            (
                                net_id.get(other.net, -1),
                                _distance(line, other),
                                class_of.get(other.net, -1),
                            )
                            for other in (
                                near.shapes[k]
                                for k in near.around(_bounds(line), reach)
                            )
                            if other.layer in ("*", name)
                        ]
                    offenders = {
                        number
                        for number, apart, theirs in memo[key]
                        if apart - (need_of[theirs] if theirs >= 0 else own.clearance)
                        < half - 1e-9
                    }
                    if offenders:
                        found = tuple(sorted(offenders))
                        out[(base + cell) * 8 + d] = found
                        out[(base + far) * 8 + (d + 4) % 8] = found
    return out


def _grazes_all(
    grid: Grid,
    maps: Sequence[list[array[int]]],
    classes: Sequence[NetClass],
    near: _Near,
    net_id: Mapping[str, int],
    class_of: Mapping[str, int],
    min_track: float,
    open_cells: set[int],
) -> list[dict[int, tuple[int, ...]]]:
    """Every class's grazing steps (`_grazes`), compiled where it is built.

    The compiled version measures the same steps against the same copper
    with the same distances, each step once for every class.
    """
    if _FAST is None:
        memo: dict[int, list[tuple[int, float, int]]] = {}
        return [
            _grazes(
                grid, maps[k], classes, k, near, net_id, class_of, min_track,
                open_cells, memo,
            )
            for k in range(len(classes))
        ]  # fmt: skip
    layer_of = {name: k for k, name in enumerate(grid.layers)}
    grazer = _FAST.Grazer(
        [
            (
                net_id.get(s.net, -1),
                class_of.get(s.net, -1),
                -1 if s.layer == "*" else layer_of.get(s.layer, -2),
                s.box,
                (s.start, s.end, s.radius)
                if s.start is not None and s.end is not None
                else None,
                s.polygon,
            )
            for s in near.shapes
        ]
    )
    # Cell centres as `Grid.at` rounds them.
    xs = [grid.at(i, 0)[0] for i in range(grid.nx)]
    ys = [grid.at(0, j)[1] for j in range(grid.ny)]
    half = min_track / 2
    reach = max(between(a, b) for a in classes for b in classes) + half
    return [
        grazer.grazes(
            maps[k][: len(grid.layers)], grid.nx, grid.ny, xs, ys, open_cells,
            [between(own, c) for c in classes], own.clearance, half, reach,
        )
        for k, own in enumerate(classes)
    ]  # fmt: skip


def _far_take(
    grid: Grid, name: str, klass: int, runs: list[list[int]], pitch: float, via: float
) -> None:
    """Add a net's far claim, if others must keep far from its class."""
    shape = grid.far_class.get(klass)
    if shape is None:
        return
    cells = _footprint(grid, runs, shape, pitch, via)
    counts = grid.far_use[klass]
    for cell in cells:
        counts[cell] += 1
    grid.far_cells[name] = (klass, cells)


def _far_drop(grid: Grid, name: str) -> None:
    """Take a net's far claim away."""
    entry = grid.far_cells.pop(name, None)
    if entry is None:
        return
    klass, cells = entry
    counts = grid.far_use[klass]
    for cell in cells:
        counts[cell] -= 1


def _far_hit(grid: Grid, klass: int, cell: int) -> bool:
    """Whether a net of a class at a cell is too near a class it must keep far from."""
    if not grid.far_of:
        return False
    return any(grid.far_use[j][cell] for j in grid.far_of[klass])


def _keep_off_edge(
    grid: Grid, maps: list[array[int]], track: float, via: float
) -> None:
    """Block every cell nearer the board's edge than copper may come."""
    for k, reach in enumerate([*([track] * (len(maps) - 1)), via]):
        for cell in _near_edge(grid, reach):
            maps[k][cell] = -1


def _merged(
    pieces: list[tuple[set[int], set[int]]],
) -> list[tuple[set[int], set[int]]]:
    """Pieces that share a cell, joined: copper already touching is one."""
    merged: list[tuple[set[int], set[int]]] = []
    for piece, anchor in pieces:
        piece, anchor = set(piece), set(anchor)
        for m in [m for m in merged if m[0] & piece]:
            merged.remove(m)
            piece |= m[0]
            anchor |= m[1]
        merged.append((piece, anchor))
    return merged


def _centre(shape: _Shape) -> tuple[float, float]:
    if shape.box is not None:
        return ((shape.box[0] + shape.box[2]) / 2, (shape.box[1] + shape.box[3]) / 2)
    if shape.polygon is not None:
        return shape.polygon[0]
    assert shape.start is not None
    return shape.start


def _edge_band(grid: Grid, reach: float) -> array[float]:
    """Each cell's starting history: 1 (twice the price) near the edge."""
    plane = [0.0] * (grid.nx * grid.ny)
    for cell in _near_edge(grid, reach):
        plane[cell] = 1.0
    return array("d", plane * len(grid.layers))


def _near_edge(grid: Grid, reach: float) -> Iterator[int]:
    """The cells of one layer nearer the board's edge than ``reach``, in mm."""
    cells = math.ceil(reach / grid.pitch)
    for j in range(grid.ny):
        near_y = j < cells or j >= grid.ny - cells
        for i in range(grid.nx):
            if near_y or i < cells or i >= grid.nx - cells:
                yield j * grid.nx + i


def _missing(grid: Grid, net: _Net, runs: list[list[int]]) -> int:
    """How many joins a net still lacks, its unreachable pieces aside."""
    nothing: set[int] = set()
    pieces = [
        (set(piece), nothing)
        for k, piece in enumerate(net.pieces)
        if k not in net.unreachable
    ]
    return len(_merged(pieces + [(set(run), nothing) for run in runs])) - 1


def _run_cells(grid: Grid, run: list[int]) -> set[int]:
    """A run's centreline cells and its vias' cells on every layer."""
    track, holes = _centres(grid, [run])
    return track | holes


def _span(grid: Grid, net: _Net) -> float:
    cells = [grid.split(c) for piece in net.pieces for c in list(piece)[:1]]
    return (max(c[0] for c in cells) - min(c[0] for c in cells)) + (
        max(c[1] for c in cells) - min(c[1] for c in cells)
    )


def _centres(grid: Grid, runs: list[list[int]]) -> tuple[set[int], set[int]]:
    """A net's routed centreline cells, and its vias' cells on every layer."""
    plane = grid.nx * grid.ny
    layers = len(grid.layers)
    track: set[int] = set()
    holes: set[int] = set()
    for run in runs:
        track.update(run)
        for a, b in pairwise(run):
            if a // plane != b // plane:
                holes.update(b % plane + k * plane for k in range(layers))
    return track, holes


def _footprint(
    grid: Grid,
    runs: list[list[int]],
    klass: NetClass,
    pitch: float,
    via: float,
) -> set[int]:
    """The cells a net's routed copper claims from others.

    A neighbour's centreline must stay this track's half width, its
    clearance and the narrowest class's half width from this one's; a
    wider neighbour checks the extra itself (`Grid.rings`). Round a via,
    which is wider and on every layer, it is a via's width and a
    clearance. A cell is claimed when
    its centre is nearer than that: measured, not rounded to whole cells,
    so a diagonal neighbour at 1.4 cells is caught.
    """
    nx, ny = grid.nx, grid.ny
    plane = nx * ny
    track, holes = _centres(grid, runs)
    cells: set[int] = set()
    for nodes, reach in (
        (track, (klass.width / 2 + klass.clearance + grid.narrow) / pitch),
        # Round a via, room for another via: a via's width and a clearance.
        (holes, (via + klass.clearance) / pitch),
    ):
        disc = _within(reach)
        for node in nodes:
            layer, rest = divmod(node, plane)
            j, i = divmod(rest, nx)
            base = layer * plane
            for di, dj in disc:
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < ny:
                    cells.add(base + jj * nx + ii)
    return cells


_WITHIN: dict[float, tuple[tuple[int, int], ...]] = {}


def _within(reach: float) -> tuple[tuple[int, int], ...]:
    """The cell offsets strictly nearer than ``reach`` cells, computed once each."""
    if reach not in _WITHIN:
        r = math.ceil(reach)
        _WITHIN[reach] = tuple(
            (di, dj)
            for dj in range(-r, r + 1)
            for di in range(-r, r + 1)
            if di * di + dj * dj < reach * reach
        )
    return _WITHIN[reach]
