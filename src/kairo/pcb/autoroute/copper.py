"""Routes on the grid drawn as tracks and vias, then made legal exactly.

Part of Kairo's router; the method is in the package's docstring.
"""

# The package is one module in layers: its private names are shared.
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import math
from array import array
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise

from kairo.pcb.autoroute.grid import _MOVES, Grid, _Near, _Net, _Shape
from kairo.pcb.board_writer import Track, Via
from kairo.pcb.specctra import NetClass

# -- copper --------------------------------------------------------------------


def _copper(
    grid: Grid,
    nets: Sequence[_Net],
    paths: Mapping[str, list[list[int]]],
    classes: Sequence[NetClass],
    foes: list[list[array[int]]],
    net_id: Mapping[str, int],
    via: tuple[float, float],
    use: array[int],
    claimed: Mapping[str, set[int]],
) -> tuple[list[Track], list[Via], list[tuple[str, int]]]:
    """Each run as straightened tracks, with a via at each layer change.

    Returns:
        The tracks, the vias, and for each (tracks first, then vias) the
        net and the index of the run that drew it.
    """
    tracks: list[Track] = []
    vias: list[Via] = []
    track_of: list[tuple[str, int]] = []
    via_of: list[tuple[str, int]] = []
    for net in nets:
        width = classes[net.klass].width
        number = net_id[net.name]
        for index, run in enumerate(paths.get(net.name, [])):
            legs: list[list[int]] = [[run[0]]]
            for a, b in pairwise(run):
                if grid.split(a)[2] != grid.split(b)[2]:
                    i, j, _ = grid.split(b)
                    vias.append(Via(grid.at(i, j), via[0], via[1], net.name))
                    via_of.append((net.name, index))
                    legs.append([b])
                else:
                    legs[-1].append(b)
            own = claimed.get(net.name, set())
            for leg in legs:
                layer = grid.layers[grid.split(leg[0])[2]]
                for chunk in _chunks(leg, net.neck):
                    # A chunk is necked when its own cells are; its first
                    # cell is shared with the chunk before it.
                    narrow = [net.neck[c] for c in chunk if c in net.neck]
                    necked = len(chunk) > 1 and chunk[1] in net.neck
                    drawn = min([width, *narrow]) if necked else width
                    corners = _straightened(
                        grid, chunk, foes[net.klass], number, use, own
                    )
                    for a, b in pairwise(corners):
                        ga = grid.at(*grid.split(a)[:2])
                        gb = grid.at(*grid.split(b)[:2])
                        points = _dogleg(
                            net.exact.get(a) or ga,
                            net.exact.get(b) or gb,
                            (gb[0] - ga[0], gb[1] - ga[1]),
                            jog_first=b in net.exact and a not in net.exact,
                        )
                        for start, end in pairwise(points):
                            if start != end:
                                tracks.append(Track(start, end, drawn, layer, net.name))
                                track_of.append((net.name, index))
    return tracks, vias, track_of + via_of


#: The slope off a leg's heading that still reads as straight: half a degree.
_STRAIGHT = math.tan(math.radians(0.5))


