"""Wires between pins, for a drawing a person reads.

A labelled drawing joins pins by name: every pin ends in a short stub and
a label, which is easy to check and hard to follow. This module draws the
wires instead: each net's pins in one group are joined by a tree of
horizontal and vertical wires on the 1.27 mm grid, found one pin at a time
by A* search (Hart, Nilsson and Raphael, "A Formal Basis for the Heuristic
Determination of Minimum Cost Paths", 1968) from the pin to the tree so
far, the way a maze router grows a net (Lee, "An Algorithm for Path
Connections and Its Applications", 1961).

KiCad joins two wires where one ends on the other, and not where two
cross, so a wire of one net may cross another's only going straight
through it. It may never end, bend or run along another net's wire, touch
another net's pin, or enter a symbol's body or the room kept for a label,
a power symbol or a pin's text. A group the search cannot join is left to
labels, never forced. Whatever this module draws, KiCad's own netlist of
the sheet must still equal the design (layer L3): the search is a way of
drawing, and the netlist is the proof.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from kairo.kicad.geometry import GRID, Box

Point = tuple[float, float]
_Node = tuple[int, int]

#: The four ways a wire can run, as grid steps.
_STEPS: tuple[_Node, ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
#: What a bend costs beyond its length, in grid steps: fewer bends read better.
BEND = 4
#: What crossing another net's wire costs, in grid steps.
CROSSING = 6
#: How far past its pins' box a group's search may go, in grid steps, at
#: each try.
WINDOWS = (16, 48)
#: The most states one search may take before the group is left to labels.
LIMIT = 200_000


@dataclass(frozen=True, slots=True)
class Terminal:
    """A pin to be joined.

    Attributes:
        key: The pin, as ``REF.NUMBER``.
        at: Where its end is on the sheet, in mm, on the grid.
        out: The step leading away from its part, such as ``(1, 0)``;
            ``None`` for a label's end on a stub, where a wire may arrive
            from any side. Such a terminal must come first in its group.
    """

    key: str
    at: Point
    out: _Node | None


@dataclass(frozen=True, slots=True)
class Group:
    """Pins of one net that should be joined by wires.

    Attributes:
        net: The net.
        terminals: Its pins in this group, two or more.
    """

    net: str
    terminals: tuple[Terminal, ...]


@dataclass(frozen=True, slots=True)
class Route:
    """The wires that join one group.

    Attributes:
        net: The net.
        segments: Each wire, from one end to the other, in mm.
        junctions: Where three or more wire ends or pins meet, in mm.
    """

    net: str
    segments: tuple[tuple[Point, Point], ...]
    junctions: tuple[Point, ...]


@dataclass(slots=True)
class Board:
    """The sheet as the search sees it.

    Attributes:
        bounds: The area wires may use, in grid steps, inclusive.
        blocked: Points no wire may use: inside a body or kept room.
        ends: Points where a net's wire ends, bends or meets a pin, by net.
        across: Points a net's horizontal wire covers, by net.
        down: Points a net's vertical wire covers, by net.
    """

    bounds: tuple[int, int, int, int]
    blocked: set[_Node] = field(default_factory=set[_Node])
    ends: dict[_Node, str] = field(default_factory=dict[_Node, str])
    across: dict[_Node, str] = field(default_factory=dict[_Node, str])
    down: dict[_Node, str] = field(default_factory=dict[_Node, str])

    def block(self, box: Box) -> None:
        """Keep wires out of a box, edges included."""
        left, top = _node((box.left, box.top), up=False)
        right, bottom = _node((box.right, box.bottom), up=True)
        for i in range(left, right + 1):
            for j in range(top, bottom + 1):
                self.blocked.add((i, j))

    def keep(self, net: str, points: Sequence[Point]) -> None:
        """Take a wire for a net through the given corners, as if drawn.

        Each given point is an end or a bend, where another net may not
        come; the points between are covered along the wire's axis, where
        another net may only cross.

        Raises:
            ValueError: If two points in a row are not on one axis.
        """
        nodes = [_node(p) for p in points]
        for node in nodes:
            self.ends[node] = net
        for a, b in itertools.pairwise(nodes):
            if a[0] != b[0] and a[1] != b[1]:
                raise ValueError(f"{points} is not along the grid's axes")
            layer = self.across if a[1] == b[1] else self.down
            for node in _between(a, b):
                layer[node] = net

    def free(self, points: Sequence[Point]) -> None:
        """Let wires use a path a block covered, such as a pin's own stub.

        Every grid point along the path is freed, not only its corners.
        """
        nodes = [_node(point) for point in points]
        for node in nodes:
            self.blocked.discard(node)
        for a, b in itertools.pairwise(nodes):
            if a[0] == b[0] or a[1] == b[1]:
                for node in _between(a, b):
                    self.blocked.discard(node)


def board_for(sheet: Box) -> Board:
    """An empty board over a sheet's drawing area."""
    left, top = _node((sheet.left, sheet.top), up=True)
    right, bottom = _node((sheet.right, sheet.bottom), up=False)
    return Board((left, top, right, bottom))


