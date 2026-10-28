"""A board's routing: its case, the route, the audit and the router for layout.

Part of Kairo's router; the method is in the package's docstring.
"""

# The package is one module in layers: its private names are shared.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import math
import time
from array import array
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from kairo.library.index import LibraryIndex
from kairo.pcb.autoroute.copper import _copper, _legalize
from kairo.pcb.autoroute.grid import (
    Grid,
    Settings,
    _bounds,
    _cells,
    _centre,
    _distance,
    _escape,
    _far_drop,
    _far_take,
    _foes,
    _footprint,
    _grazes_all,
    _keep_off_edge,
    _merged,
    _Near,
    _Net,
    _Scratch,
    _Shape,
    _within,
)
from kairo.pcb.autoroute.negotiate import _negotiate
from kairo.pcb.autoroute.search import _rejoin
from kairo.pcb.board_writer import PlacedPart, Stackup, Track, Via, Zone
from kairo.pcb.layout import Router
from kairo.pcb.place import placed_pads, read_outline
from kairo.pcb.specctra import NetClass, between, keepout_polygons, net_gaps


def kairo_router(
    index: LibraryIndex,
    *,
    settings: Settings | None = None,
    stage: str = "all",
    only: Collection[str] = (),
    layers: Sequence[str] = (),
) -> Router:
    """A router for `kairo.pcb.layout.lay_out_board`, routed by Kairo itself.

    Args:
        index: The library index, for each footprint's pads.
        settings: The grid and the costs.
        stage: ``all`` (the default) routes every net as tracks, the
            poured nets too, before any pour is filled: the pours then
            add copper to what is already joined (docs/router-log.md,
            finding 17). ``join`` is given the filled pours' islands as copper and
            joins every net whose copper is still in pieces.
        only: Route these nets alone, the rest left as they are: a net
            routed first, as by hand (`kairo.ir.RouteGuide`).
        layers: The routing layers to use, of those the board routes on;
            all of them if none are named.

    Returns:
        A router.
    """
    tune = settings or Settings()

    def route(
        parts: Sequence[PlacedPart],
        outline: Sequence[tuple[float, float]],
        zones: Sequence[Zone],
        stackup: Stackup,
        fixed: Sequence[Via],
        fixed_tracks: Sequence[Track],
        classes: Sequence[NetClass],
    ) -> tuple[list[Track], list[Via]]:
        case = case_for(
            index, parts, outline, zones, stackup, fixed, fixed_tracks, classes, stage
        )
        return route_case(focus(case, only, layers), tune)

    return route


@dataclass(frozen=True, slots=True)
class RouteCase:
    """Everything the router is given, frozen: a benchmark can replay it.

    Attributes:
        shapes: All copper already on the board, by net.
        outline: The board edge's corners, in mm.
        layers: The layers to route on, top first.
        classes: Each net's width and clearance.
        skip: Nets not to route.
        via: The via's diameter and drill, in mm.
    """

    shapes: tuple[_Shape, ...]
    outline: tuple[tuple[float, float], ...]
    layers: tuple[str, ...]
    classes: tuple[NetClass, ...]
    skip: frozenset[str]
    via: tuple[float, float]


def case_for(
    index: LibraryIndex,
    parts: Sequence[PlacedPart],
    outline: Sequence[tuple[float, float]],
    zones: Sequence[Zone],
    stackup: Stackup,
    fixed: Sequence[Via],
    fixed_tracks: Sequence[Track],
    classes: Sequence[NetClass],
    stage: str = "all",
) -> RouteCase:
    """What the router is to route, from the placed board.

    Args:
        index: The library index, for each footprint's pads.
        parts: The placed parts.
        outline: The board edge's corners, in mm.
        zones: The pours: whole-layer planes, and on shared layers the
            filled islands (``join``).
        stackup: The copper layers.
        fixed: Vias already placed.
        fixed_tracks: Tracks already placed.
        classes: The routing classes.
        stage: ``all`` or ``join``, as `kairo_router` takes.

    Returns:
        The case.
    """
    planes = {
        z.layer for z in zones if len({y.net for y in zones if y.layer == z.layer}) == 1
    }
    layers = tuple(name for name in stackup.copper if name not in planes)
    shapes = _pads(parts, index)
    # A footprint's keep-out (a radio module's antenna area) is in
    # everyone's way: copper of no net.
    shapes += [
        _Shape("", "*", polygon=tuple(corners))
        for _, corners in keepout_polygons(index, parts)
    ]
    shapes += [
        _Shape(t.net, t.layer, start=t.start, end=t.end, radius=t.width / 2)
        for t in fixed_tracks
    ]
    shapes += [
        _Shape(v.net, "*", start=v.at, end=v.at, radius=v.size / 2) for v in fixed
    ]
    if stage == "join":
        shapes += [
            _Shape(z.net, z.layer, polygon=z.outline)
            for z in zones
            if z.layer not in planes
        ]
    return RouteCase(
        tuple(shapes),
        tuple(outline),
        layers,
        tuple(classes),
        frozenset(),
        _via_of(fixed),
    )


