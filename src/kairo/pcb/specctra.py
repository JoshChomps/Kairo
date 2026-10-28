"""Specctra design (DSN) out, session (SES) back: a board for an autorouter.

Freerouting, like most autorouters, reads a board in Cadence's Specctra
design language and writes its routes as a Specctra session. Kairo writes
its own DSN from what it placed, and reads the session's wires and vias
back as tracks and vias for its board writer; KiCad's DRC then judges the
result. The router itself is GPL and runs only as a separate program.

The forms follow the Specctra design language as KiCad's own exporter and
Freerouting use it: lengths in micrometres at a resolution of ten per
micrometre, the y axis pointing up (so a board's y is negated), each
footprint an image of pins at its own unturned coordinates, each pad
shape a padstack, placed parts turned by their rotation, copper pours as
planes and keep-outs as keepouts.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from kairo.kicad.sexpr import Atom, SList, parse
from kairo.library.index import LibraryIndex
from kairo.pcb.board_writer import PlacedPart, Track, Via, Zone
from kairo.pcb.geometry import rotate

#: Specctra lengths: micrometres, ten steps to each.
_SCALE = 1000.0
#: The clearance between two SMD pads, in mm: a land pattern's own pads
#: may be closer than the board's clearance (a USB-C receptacle's are 0.2 mm
#: apart), so this rule, as KiCad's exporter writes it, lets the router
#: accept them as placed.
SMD_SMD = 0.05


@dataclass(frozen=True, slots=True)
class NetClass:
    """A routing class: its track width, clearance and member nets.

    Attributes:
        name: The class's name.
        width: Track width, in mm.
        clearance: Clearance to other copper, in mm.
        nets: Its nets, by KiCad's names.
        apart: Clearances to particular other classes, by their names,
            where a pair needs more than either class's own (the voltage
            between them, `kairo.pcb.rules.routing_classes`).
    """

    name: str
    width: float
    clearance: float
    nets: tuple[str, ...]
    apart: tuple[tuple[str, float], ...] = ()


def between(a: NetClass, b: NetClass) -> float:
    """The clearance two classes' copper keeps: the larger of their own, or the pair's.

    As KiCad's DRC keeps it: the larger of the two classes' clearances,
    and a rule for the pair where one is written.
    """
    pair = max(
        (gap for name, gap in a.apart if name == b.name),
        default=0.0,
    )
    pair = max(pair, max((gap for name, gap in b.apart if name == a.name), default=0.0))
    return max(a.clearance, b.clearance, pair)


def net_gaps(classes: Sequence[NetClass]) -> Callable[[str, str], float]:
    """The clearance between two nets' copper, by KiCad's names (`between`).

    A net in no class keeps the loosest class's clearance.
    """
    of = {net: c for c in classes for net in c.nets}
    loosest = min((c.clearance for c in classes), default=0.2)
    known: dict[tuple[str, str], float] = {}

    def gap(a: str, b: str) -> float:
        key = (a, b)
        if key not in known:
            ca, cb = of.get(a), of.get(b)
            if ca is not None and cb is not None:
                known[key] = between(ca, cb)
            else:
                known[key] = max(
                    ca.clearance if ca is not None else loosest,
                    cb.clearance if cb is not None else loosest,
                )
        return known[key]

    return gap


@dataclass(frozen=True, slots=True)
class _Pad:
    number: str
    x: float
    y: float
    stack: str


def write_dsn(
    name: str,
    parts: Sequence[PlacedPart],
    outline: Sequence[tuple[float, float]],
    index: LibraryIndex,
    *,
    layers: Sequence[str],
    classes: Sequence[NetClass],
    zones: Sequence[Zone] = (),
    via: tuple[float, float] = (0.6, 0.3),
    power_layers: Sequence[str] = (),
    edge_clearance: float = 0.5,
    fixed_vias: Sequence[Via] = (),
    fixed_tracks: Sequence[Track] = (),
    areas: Sequence[Zone] = (),
) -> str:
    """A Specctra design of a placed board, for an autorouter.

    Args:
        name: The board's name.
        parts: Every footprint, placed, with its pads' nets.
        outline: The board edge's corners, in mm.
        index: The library index, for each footprint's pads.
        layers: The copper layers, top to bottom.
        classes: The routing classes; every routed net must be in one.
        zones: Copper pours, given to the router as planes it can reach.
        via: The via's diameter and drill, in mm.
        power_layers: Layers that carry a whole plane, given as power
            layers so the router does not route signals across them.
        edge_clearance: Copper's least distance from the board edge, in
            mm; the router's boundary is drawn that far inside it.
        fixed_vias: Vias already decided, such as a pour's stitching,
            which the router keeps and routes around.
        fixed_tracks: Tracks already decided, such as a plane's fanout.
        areas: Copper already there, such as a pour's filled islands,
            given as fixed wires shaped as polygons. A router treats every
            plane of one net as one joined plane however far apart its
            pieces lie; an area is copper it must join to the rest.

    Returns:
        The DSN file's text.
    """
    images: dict[str, list[_Pad]] = {}
    stacks: dict[str, str] = {}
    for part in parts:
        image = _image_name(part.footprint)
        if image not in images:
            images[image], shapes = _footprint_pads(index, part.footprint, layers)
            stacks.update(shapes)
    # A footprint's keep-outs (an antenna's) go on the board, turned and
    # moved with their part.
    keepouts = _zone_keepouts(index, parts)
    via_name = f"Via[0-{len(layers) - 1}]_{_um(via[0])}:{_um(via[1])}_um"
    lines: list[str] = []
    out = lines.append
    out(f'(pcb "{name}.dsn"')
    out(
        '  (parser (string_quote ") (space_in_quoted_tokens on) '
        '(host_cad "Kairo") (host_version "1"))'
    )
    out("  (resolution um 10)")
    out("  (unit um)")
    out("  (structure")
    for i, layer in enumerate(layers):
        kind = "power" if layer in power_layers else "signal"
        out(f"    (layer {layer} (type {kind}) (property (index {i})))")
    left = min(x for x, _ in outline) + edge_clearance
    right = max(x for x, _ in outline) - edge_clearance
    top = min(y for _, y in outline) + edge_clearance
    bottom = max(y for _, y in outline) - edge_clearance
    inside = [(left, top), (right, top), (right, bottom), (left, bottom)]
    ring = " ".join(f"{_x(x)} {_y(y)}" for x, y in [*inside, inside[0]])
    out(f"    (boundary (path pcb 0 {ring}))")
    for zone in zones:
        pts = " ".join(f"{_x(x)} {_y(y)}" for x, y in [*zone.outline, zone.outline[0]])
        out(f'    (plane "{zone.net}" (polygon {zone.layer} 0 {pts}))')
    for keepout in keepouts:
        out(keepout)
    out(f'    (via "{via_name}")')
    default = min(classes, key=lambda c: c.width)
    out(
        f"    (rule (width {_um(default.width)}) (clearance {_um(default.clearance)}) "
        f"(clearance {_um(SMD_SMD)} (type smd_smd)))"
    )
    out("  )")
    out("  (placement")
    by_image: dict[str, list[PlacedPart]] = {}
    for part in parts:
        by_image.setdefault(_image_name(part.footprint), []).append(part)
    for image, members in by_image.items():
        out(f'    (component "{image}"')
        for part in members:
            out(
                f'      (place "{part.ref}" {_x(part.x)} {_y(part.y)} front '
                f"{_rotation(part.rotation)})"
            )
        out("    )")
    out("  )")
    out("  (library")
    for image, pads in images.items():
        out(f'    (image "{image}"')
        for pad in pads:
            out(f'      (pin "{pad.stack}" "{pad.number}" {_um(pad.x)} {_um(-pad.y)})')
        for x, y, across in _PEGS.get(image, []):
            out(f'      (keepout "" (circle signal {_um(across)} {_um(x)} {_um(-y)}))')
        out("    )")
    for stack, shape in stacks.items():
        out(f'    (padstack "{stack}"')
        out(shape)
        out("      (attach off)")
        out("    )")
    via_shapes = " ".join(f"(shape (circle {layer} {_um(via[0])}))" for layer in layers)
    out(f'    (padstack "{via_name}" {via_shapes} (attach off))')
    out("  )")
    out("  (network")
    nets: dict[str, list[str]] = {}
    for part in parts:
        pins = images[_image_name(part.footprint)]
        for number, net in part.pads.items():
            if net.startswith("unconnected-"):
                continue
            for pin in pins:
                if pin.number.split("@")[0] == number:
                    nets.setdefault(net, []).append(f'"{part.ref}"-"{pin.number}"')
    for net, pins in nets.items():
        out(f'    (net "{net}" (pins {" ".join(pins)}))')
    for net_class in classes:
        members = " ".join(f'"{n}"' for n in net_class.nets if n in nets)
        if not members:
            continue
        out(f'    (class "{net_class.name}" {members}')
        out(f'      (circuit (use_via "{via_name}"))')
        out(
            f"      (rule (width {_um(net_class.width)}) "
            f"(clearance {_um(net_class.clearance)}))"
        )
        out("    )")
    # Between two classes KiCad's DRC keeps the larger clearance; the
    # router is told so pair by pair, or it keeps its default between them.
    used = [c for c in classes if any(n in nets for n in c.nets)]
    for k, first in enumerate(used):
        for second in used[k + 1 :]:
            gap = between(first, second)
            out(
                f'    (class_class (classes "{first.name}" "{second.name}") '
                f"(rule (clearance {_um(gap)})))"
            )
    out("  )")
    out("  (wiring")
    for area in areas:
        pts = " ".join(f"{_x(x)} {_y(y)}" for x, y in [*area.outline, area.outline[0]])
        out(
            f"    (wire (polygon {area.layer} 0 {pts}) "
            f'(net "{area.net}") (type protect))'
        )
    for track in fixed_tracks:
        out(
            f"    (wire (path {track.layer} {_um(track.width)} "
            f"{_x(track.start[0])} {_y(track.start[1])} "
            f"{_x(track.end[0])} {_y(track.end[1])}) "
            f'(net "{track.net}") (type protect))'
        )
    for fixed in fixed_vias:
        out(
            f'    (via "{via_name}" {_x(fixed.at[0])} {_y(fixed.at[1])} '
            f'(net "{fixed.net}") (type protect))'
        )
    out("  )")
    out(")")
    return "\n".join(lines) + "\n"


def read_ses(text: str, min_width: float = 0.0) -> tuple[list[Track], list[Via]]:
    """The wires and vias of a Specctra session, in the board's mm.

    Args:
        text: The session file, as the router wrote it.
        min_width: The board maker's least track width, in mm. A router
            narrows a track to reach a small pad (neck-down); a narrower
            track is widened to this, and KiCad's DRC then says whether it
            still clears its neighbours.

    Returns:
        Each wire segment as a track and each via, with its net.
    """
    root = parse(text).root
    routes = root.first("routes")
    if routes is None:
        return [], []
    resolution = routes.first("resolution")
    per_um = float(resolution.values()[1]) if resolution is not None else 10.0
    scale = 1000.0 * per_um
    stacks: dict[str, tuple[float, float]] = {}
    library = routes.first("library_out")
    if library is not None:
        for stack in library.lists("padstack"):
            name = str(_value(stack.items[1]))
            shape = stack.first("shape")
            size = 0.0
            if shape is not None:
                circle = shape.first("circle")
                if circle is not None:
                    size = float(circle.values()[1]) / scale
            stacks[name] = (size, _drill_of(name))
    tracks: list[Track] = []
    vias: list[Via] = []
    network = routes.first("network_out")
    if network is None:
        return tracks, vias
    for net in network.lists("net"):
        name = str(_value(net.items[1]))
        for wire in net.lists("wire"):
            path = wire.first("path")
            if path is None:
                continue
            values = path.values()
            layer, width = str(values[0]), float(values[1]) / scale
            coords = [float(v) / scale for v in values[2:]]
            points = [(coords[i], -coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
            for start, end in itertools.pairwise(points):
                if start != end:
                    tracks.append(
                        Track(
                            _round(start),
                            _round(end),
                            round(max(width, min_width), 4),
                            layer,
                            name,
                        )
                    )
        for via in net.lists("via"):
            values = via.values()
            stack = str(values[0])
            x, y = float(values[1]) / scale, -float(values[2]) / scale
            size, drill = stacks.get(stack, (_size_of(stack), _drill_of(stack)))
            vias.append(Via(_round((x, y)), round(size, 4), round(drill, 4), name))
    return tracks, vias


def _value(item: object) -> str:
    return item.value if isinstance(item, Atom) else str(item)


def _size_of(stack: str) -> float:
    found = re.search(r"_(\d+):(\d+)_um", stack)
    return int(found.group(1)) / 1000 if found else 0.6


def _drill_of(stack: str) -> float:
    found = re.search(r"_(\d+):(\d+)_um", stack)
    return int(found.group(2)) / 1000 if found else 0.3


def _round(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], 4), round(point[1], 4))


def _image_name(footprint: str) -> str:
    return footprint.replace('"', "")


def _footprint_pads(
    index: LibraryIndex, footprint: str, layers: Sequence[str]
) -> tuple[list[_Pad], dict[str, str]]:
    """A footprint's pins and the padstacks their shapes need.

    Pads that share a number (a bolt terminal's ring of vias) are pins
    ``1``, ``1@1``, ``1@2`` and so on, as KiCad's own exporter names them,
    so each pin has a name of its own.
    """
    path = index.footprint_file(footprint)
    assert path is not None
    root = parse(Path(path).read_bytes().decode("utf-8")).root
    pads: list[_Pad] = []
    stacks: dict[str, str] = {}
    pegs: list[tuple[float, float, float]] = []
    for node in root.lists("pad"):
        number = _value(node.items[1])
        kind = _value(node.items[2])
        shape = _value(node.items[3])
        at = node.first("at")
        size = node.first("size")
        if at is None or size is None:
            continue
        values = at.values()
        x, y = float(values[0]), float(values[1])
        angle = float(values[2]) if len(values) > 2 else 0.0
        w, h = (float(v) for v in size.values()[:2])
        if kind == "np_thru_hole":
            # A peg's hole: nothing may be routed through it.
            drill = node.first("drill")
            across = float(drill.values()[0]) if drill is not None else 0.0
            pegs.append((x, y, max(across, w, h)))
            continue
        if not number:
            continue
        offset = (0.0, 0.0)
        if shape == "custom":
            w, h, *centre = _custom_extent(node, w, h)
            offset = (centre[0], centre[1])
            shape = "rect"
        on = _pad_layers(node, layers, kind)
        stack, text = _padstack(shape, w, h, angle, on, offset)
        stacks[stack] = text
        seen = sum(1 for pad in pads if pad.number.split("@")[0] == number)
        pads.append(_Pad(number if not seen else f"{number}@{seen}", x, y, stack))
    _PEGS[footprint] = pegs
    return pads, stacks


# The pegs of each footprint read by `_footprint_pads`: x, y and diameter.
_PEGS: dict[str, list[tuple[float, float, float]]] = {}


def _custom_extent(
    node: SList, w: float, h: float
) -> tuple[float, float, float, float]:
    """A custom pad's whole extent: its anchor and every primitive's points.

    Returns:
        The width and height of the box holding all of it, and the box's
        centre from the pad's anchor.
    """
    xs = [-w / 2, w / 2]
    ys = [-h / 2, h / 2]
    primitives = node.first("primitives")
    if primitives is not None:
        for item in primitives.items:
            if not isinstance(item, SList):
                continue
            width = item.first("width")
            grow = float(width.values()[0]) / 2 if width is not None else 0.0
            points: list[tuple[float, float]] = []
            pts = item.first("pts")
            if pts is not None:
                points += [
                    (float(xy.values()[0]), float(xy.values()[1]))
                    for xy in pts.lists("xy")
                ]
            for key in ("start", "end", "center", "mid"):
                point = item.first(key)
                if point is not None:
                    points.append((float(point.values()[0]), float(point.values()[1])))
            for px, py in points:
                xs += [px - grow, px + grow]
                ys += [py - grow, py + grow]
    return (
        max(xs) - min(xs),
        max(ys) - min(ys),
        (max(xs) + min(xs)) / 2,
        (max(ys) + min(ys)) / 2,
    )


def _pad_layers(node: SList, layers: Sequence[str], kind: str) -> list[str]:
    if kind == "thru_hole":
        return list(layers)
    listed = node.first("layers")
    names = [str(v) for v in listed.values()] if listed is not None else []
    found = [layer for layer in layers if layer in names]
    if "*.Cu" in names:
        return list(layers)
    return found or [layers[0]]


def _padstack(
    shape: str,
    w: float,
    h: float,
    angle: float,
    on: list[str],
    offset: tuple[float, float] = (0.0, 0.0),
) -> tuple[str, str]:
    """A padstack's name and its shapes on each layer, unturned with its part.

    The name holds the layers: one shape on F.Cu and on B.Cu (a power
    package's drain, top and bottom) is two padstacks, not one.
    """
    where = "A" if len(on) > 1 else on[0].replace(".", "")
    if shape == "circle" and offset == (0.0, 0.0):
        name = f"Round[{where}]Pad_{_um(w)}_um"
        body = " ".join(f"(shape (circle {layer} {_um(w)}))" for layer in on)
        return name, f"      {body}"
    # Rectangles, rounded rectangles, ovals and custom shapes are given as
    # their bounding rectangle, turned by the pad's own angle: a router
    # then keeps its clearance from the pad's whole extent.
    ox, oy = offset
    corners = [
        (ox - w / 2, oy - h / 2),
        (ox + w / 2, oy - h / 2),
        (ox + w / 2, oy + h / 2),
        (ox - w / 2, oy + h / 2),
    ]
    turned = [rotate(x, y, angle) for x, y in corners]
    name = (
        f"Rect[{where}]Pad_{_um(w)}x{_um(h)}_{round(angle) % 360}"
        f"_{_um(ox)}_{_um(oy)}_um"
    )
    pts = " ".join(f"{_um(x)} {_um(-y)}" for x, y in [*turned, turned[0]])
    body = " ".join(f"(shape (polygon {layer} 0 {pts}))" for layer in on)
    return name, f"      {body}"


def _zone_keepouts(index: LibraryIndex, parts: Sequence[PlacedPart]) -> list[str]:
    """Each footprint's keep-out zone as a DSN keepout."""
    found: list[str] = []
    for ref, corners in keepout_polygons(index, parts):
        ring = " ".join(f"{_x(x)} {_y(y)}" for x, y in [*corners, corners[0]])
        found.append(f'    (keepout "{ref}" (polygon signal 0 {ring}))')
    return found


def keepout_polygons(
    index: LibraryIndex, parts: Sequence[PlacedPart]
) -> list[tuple[str, list[tuple[float, float]]]]:
    """Each footprint's keep-out zone, turned and moved onto the board.

    Args:
        index: The library index, for each footprint's file.
        parts: The placed parts.

    Returns:
        Each keep-out's part reference and corners, in board mm.
    """
    found: list[tuple[str, list[tuple[float, float]]]] = []
    for part in parts:
        path = index.footprint_file(part.footprint)
        if path is None:
            continue
        root = parse(Path(path).read_bytes().decode("utf-8")).root
        for zone in root.lists("zone"):
            if zone.first("keepout") is None:
                continue
            polygon = zone.first("polygon")
            pts = polygon.first("pts") if polygon is not None else None
            if pts is None:
                continue
            corners: list[tuple[float, float]] = []
            for xy in pts.lists("xy"):
                px, py = (float(v) for v in xy.values()[:2])
                dx, dy = rotate(px, py, part.rotation)
                corners.append((part.x + dx, part.y + dy))
            found.append((part.ref, corners))
    return found


def _rotation(turn: float) -> int:
    return round(turn) % 360


def _um(mm: float) -> int | float:
    value = round(mm * _SCALE, 1)
    return int(value) if value == int(value) else value


def _x(mm: float) -> int | float:
    return _um(mm)


def _y(mm: float) -> int | float:
    return _um(-mm)


def classes_by_name(
    classes: Mapping[str, tuple[float, float, Sequence[str]]],
) -> list[NetClass]:
    """Routing classes from a mapping of name to width, clearance and nets."""
    return [
        NetClass(name, width, clearance, tuple(nets))
        for name, (width, clearance, nets) in classes.items()
    ]