def route_groups(board: Board, groups: Iterable[Group]) -> dict[int, Route]:
    """Join each group's pins with wires, the smallest groups first.

    Args:
        board: The sheet, with every pin's end already taken by its net
            (`Board.keep`) and every body and kept room blocked. Each
            route found is added to it.
        groups: The groups to join.

    Returns:
        Each group's wires, by its position in ``groups``. A group that
        could not be joined is absent and is left to labels.
    """
    listed = list(groups)
    order = sorted(range(len(listed)), key=lambda n: _spread(listed[n]))
    routes: dict[int, Route] = {}
    for number in order:
        found = _route_group(board, listed[number])
        if found is not None:
            routes[number] = found
    return routes


def _spread(group: Group) -> tuple[float, int]:
    xs = [t.at[0] for t in group.terminals]
    ys = [t.at[1] for t in group.terminals]
    return (max(xs) - min(xs) + max(ys) - min(ys), len(group.terminals))


def _route_group(board: Board, group: Group) -> Route | None:
    """Join one group, trying a wider window before giving up."""
    for window in WINDOWS:
        paths = _grow(board, group, window)
        if paths is not None:
            break
    else:
        return None
    for path in paths:
        for a, b in _straight(path):
            board.keep(group.net, [_point(a), _point(b)])
    return _as_route(group, paths)


def _grow(board: Board, group: Group, window: int) -> list[list[_Node]] | None:
    """Grow a tree from the first pin, adding the nearest pin each time."""
    net = group.net
    terminals = list(group.terminals)
    first = terminals.pop(0)
    tree: set[_Node] = {_node(first.at)}
    pins: dict[_Node, _Node | None] = {_node(t.at): t.out for t in group.terminals}
    xs = [_node(t.at)[0] for t in group.terminals]
    ys = [_node(t.at)[1] for t in group.terminals]
    left, top, right, bottom = board.bounds
    bounds = (
        max(left, min(xs) - window),
        max(top, min(ys) - window),
        min(right, max(xs) + window),
        min(bottom, max(ys) + window),
    )
    paths: list[list[_Node]] = []
    # Paths found here are not yet on the board, so a later pin of the
    # same group must not cross or run along them as if they were another
    # net's: they are the tree it joins.
    while terminals:
        terminals.sort(key=lambda t: _distance(_node(t.at), tree))
        start = terminals.pop(0)
        path = _search(board, net, start, tree, pins, bounds)
        if path is None:
            return None
        paths.append(path)
        tree.update(_on_path(path))
    return paths


def _search(
    board: Board,
    net: str,
    start: Terminal,
    tree: set[_Node],
    pins: dict[_Node, _Node | None],
    bounds: tuple[int, int, int, int],
) -> list[_Node] | None:
    """The cheapest path from a pin out along its stub to the tree."""
    origin = _node(start.at)
    out = start.out
    if out is None:
        raise ValueError(
            f"{start.key} has no way out; only a group's first may lack one"
        )
    box = _box_of(tree)
    count = itertools.count()
    begin = (origin, _STEPS.index(out))
    frontier: list[tuple[int, int, int, tuple[_Node, int]]] = [
        (_to_box(origin, box), 0, next(count), begin)
    ]
    cost: dict[tuple[_Node, int], int] = {begin: 0}
    came: dict[tuple[_Node, int], tuple[_Node, int] | None] = {begin: None}
    taken = 0
    while frontier:
        _, spent, _, state = heapq.heappop(frontier)
        if spent > cost.get(state, spent):
            continue
        taken += 1
        if taken > LIMIT:
            return None
        node, heading = state
        for turn, step in enumerate(_STEPS):
            if step == (-_STEPS[heading][0], -_STEPS[heading][1]):
                continue
            bending = turn != heading
            if bending and (node == origin or not _may_bend(board, net, node)):
                # A pin's wire leaves straight out along its own stub.
                continue
            nxt = (node[0] + step[0], node[1] + step[1])
            if not _inside(nxt, bounds):
                continue
            arrived = nxt in tree and _may_end(board, net, nxt, step, pins)
            if not arrived and not _may_pass(board, net, nxt, step):
                continue
            extra = 1 + (BEND if bending else 0)
            if _crosses(board, net, nxt, step):
                extra += CROSSING
            new = (nxt, turn)
            total = spent + extra
            if total >= cost.get(new, total + 1):
                continue
            cost[new] = total
            came[new] = state
            if arrived:
                return _back(came, new)
            heapq.heappush(
                frontier, (total + _to_box(nxt, box), total, next(count), new)
            )
    return None