def _dogleg(
    start: tuple[float, float],
    end: tuple[float, float],
    heading: tuple[float, float],
    jog_first: bool,
) -> list[tuple[float, float]]:
    """A leg from ``start`` to ``end`` as octilinear runs: one, or a long and a jog.

    The grid's leg runs along ``heading``, a multiple of 45 degrees; an end
    moved onto a pad's true axis is up to half a cell off it, and a
    straight line to it would run a degree or two off 45, which reads as
    a mistake. Instead the leg keeps ``heading`` and a short run at 45
    degrees to it takes up the offset, solved exactly: ``end - start`` as
    ``s * heading + t * side``, both lengths positive. The jog goes at the
    end that is not on a pad's axis (``jog_first`` when that is the start),
    so the run along the axis stays on it.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    hx, hy = heading
    size = math.hypot(hx, hy)
    # Off the heading by less than half a degree reads as straight.
    if size == 0 or abs(dx * hy - dy * hx) / size <= _STRAIGHT * math.hypot(dx, dy):
        return [start, end]
    ux, uy = hx / size, hy / size
    best: tuple[float, float, float, float] | None = None
    for turn in (math.pi / 4, -math.pi / 4):
        cos, sin = math.cos(turn), math.sin(turn)
        vx, vy = ux * cos - uy * sin, ux * sin + uy * cos
        det = ux * vy - uy * vx
        s = (dx * vy - dy * vx) / det
        side = (ux * dy - uy * dx) / det
        if s >= 0 and side >= 0 and (best is None or side < best[1]):
            best = (s, side, vx, vy)
    if best is None:
        return [start, end]
    s, side, vx, vy = best
    if jog_first:
        bend = (start[0] + side * vx, start[1] + side * vy)
    else:
        bend = (start[0] + s * ux, start[1] + s * uy)
    return [start, (round(bend[0], 4), round(bend[1], 4)), end]


def _legalize(
    tracks: list[Track],
    vias: Sequence[Via],
    shapes: Sequence[_Shape],
    gap: Callable[[str, str], float],
    slack: float,
    min_track: float,
) -> tuple[list[Track], int]:
    """Neck each track down to the room its neighbours leave, measured exactly.

    The grid measures clearance at cell centres; a track's true line (a
    jog onto a pad's axis, a diagonal past a corner) may come a little
    nearer. Each track is measured against every other net's copper,
    the pads and the routed tracks and vias, and where it is too near it
    is drawn narrower, as a designer necks a track past an obstacle, but
    never below the maker's least track. What still does not fit is left
    as it is, for the audit to report.

    Returns:
        The tracks, and how many were necked.
    """
    copper = list(shapes)
    copper += [
        _Shape(tr.net, tr.layer, start=tr.start, end=tr.end, radius=tr.width / 2)
        for tr in tracks
    ]
    copper += [
        _Shape(v.net, "*", start=v.at, end=v.at, radius=v.size / 2) for v in vias
    ]
    near = _Near(copper)
    out: list[Track] = []
    necked = 0
    for tr in tracks:
        line = _Shape(tr.net, tr.layer, start=tr.start, end=tr.end, radius=0.0)
        room = near.room_for(
            line,
            tr.width / 2,
            lambda other, me=tr.net: gap(me, other) + slack,
        )
        if room < tr.width / 2 and room * 2 >= min_track:
            width = math.floor(200 * room) / 100
            out.append(Track(tr.start, tr.end, width, tr.layer, tr.net))
            necked += 1
        else:
            out.append(tr)
    return out, necked


def _chunks(leg: list[int], neck: Mapping[int, float]) -> list[list[int]]:
    """A leg cut where it enters or leaves a necked stretch, sharing the cut cell."""
    if not neck:
        return [leg]
    chunks: list[list[int]] = [[leg[0]]]
    for a, b in pairwise(leg):
        if (a in neck) != (b in neck):
            chunks.append([a])
        chunks[-1].append(b)
    return [c for c in chunks if len(c) > 1] or [leg]


def _straightened(
    grid: Grid,
    leg: list[int],
    foes: list[array[int]],
    number: int,
    use: array[int],
    own: set[int],
) -> list[int]:
    """The fewest corners along a run whose straight octilinear shortcuts are open."""
    if len(leg) < 3:
        return leg
    layer = grid.split(leg[0])[2]
    plane = grid.nx * grid.ny
    corners = [leg[0]]
    k = 0
    while k < len(leg) - 1:
        reach = k + 1
        for m in range(len(leg) - 1, k + 1, -1):
            if _open_line(
                grid, leg[k], leg[m], foes[layer], number, layer * plane, use, own
            ):
                reach = m
                break
        corners.append(leg[reach])
        k = reach
    return corners


def _open_line(
    grid: Grid,
    a: int,
    b: int,
    foe: array[int],
    number: int,
    offset: int,
    use: array[int],
    own: set[int],
) -> bool:
    ia, ja, _ = grid.split(a)
    ib, jb, _ = grid.split(b)
    di, dj = ib - ia, jb - ja
    if not (di == 0 or dj == 0 or abs(di) == abs(dj)):
        return False
    steps = max(abs(di), abs(dj))
    si, sj = (di > 0) - (di < 0), (dj > 0) - (dj < 0)
    move = _MOVES.index((si, sj)) if si or sj else 0
    graze = grid.grazes.get(number, {})
    for s in range(steps + 1):
        cell = (ja + s * sj) * grid.nx + ia + s * si
        if foe[cell] not in (0, number) and s not in (0, steps):
            return False
        node = offset + cell
        if use[node] - (1 if node in own else 0) > 0:
            return False
        # The corner rule the search keeps: no diagonal between two
        # blocked cells (read only where the step exists).
        if (
            si
            and sj
            and s < steps
            and foe[cell + si] not in (0, number)
            and foe[cell + sj * grid.nx] not in (0, number)
        ):
            return False
        if si and sj and s < steps:
            near = graze.get(node * 8 + move)
            if near is not None and any(n != number for n in near):
                return False
    return True