def focus(
    case: RouteCase, only: Collection[str] = (), layers: Sequence[str] = ()
) -> RouteCase:
    """A case narrowed to some nets and layers: one net routed as by hand.

    Args:
        case: The whole board's case.
        only: The nets to route; the rest stay as they are, in the way.
            Every net if none are named.
        layers: The routing layers to use; every one if none are named.

    Returns:
        The narrowed case.
    """
    if only:
        nets = {s.net for s in case.shapes if s.net}
        case = replace(case, skip=frozenset(case.skip | (nets - set(only))))
    if layers:
        case = replace(case, layers=tuple(n for n in case.layers if n in layers))
    return case


def route_case(
    case: RouteCase, tune: Settings, stats: dict[str, Any] | None = None
) -> tuple[list[Track], list[Via]]:
    """Route a case (`route_board` on its parts)."""
    return route_board(
        case.shapes,
        case.outline,
        case.layers,
        case.classes,
        tune,
        skip=set(case.skip),
        via=case.via,
        stats=stats,
    )


def case_to_json(case: RouteCase) -> dict[str, object]:
    """A case as plain data, for a benchmark file."""
    return {
        "shapes": [
            {
                k: v
                for k, v in (
                    ("net", s.net),
                    ("layer", s.layer),
                    ("box", s.box),
                    ("start", s.start),
                    ("end", s.end),
                    ("radius", s.radius or None),
                    ("polygon", s.polygon),
                )
                if v is not None
            }
            for s in case.shapes
        ],
        "outline": case.outline,
        "layers": case.layers,
        "classes": [
            {
                "name": c.name,
                "width": c.width,
                "clearance": c.clearance,
                "nets": c.nets,
                **({"apart": [list(a) for a in c.apart]} if c.apart else {}),
            }
            for c in case.classes
        ],
        "skip": sorted(case.skip),
        "via": case.via,
    }


def case_from_json(data: Mapping[str, Any]) -> RouteCase:
    """A case back from `case_to_json`'s data."""

    def pair(v: Sequence[float] | None) -> tuple[float, float] | None:
        return (float(v[0]), float(v[1])) if v is not None else None

    shapes = tuple(
        _Shape(
            str(s["net"]),
            str(s["layer"]),
            box=tuple(s["box"]) if "box" in s else None,
            start=pair(s.get("start")),
            end=pair(s.get("end")),
            radius=float(s.get("radius", 0.0)),
            polygon=tuple(pair(q) for q in s["polygon"]) if "polygon" in s else None,  # type: ignore[misc]
        )
        for s in data["shapes"]
    )
    return RouteCase(
        shapes,
        tuple(pair(q) for q in data["outline"]),  # type: ignore[misc]
        tuple(data["layers"]),
        tuple(
            NetClass(
                c["name"],
                c["width"],
                c["clearance"],
                tuple(c["nets"]),
                tuple((str(n), float(g)) for n, g in c.get("apart", [])),
            )
            for c in data["classes"]
        ),
        frozenset(data["skip"]),
        pair(data["via"]) or (0.6, 0.3),
    )