def _may_pass(board: Board, net: str, node: _Node, step: _Node) -> bool:
    """Whether a wire of ``net`` may enter a point going ``step``."""
    if node in board.blocked:
        return False
    if board.ends.get(node, net) != net:
        return False
    along = board.across if step[1] == 0 else board.down
    return along.get(node, net) == net


def _may_bend(board: Board, net: str, node: _Node) -> bool:
    """Whether a wire may turn at a point: nothing of another net is there."""
    return all(
        layer.get(node, net) == net for layer in (board.ends, board.across, board.down)
    )


def _may_end(
    board: Board, net: str, node: _Node, step: _Node, pins: dict[_Node, _Node | None]
) -> bool:
    """Whether a wire may end on the tree here, arriving by ``step``."""
    if not _may_bend(board, net, node):
        return False
    out = pins.get(node)
    if out is not None:
        # Into a pin only along its stub, from outside the part.
        return step == (-out[0], -out[1])
    return True


def _crosses(board: Board, net: str, node: _Node, step: _Node) -> bool:
    across = board.down if step[1] == 0 else board.across
    return across.get(node, net) != net


def _back(
    came: dict[tuple[_Node, int], tuple[_Node, int] | None], end: tuple[_Node, int]
) -> list[_Node]:
    nodes: list[_Node] = []
    state: tuple[_Node, int] | None = end
    while state is not None:
        nodes.append(state[0])
        state = came[state]
    return nodes[::-1]


def _as_route(group: Group, paths: list[list[_Node]]) -> Route:
    """Paths as straight wires, with a junction where three or more meet."""
    segments: list[tuple[_Node, _Node]] = []
    for path in paths:
        segments += _straight(path)
    degree: dict[_Node, int] = {}
    for a, b in segments:
        for end in (a, b):
            degree[end] = degree.get(end, 0) + 1
        for node in _between(a, b):
            if node not in (a, b):
                degree[node] = degree.get(node, 0) + 2
    for terminal in group.terminals:
        node = _node(terminal.at)
        degree[node] = degree.get(node, 0) + 1
    junctions = sorted(_point(n) for n, d in degree.items() if d >= 3)
    return Route(
        group.net,
        tuple((_point(a), _point(b)) for a, b in segments),
        tuple(junctions),
    )


def _straight(path: list[_Node]) -> list[tuple[_Node, _Node]]:
    """A path's points merged into straight runs."""
    runs: list[tuple[_Node, _Node]] = []
    start = path[0]
    for before, here, after in zip(path, path[1:], path[2:], strict=False):
        if (here[0] - before[0], here[1] - before[1]) != (
            after[0] - here[0],
            after[1] - here[1],
        ):
            runs.append((start, here))
            start = here
    runs.append((start, path[-1]))
    return [(a, b) for a, b in runs if a != b]


def _on_path(path: list[_Node]) -> set[_Node]:
    return set(path)


def _between(a: _Node, b: _Node) -> list[_Node]:
    """Every grid point from a to b, both included, along one axis."""
    if a[1] == b[1]:
        lo, hi = sorted((a[0], b[0]))
        return [(i, a[1]) for i in range(lo, hi + 1)]
    lo, hi = sorted((a[1], b[1]))
    return [(a[0], j) for j in range(lo, hi + 1)]


def _box_of(nodes: set[_Node]) -> tuple[int, int, int, int]:
    xs = [n[0] for n in nodes]
    ys = [n[1] for n in nodes]
    return (min(xs), min(ys), max(xs), max(ys))


def _to_box(node: _Node, box: tuple[int, int, int, int]) -> int:
    """Steps from a point to a box: never more than the path still needs."""
    left, top, right, bottom = box
    dx = max(left - node[0], 0, node[0] - right)
    dy = max(top - node[1], 0, node[1] - bottom)
    return dx + dy


def _distance(node: _Node, tree: set[_Node]) -> int:
    return min(abs(node[0] - t[0]) + abs(node[1] - t[1]) for t in tree)


def _inside(node: _Node, bounds: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = bounds
    return left <= node[0] <= right and top <= node[1] <= bottom


def _node(point: Point, up: bool | None = None) -> _Node:
    """A point in mm as grid steps; ``up`` rounds outward for a box's edge."""
    x, y = point[0] / GRID, point[1] / GRID
    if up is None:
        return (round(x), round(y))
    if up:
        return (math.ceil(x - 1e-6), math.ceil(y - 1e-6))
    return (math.floor(x + 1e-6), math.floor(y + 1e-6))


def _point(node: _Node) -> Point:
    return (round(node[0] * GRID, 4), round(node[1] * GRID, 4))
