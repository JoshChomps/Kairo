"""Copper pours for a board's high-current nets and planes.

A net poured alone on a layer is a plane: one zone over the whole board,
inset from the edge, which KiCad fills around everything else. Nets
poured together on a layer share it the way a designer divides a power
area: each square millimetre around their pads goes to the net whose
nearest pad is closest (a Voronoi division, Aurenhammer, "Voronoi
diagrams: a survey of a fundamental geometric data structure", ACM
Computing Surveys 23, 1991), so each pour reaches its own pads and stops
halfway to its neighbour's. A net's squares are then joined into as few
rectangles as rows allow, each a zone of that net; zones of one net that
touch fill as one copper area. KiCad fills every zone with its clearance
to other nets (``kicad-cli pcb drc --refill-zones``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence

from kairo.kicad.sexpr import parse
from kairo.pcb.board_writer import Via, Zone
from kairo.pcb.geometry import Rect, edge_distance, inside, point_box

#: The size of the squares a shared layer is divided into, in mm.
CELL = 1.0
#: How far a shared pour reaches beyond its pads' box, in mm.
REACH = 3.0
#: No square farther than this from every pad of a shared pour is poured, in mm.
#: Stitching vias' pitch, and their least distance from a pour's edge, in mm.
STITCH_PITCH = 2.5
STITCH_INSET = 1.0
MAX_REACH = 12.0


def plane(
    net: str,
    layer: str,
    width: float,
    height: float,
    origin: tuple[float, float],
    inset: float = 0.5,
) -> Zone:
    """One zone over the whole board for a net that has a layer to itself."""
    ox, oy = origin
    return Zone(
        net,
        layer,
        (
            (ox + inset, oy + inset),
            (ox + width - inset, oy + inset),
            (ox + width - inset, oy + height - inset),
            (ox + inset, oy + height - inset),
        ),
        priority=0,
    )


def shared(
    pads: Mapping[str, Sequence[tuple[float, float]]],
    layer: str,
    bounds: tuple[float, float, float, float],
    *,
    clearance: float = 0.5,
) -> list[Zone]:
    """Zones that divide a layer among several nets by nearest pad.

    Only the box around the given pads, grown by `REACH`, is divided, and
    no square farther than `MAX_REACH` from every pad is poured: a pour
    stays where its current flows.

    Args:
        pads: Each net's pad centres on the board, in mm.
        layer: The copper layer they share.
        bounds: The board's edge, as left, top, right and bottom, in mm.
        clearance: The zones' clearance to other copper, in mm.

    Returns:
        One zone per connected area of each net, its outline traced around
        its squares. A smaller zone is filled first, so a pour enclosed by
        another keeps its own area.
    """
    left, top, right, bottom = bounds
    xs = [x for points in pads.values() for x, _ in points]
    ys = [y for points in pads.values() for _, y in points]
    if not xs:
        return []
    x0 = max(left + 0.5, min(xs) - REACH)
    y0 = max(top + 0.5, min(ys) - REACH)
    x1 = min(right - 0.5, max(xs) + REACH)
    y1 = min(bottom - 0.5, max(ys) + REACH)
    columns = max(1, math.floor((x1 - x0) / CELL))
    rows = max(1, math.floor((y1 - y0) / CELL))
    owner: dict[tuple[int, int], str] = {}
    for r in range(rows):
        cy = y0 + (r + 0.5) * CELL
        for c in range(columns):
            cx = x0 + (c + 0.5) * CELL
            distance, net = min(
                (min((px - cx) ** 2 + (py - cy) ** 2 for px, py in points), net)
                for net, points in pads.items()
                if points
            )
            if distance <= MAX_REACH**2:
                owner[(c, r)] = net
    outlines: list[tuple[str, float, tuple[tuple[float, float], ...]]] = []
    for net in sorted(set(owner.values())):
        cells = {cell for cell, held in owner.items() if held == net}
        for group in _groups(cells):
            loop = _outline(group)
            corners = tuple(
                (round(x0 + c * CELL, 4), round(y0 + r * CELL, 4)) for c, r in loop
            )
            outlines.append((net, len(group) * CELL * CELL, corners))
    # Smallest first gets the highest priority.
    outlines.sort(key=lambda o: (o[1], o[0]))
    count = len(outlines)
    return [
        Zone(
            net,
            layer,
            corners,
            priority=count - rank,
            clearance=clearance,
            solid=True,
        )
        for rank, (net, _, corners) in enumerate(outlines)
    ]


def _groups(cells: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    """The cells joined edge to edge, each group once."""
    left = set(cells)
    groups: list[set[tuple[int, int]]] = []
    while left:
        first = left.pop()
        group, frontier = {first}, [first]
        while frontier:
            c, r = frontier.pop()
            for near in ((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1)):
                if near in left:
                    left.remove(near)
                    group.add(near)
                    frontier.append(near)
        groups.append(group)
    return groups


def _outline(cells: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """The outer boundary of edge-joined cells, as grid corners in order.

    Each cell side with no cell beyond it is a boundary edge, directed so
    the cells lie on its right; the edges chain into loops, and the loop
    enclosing the most area is the outline (any others are holes, left to
    the fill priority). Corners where the boundary runs straight on are
    dropped.
    """
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for c, r in cells:
        if (c, r - 1) not in cells:
            edges.setdefault((c, r), []).append((c + 1, r))
        if (c + 1, r) not in cells:
            edges.setdefault((c + 1, r), []).append((c + 1, r + 1))
        if (c, r + 1) not in cells:
            edges.setdefault((c + 1, r + 1), []).append((c, r + 1))
        if (c - 1, r) not in cells:
            edges.setdefault((c, r + 1), []).append((c, r))
    loops: list[list[tuple[int, int]]] = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        point = start
        while True:
            ends = edges[point]
            end = ends.pop()
            if not ends:
                del edges[point]
            if end == start:
                break
            loop.append(end)
            point = end
        loops.append(loop)
    best = max(loops, key=lambda lp: abs(_area(lp)))
    corners = [
        p
        for i, p in enumerate(best)
        if not _straight(best[i - 1], p, best[(i + 1) % len(best)])
    ]
    return _unstepped(corners)


#: The longest step, in squares, that counts as part of a staircase.
_STAIR = 2


def _unstepped(corners: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """An outline with each staircase replaced by one straight diagonal.

    Squares make every slanting edge a staircase of short steps; copper
    poured that way has a corner at each step, which crowds current and
    traps etchant. A run of short steps that keep going the same way (each
    across and each down of one sign, each at most `_STAIR` squares) is
    cut to one straight edge from its first corner to its last: 45 degrees
    where the steps are even, as a designer would draw it.
    """
    n = len(corners)
    if n < 6:
        return corners

    def step(i: int) -> tuple[int, int]:
        a, b = corners[i % n], corners[(i + 1) % n]
        return (b[0] - a[0], b[1] - a[1])

    def short(s: tuple[int, int]) -> bool:
        return 0 < abs(s[0]) + abs(s[1]) <= _STAIR

    keep = [True] * n
    i = 0
    while i < n:
        j = i
        sx = sy = 0
        while j - i < n and short(step(j)):
            dx, dy = step(j)
            if (dx and sx and (dx > 0) != (sx > 0)) or (
                dy and sy and (dy > 0) != (sy > 0)
            ):
                break
            sx = sx or dx
            sy = sy or dy
            j += 1
        # A staircase needs steps both across and down: at least three.
        if j - i >= 3 and sx and sy:
            for k in range(i + 1, j):
                keep[k % n] = False
            i = j
        else:
            i += 1
    return [c for c, kept in zip(corners, keep, strict=True) if kept]


def _area(loop: list[tuple[int, int]]) -> float:
    return (
        sum(
            x0 * y1 - x1 * y0
            for (x0, y0), (x1, y1) in zip(loop, loop[1:] + loop[:1], strict=True)
        )
        / 2
    )


def _straight(a: tuple[int, int], b: tuple[int, int], c: tuple[int, int]) -> bool:
    return (b[0] - a[0]) * (c[1] - b[1]) == (b[1] - a[1]) * (c[0] - b[0])


def filled_islands(board_text: str) -> list[Zone]:
    """Each piece of copper KiCad filled, as a zone of its own.

    A pour that shares its layer is filled around other nets' copper at
    their clearance and may break into islands; each ``filled_polygon``
    KiCad saved is one. Given to the router as copper, they let it join
    what the fill left apart.

    Args:
        board_text: A board file KiCad has filled and saved.

    Returns:
        One zone per filled polygon, with its zone's net and layer.
    """
    islands: list[Zone] = []
    for zone in parse(board_text).root.lists("zone"):
        net = zone.first("net")
        if net is None or not net.values():
            continue
        name = str(net.values()[0])
        for filled in zone.lists("filled_polygon"):
            layer = filled.first("layer")
            pts = filled.first("pts")
            if layer is None or pts is None:
                continue
            corners = tuple(
                (float(xy.values()[0]), float(xy.values()[1])) for xy in pts.lists("xy")
            )
            if len(corners) >= 3:
                islands.append(Zone(name, str(layer.values()[0]), corners))
    return islands


#: Where an island stitch may go: a grid this fine, in mm.
ISLAND_PITCH = 1.0
#: How far inside an island's edge a stitch's pad must stay, in mm.
ISLAND_INSET = 0.1


def island_stitches(
    board_text: str,
    nets: Sequence[str],
    layers: Sequence[str],
    *,
    size: float = 0.6,
    drill: float = 0.3,
    hole_gap: float = 0.2,
    through: Sequence[Rect] = (),
) -> list[Via]:
    """Vias that join the islands a pour's fill broke it into.

    KiCad fills a shared pour around other nets' pads at their clearance,
    and where they crowd it the copper breaks apart. A designer joins the
    pieces with vias where they overlap from layer to layer. Here a via
    goes at each point of a `ISLAND_PITCH` grid that lies inside one of
    the net's filled islands on every layer in ``layers``, its pad
    `ISLAND_INSET` inside each island's edge (so the fill already keeps it
    clear of other nets there), one via for each set of islands it would
    join that no via of the net already joins. A new via's drill keeps a
    hole's spacing from every other via and every through pad, whose hole
    is taken as its copper (KiCad's hole-to-hole rule holds within a net
    too).

    Args:
        board_text: A board file KiCad has filled and saved.
        nets: The pour nets to stitch, by KiCad's names.
        layers: The layers they are poured on; a through via meets each.
        size: The via's diameter, in mm.
        drill: Its drill, in mm.
        hole_gap: The least distance between drill holes, in mm.
        through: The through pads' copper boxes on the board, in mm.

    Returns:
        The new vias.
    """
    islands = [z for z in filled_islands(board_text) if z.net in nets]
    root = parse(board_text).root
    existing: list[tuple[float, float, str]] = []
    for via in root.lists("via"):
        at, net = via.first("at"), via.first("net")
        if at is not None and net is not None and net.values():
            existing.append(
                (float(at.values()[0]), float(at.values()[1]), str(net.values()[0]))
            )
    boxes = [
        (
            min(x for x, _ in z.outline),
            min(y for _, y in z.outline),
            max(x for x, _ in z.outline),
            max(y for _, y in z.outline),
        )
        for z in islands
    ]
    reach = size / 2 + ISLAND_INSET

    def joined(x: float, y: float, net: str, margin: float) -> tuple[int, ...] | None:
        """The island on each layer a via there would meet, if it meets one on each."""
        found: list[int] = []
        for layer in layers:
            hit = None
            for k, z in enumerate(islands):
                left, top, right, bottom = boxes[k]
                if (
                    z.net == net
                    and z.layer == layer
                    and left <= x <= right
                    and top <= y <= bottom
                    and inside((x, y), z.outline)
                    and edge_distance((x, y), z.outline) >= margin
                ):
                    hit = k
                    break
            if hit is None:
                return None
            found.append(hit)
        return tuple(found)

    done: set[tuple[int, ...]] = set()
    for x, y, net in existing:
        if net in nets and (key := joined(x, y, net, 0.0)) is not None:
            done.add(key)
    vias: list[Via] = []
    pitch = ISLAND_PITCH
    holes = [(x, y) for x, y, _ in existing]
    apart = drill + hole_gap
    for net in nets:
        own = [
            k for k, z in enumerate(islands) if z.net == net and z.layer == layers[0]
        ]
        for k in own:
            left, top, right, bottom = boxes[k]
            x = math.ceil(left / pitch) * pitch
            while x <= right:
                y = math.ceil(top / pitch) * pitch
                while y <= bottom:
                    key = joined(x, y, net, reach)
                    fresh = key is not None and key not in done
                    if (
                        fresh
                        and all(math.hypot(x - hx, y - hy) >= apart for hx, hy in holes)
                        and all(
                            point_box((x, y), box) >= drill / 2 + hole_gap
                            for box in through
                        )
                    ):
                        assert key is not None
                        done.add(key)
                        holes.append((x, y))
                        vias.append(Via((round(x, 3), round(y, 3)), size, drill, net))
                    y += pitch
                x += pitch
    return vias


def stitching(
    zones: Sequence[Zone],
    keep_clear: Sequence[tuple[float, float, float, float]],
    *,
    size: float = 0.6,
    drill: float = 0.3,
    pads: Sequence[tuple[str, Rect]] = (),
    gap: Callable[[str, str], float] | None = None,
) -> list[Via]:
    """Vias that join a net's pours on every layer it is poured on.

    A pour on an outer layer reaches its pads; the same pour on the other
    layers carries current only if vias join them, as a designer stitches
    a power pour with an array of vias. Each net poured on more than one
    layer gets vias on a `STITCH_PITCH` grid inside its outline on its
    first layer (the outlines are the same on each, being divided by the
    same pads), at least `STITCH_INSET` from the outline's edge, clear
    of every part's courtyard, and a clearance from every other net's
    pad: the larger of the two nets' (``gap``), as KiCad's DRC keeps it.
    A courtyard's margin alone is less than a high-voltage clearance.

    Args:
        zones: The board's pours.
        keep_clear: Boxes no via may touch, such as courtyards, in mm.
        pads: Every pad's net and copper box on the board, in mm.
        gap: The clearance between two nets, in mm
            (`kairo.pcb.specctra.net_gaps`); without it, 0.2.
        size: The via's diameter, in mm.
        drill: Its drill, in mm.

    Returns:
        The stitching vias, each on its pour's net.
    """
    layers: dict[str, set[str]] = {}
    for zone in zones:
        layers.setdefault(zone.net, set()).add(zone.layer)
    first: dict[str, str] = {}
    for zone in zones:
        first.setdefault(zone.net, zone.layer)
    vias: list[Via] = []
    half = size / 2
    for zone in zones:
        if len(layers[zone.net]) < 2 or zone.layer != first[zone.net]:
            continue
        xs = [x for x, _ in zone.outline]
        ys = [y for _, y in zone.outline]
        x = math.ceil(min(xs) / STITCH_PITCH) * STITCH_PITCH
        while x <= max(xs):
            y = math.ceil(min(ys) / STITCH_PITCH) * STITCH_PITCH
            while y <= max(ys):
                around = [
                    (x + dx, y + dy)
                    for dx in (-STITCH_INSET, 0.0, STITCH_INSET)
                    for dy in (-STITCH_INSET, 0.0, STITCH_INSET)
                ]
                clear = all(
                    x + half <= left
                    or x - half >= right
                    or y + half <= top
                    or y - half >= bottom
                    for left, top, right, bottom in keep_clear
                )
                clear = clear and all(
                    point_box((x, y), box) - half
                    >= (gap(zone.net, net) if gap is not None else 0.2) - 1e-9
                    for net, box in pads
                    if net != zone.net
                )
                if clear and all(inside(p, zone.outline) for p in around):
                    vias.append(Via((round(x, 4), round(y, 4)), size, drill, zone.net))
                y += STITCH_PITCH
            x += STITCH_PITCH
    return vias