def audit(
    case: RouteCase,
    tracks: Sequence[Track],
    vias: Sequence[Via],
    edge: float = 0.5,
    examples: dict[str, list[str]] | None = None,
    hole_gap: float = 0.2,
    pairs: list[tuple[int, int]] | None = None,
) -> dict[str, int]:
    """An exact check of routed copper against the case, before KiCad's.

    Every new track and via is measured against every shape and every
    other new item of another net on a shared layer, by true distance
    (segments, discs and boxes), through a spatial hash of 1 mm buckets,
    so each item meets only its neighbours. Two items of different nets
    must stand the larger of their classes' clearances apart.

    Args:
        case: The case routed.
        tracks: The tracks the router laid.
        vias: The vias it placed.
        edge: Copper's least distance to the board's edge, in mm.
        hole_gap: The least distance between drill holes, in mm.
        examples: Filled, if given, with up to five examples of each
            fault, naming the two items, their nets and layers, and the
            distance.
        pairs: Filled, if given, with each short, clearance or hole fault
            as two indices into the new items (tracks, then vias); -1 where
            the other item is copper the case already had.

    Returns:
        ``shorts`` (copper touching), ``clearance`` (nearer than allowed),
        ``edge``, ``keepout`` (inside a keep-out) and ``holes`` (drill
        holes nearer than ``hole_gap``), each counted once per pair.
    """
    gap = net_gaps(case.classes)
    widest = max(
        (between(a, b) for a in case.classes for b in case.classes), default=0.2
    )
    items: list[tuple[str, str, _Shape]] = []  # net, layer, shape
    for s in case.shapes:
        items.append((s.net, s.layer, s))
    new = len(items)
    for tr in tracks:
        items.append(
            (
                tr.net,
                tr.layer,
                _Shape(
                    tr.net, tr.layer, start=tr.start, end=tr.end, radius=tr.width / 2
                ),
            )
        )
    for v in vias:
        items.append(
            (v.net, "*", _Shape(v.net, "*", start=v.at, end=v.at, radius=v.size / 2))
        )
    index = _Near([s for _, _, s in items])
    boxes = [_bounds(s) for _, _, s in items]
    xs = [x for x, _ in case.outline]
    ys = [y for _, y in case.outline]
    found = {"shorts": 0, "clearance": 0, "edge": 0, "keepout": 0, "holes": 0}
    seen: set[tuple[int, int]] = set()
    for k in range(new, len(items)):
        net, layer, shape = items[k]
        left, top, right, bottom = boxes[k]
        if (
            left < min(xs) + edge - 1e-6
            or top < min(ys) + edge - 1e-6
            or right > max(xs) - edge + 1e-6
            or bottom > max(ys) - edge + 1e-6
        ):
            found["edge"] += 1
        reach = widest
        for m in index.around(boxes[k], reach):
            other_net, other_layer, other = items[m]
            if m == k or (other_net == net and other_net):
                continue
            if layer != "*" and other_layer != "*" and layer != other_layer:
                continue
            pair_key = (min(k, m), max(k, m))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            apart = _distance(shape, other)
            if other.polygon is not None and not other_net:
                if apart <= 0:
                    found["keepout"] += 1
                continue
            need = gap(net, other_net)
            kind = (
                "shorts"
                if apart <= 1e-6
                else "clearance"
                if apart < need - 1e-3
                else ""
            )
            if not kind:
                continue
            found[kind] += 1
            if pairs is not None:
                pairs.append((k - new, m - new if m >= new else -1))
            if examples is not None and len(examples.setdefault(kind, [])) < 5:
                examples[kind].append(
                    f"{_name(shape)} {net} on {layer} | {_name(other)} "
                    f"{other_net or '(no net)'} on {other_layer}: {apart:.3f} mm"
                )
    # Drill holes keep apart whatever their nets: each new via against the
    # other new vias and every through pad, hole edge to hole edge (a
    # pad's hole taken as its copper, which holds it).
    drill = case.via[1] / 2
    holes = [v.at for v in vias]
    found["holes"] = 0
    for n, (x, y) in enumerate(holes):
        for m in range(n + 1, len(holes)):
            ox, oy = holes[m]
            if (
                abs(ox - x) < 2
                and math.hypot(ox - x, oy - y) - 2 * drill < hole_gap - 1e-3
            ):
                found["holes"] += 1
                if pairs is not None:
                    pairs.append((len(tracks) + n, len(tracks) + m))
                if examples is not None and len(examples.setdefault("holes", [])) < 5:
                    examples["holes"].append(f"via at {(x, y)} | via at {(ox, oy)}")
        probe = _Shape("", "*", start=(x, y), end=(x, y), radius=drill)
        for s in case.shapes:
            if (
                s.layer == "*"
                and s.box is not None
                and abs(s.box[0] - x) < 5
                and _distance(probe, s) < hole_gap - 1e-3
            ):
                found["holes"] += 1
                if pairs is not None:
                    pairs.append((len(tracks) + n, -1))
                if examples is not None and len(examples.setdefault("holes", [])) < 5:
                    examples["holes"].append(f"via at {(x, y)} | {s.net} pin")
    return found


def _name(shape: _Shape) -> str:
    if shape.box is not None:
        return "pad"
    if shape.polygon is not None:
        return "area"
    return "via" if shape.start == shape.end else "track"


def route_board(
    shapes: Sequence[_Shape],
    outline: Sequence[tuple[float, float]],
    layers: Sequence[str],
    classes: Sequence[NetClass],
    tune: Settings,
    *,
    skip: set[str] | None = None,
    via: tuple[float, float] = (0.6, 0.3),
    stats: dict[str, Any] | None = None,
) -> tuple[list[Track], list[Via]]:
    """Route every net whose copper is in more than one piece.

    Args:
        shapes: All copper already on the board, by net.
        outline: The board edge's corners, in mm.
        layers: The layers to route on, top first.
        classes: Each net's width and clearance.
        tune: The grid and the costs.
        skip: Nets not to route.
        via: The via's diameter and drill, in mm.
        stats: Filled, if given, with what the router did: ``connections``
            (pieces to join), ``routed``, ``unreachable`` (blocked by fixed
            copper), ``dropped`` (still crossing after the last round),
            ``rounds`` and the seconds each phase took.

    Returns:
        The tracks and vias that join them.
    """
    clock = time.perf_counter()
    report: dict[str, Any] = stats if stats is not None else {}
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    p = tune.pitch
    grid = Grid(
        min(xs),
        min(ys),
        p,
        int((max(xs) - min(xs)) / p) + 1,
        int((max(ys) - min(ys)) / p) + 1,
        tuple(layers),
    )
    grid.scratch = _Scratch(grid.nx * grid.ny * len(grid.layers))
    grid.scratch.hole_ring = _within((via[1] + tune.hole_gap) / p)
    narrowest = min((c.width for c in classes), default=0.25)
    grid.via_ring = _within(max((via[0] - narrowest) / 2 / p, 0.0) + 1.0)
    grid.narrow = narrowest / 2
    # A neighbour's claim reaches its own clearance out; a mover's ring
    # covers what the larger class of the pair needs beyond that, its width
    # aside. A pair that needs more (the voltage between them) is held by
    # far claims instead (`Grid.far_of`), so a class keeps only its own
    # clearance from the classes it is near in voltage.
    beyond = [max(0.0, *(c.clearance - d.clearance for d in classes)) for c in classes]
    grid.rings = tuple(
        _within(((c.width - narrowest) / 2 + extra) / p + 0.5) or ((0, 0),)
        for c, extra in zip(classes, beyond, strict=True)
    )
    far_of = tuple(
        tuple(
            j
            for j, d in enumerate(classes)
            if between(c, d) > max(c.clearance, d.clearance) + 1e-9
        )
        for c in classes
    )
    grid.far_of = far_of if any(far_of) else ()
    size = grid.nx * grid.ny * len(grid.layers)
    for j in sorted({j for kept in far_of for j in kept}):
        d = classes[j]
        movers = [k for k, kept in enumerate(far_of) if j in kept]
        reach = max(between(d, classes[k]) for k in movers)
        half = max([via[0] / 2, *(classes[k].width / 2 for k in movers)])
        # A claim reaching the pair's spacing plus the widest mover's half,
        # so the mover need only look at its own centre.
        grid.far_class[j] = NetClass(d.name, d.width, reach + half - grid.narrow, ())
        grid.far_use[j] = array("i", bytes(4 * size))
    class_of = {net: k for k, c in enumerate(classes) for net in c.nets}
    names = sorted(
        {s.net for s in shapes if s.net and not s.net.startswith("unconnected-")}
    )
    net_id = {name: k + 1 for k, name in enumerate(names)}
    # Each class's map of other nets' copper, grown for that class.
    foes = [
        _foes(
            grid, shapes, classes, k, net_id, class_of, via, tune.slack, tune.hole_gap
        )
        for k in range(len(classes))
    ]
    for k, klass in enumerate(classes):
        _keep_off_edge(
            grid, foes[k], tune.edge + klass.width / 2, tune.edge + via[0] / 2
        )
    near = _Near(shapes)
    gap = net_gaps(classes)
    nets = _nets(
        grid,
        shapes,
        names,
        class_of,
        skip or set(),
        classes,
        near,
        gap,
        tune.slack,
        tune.min_track,
    )
    # A pad's own cells, and its escape, are open to its net past other
    # copper: a step there matters even where every net is blocked.
    open_cells = {c for n in nets for piece in n.pieces for c in piece}
    open_cells |= {c for n in nets for c in n.escape}
    barred = _grazes_all(
        grid, foes, classes, near, net_id, class_of, tune.min_track, open_cells
    )
    grid.grazes = {
        number: barred[class_of[name]]
        for name, number in net_id.items()
        if name in class_of
    }
    report["setup_s"] = round(time.perf_counter() - clock, 2)
    clock = time.perf_counter()
    paths, use, claimed = _negotiate(
        grid, nets, foes, net_id, classes, tune, via[0], report
    )
    report["negotiate_s"] = round(time.perf_counter() - clock, 2)
    clock = time.perf_counter()
    tracks, vias, owner = _copper(
        grid, nets, paths, classes, foes, net_id, via, use, claimed
    )
    tracks, report["necked"] = _legalize(
        tracks, vias, shapes, gap, tune.slack, tune.min_track
    )
    # The last word is exact geometry: a run still at fault after
    # legalising (the shorter of a clashing pair) is dropped, its net
    # joined again with every route a wall, and the rest drawn again, so
    # a gap is reported where a short would have been made.
    case = RouteCase(
        tuple(shapes),
        tuple(outline),
        tuple(layers),
        tuple(classes),
        frozenset(skip or ()),
        via,
    )
    report["faulted"] = 0
    by_name = {net.name: net for net in nets}
    calm = array("d", bytes(8 * len(use)))
    for attempt in range(3):
        pairs: list[tuple[int, int]] = []
        audit(case, tracks, vias, edge=tune.edge, hole_gap=tune.hole_gap, pairs=pairs)
        if not pairs:
            break
        drop: set[tuple[str, int]] = set()
        for a, b in pairs:
            mine, theirs = owner[a], owner[b] if b >= 0 else None
            if theirs is not None and len(paths[theirs[0]][theirs[1]]) < len(
                paths[mine[0]][mine[1]]
            ):
                mine = theirs
            drop.add(mine)
        for name in {name for name, _ in drop}:
            paths[name] = [
                run for k, run in enumerate(paths[name]) if (name, k) not in drop
            ]
            # Joined again, every other route a wall, as the completion
            # pass joins; a new fault is dropped next round, and the last
            # round only drops, so the copper returned was audited.
            if attempt == 2:
                continue
            for cell in claimed.pop(name, set()):
                use[cell] -= 1
            _far_drop(grid, name)
            net = by_name[name]
            paths[name] += _rejoin(
                grid, net, paths[name], foes, net_id, use, calm, 1.0, tune, hard=True
            )
            claimed[name] = _footprint(
                grid, paths[name], classes[net.klass], tune.pitch, via[0]
            )
            for cell in claimed[name]:
                use[cell] += 1
            _far_take(grid, name, net.klass, paths[name], tune.pitch, via[0])
        report["faulted"] += len(drop)
        tracks, vias, owner = _copper(
            grid, nets, paths, classes, foes, net_id, via, use, claimed
        )
        tracks, report["necked"] = _legalize(
            tracks, vias, shapes, gap, tune.slack, tune.min_track
        )
    report["legalize_s"] = round(time.perf_counter() - clock, 2)
    # Counted from the copper, not from the runs: a net is joined when its
    # pieces and runs make one piece, however many runs that took.
    report["connections"] = sum(len(n.pieces) - 1 for n in nets)
    nothing: set[int] = set()
    left = sum(
        len(
            _merged(
                [(set(p), nothing) for p in n.pieces]
                + [(set(run), nothing) for run in paths.get(n.name, [])]
            )
        )
        - 1
        for n in nets
    )
    report["routed"] = report["connections"] - left
    report["unreachable"] = sum(len(n.unreachable) for n in nets)
    # Which pins, by net and place, for reading why.
    report["unreachable_at"] = [
        f"{n.name} at {grid.at(*grid.split(next(iter(n.anchors[k])))[:2])}"
        for n in nets
        for k in sorted(n.unreachable)
    ][:20]
    report["dropped"] = left - report["unreachable"]
    return tracks, vias


# -- building the grid ------------------------------------------------------


def _pads(parts: Sequence[PlacedPart], index: LibraryIndex) -> list[_Shape]:
    shapes: list[_Shape] = []
    for part in parts:
        outline = read_outline(index, part.footprint)
        for number, box, layer in placed_pads(outline, part.x, part.y, part.rotation):
            shapes.append(
                _Shape(
                    part.pads.get(number, ""),
                    "*" if layer == "all" else layer,
                    box=box,
                )
            )
    return shapes


def _via_of(fixed: Sequence[Via]) -> tuple[float, float]:
    """The via most used on the board already, or KiCad's Default via."""
    sizes = [(v.size, v.drill) for v in fixed if v.size <= 1.0]
    return max(set(sizes), key=sizes.count) if sizes else (0.6, 0.3)


def _nets(
    grid: Grid,
    shapes: Sequence[_Shape],
    names: Sequence[str],
    class_of: Mapping[str, int],
    skip: set[str],
    classes: Sequence[NetClass],
    near: _Near,
    gap: Callable[[str, str], float],
    slack: float,
    min_track: float,
) -> list[_Net]:
    """Each net to route, with its copper as pieces of grid cells."""
    names_set = set(names)
    by_net: dict[str, list[tuple[set[int], set[int]]]] = {}
    escapes: dict[str, set[int]] = {}
    exacts: dict[str, dict[int, tuple[float, float]]] = {}
    necks: dict[str, dict[int, float]] = {}
    last = max(class_of.values(), default=0)
    for shape in shapes:
        if shape.net not in names_set or shape.net in skip:
            continue
        on = (
            range(len(grid.layers))
            if shape.layer == "*"
            else [grid.layers.index(shape.layer)]
            if shape.layer in grid.layers
            else []
        )
        cells = _cells(grid, shape, 0.0) or [
            grid.cell(*_centre(shape)),
        ]
        piece = {grid.index(i, j, layer) for layer in on for i, j in cells}
        if shape.box is not None:
            centre = _centre(shape)
            ci, cj = grid.cell(*centre)
            anchor = {grid.index(ci, cj, layer) for layer in on}
            klass = classes[class_of.get(shape.net, last)]
            left, top, right, bottom = shape.box
            across = right - left >= bottom - top
            true = exacts.setdefault(shape.net, {})
            for layer in on:
                strip = _escape(
                    grid,
                    shape.box,
                    klass.width + klass.clearance,
                    lambda x, y, layer=layer, klass=klass, net=shape.net: near.room(
                        (x, y),
                        klass.width / 2,
                        net,
                        grid.layers[layer],
                        lambda other, net=net: gap(net, other) + slack,
                    ),
                    min_track / 2,
                )
                escapes.setdefault(shape.net, set()).update(
                    grid.index(i, j, layer) for i, j, _ in strip
                )
                neck = necks.setdefault(shape.net, {})
                for i, j, half in strip:
                    x, y = grid.at(i, j)
                    cell = grid.index(i, j, layer)
                    true[cell] = (x, centre[1]) if across else (centre[0], y)
                    if half < klass.width / 2:
                        neck[cell] = min(
                            neck.get(cell, 1e9), math.floor(200 * half) / 100
                        )
                true[grid.index(ci, cj, layer)] = (
                    round(centre[0], 4),
                    round(centre[1], 4),
                )
        else:
            anchor = set(piece)
        if piece:
            by_net.setdefault(shape.net, []).append((piece, anchor))
    nets: list[_Net] = []
    for name in names:
        merged = _merged(by_net.get(name, []))
        if len(merged) > 1:
            nets.append(
                _Net(
                    name,
                    class_of.get(name, last),
                    [piece for piece, _ in merged],
                    [anchor for _, anchor in merged],
                    escape=escapes.get(name, set()),
                    exact=exacts.get(name, {}),
                    neck=necks.get(name, {}),
                )
            )
    return nets
