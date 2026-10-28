"""Where each footprint goes on a board, from constraints rather than coordinates.

A design never gives a part's position. It says how big the board is,
which side of it each block belongs on (a three-by-three compass: ``west``,
``center``, ``east`` across, ``north``, ``middle``, ``south`` down, joined
as ``north-west`` and so on, or one word for a whole column or row), and
which parts sit on which edge (a connector, the antenna end of a radio
module, a bolt terminal). This module turns that into positions:

0. Parts are placed with their job in mind (the KiStack placement
   skill's reasoning, built here from Kairo's own terms): a fine-pitch
   IC keeps `FRONT` millimetres in front of each pin free of any part
   not on that pin's net, so every pin keeps a way out for its track.
   How near each local part sits to the IC pin it serves is measured
   (`placement_report`), not pulled: a pull strong enough to seat each
   decoupler at its pin bent the whole placement round the ICs, and the
   board routed worse (docs/router-log.md, finding 24).
1. Each block's region is its compass cell or cells. The cells are not
   fixed thirds: a column is as wide, and a cell within it as tall, as the
   parts in it need (`floorplan`), as a designer sizes a floorplan by area
   before placing anything. Parts start packed in shelves inside their
   region, largest first, so the first placement is already legal: no two
   courtyards, each grown by the clearance, overlap, and none crosses the
   board's edge.
2. Simulated annealing (Kirkpatrick, Gelatt and Vecchi, Science 220, 1983;
   as TimberWolf placed cells, Sechen and Sangiovanni-Vincentelli, IEEE
   JSSC 20, 1985) then moves, swaps and turns parts to shorten the nets:
   the cost is each net's half-perimeter wirelength, weighted (a pour's net
   counts more, a ground less since a plane takes it), plus a penalty for
   each millimetre a part strays from its block's region. A move that
   makes two courtyards overlap or leaves the board is never taken.
3. A part held to an edge stays against it, turned so its outward side
   faces out: the side where its courtyard reaches furthest past its pads
   (`facing`: a radio module's antenna, a receptacle's mouth, whichever
   way the library draws it); it slides only along the edge.
   Its body (the fabrication outline) stands flush with the edge unless its
   copper would then come closer to the edge than the clearance; courtyard
   beyond the edge, such as a radio module's antenna keepout, hangs off
   the board, as the module maker asks.

Every random choice is seeded from the parts' names and shapes, so the same
design is placed the same way every time.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kairo.kicad.sexpr import SList, parse
from kairo.library.index import LibraryIndex
from kairo.pcb.geometry import rotate

Rect = tuple[float, float, float, float]

#: The compass words a block's side may use, as fractions of the board.
_ACROSS = {"west": (0.0, 1 / 3), "center": (1 / 3, 2 / 3), "east": (2 / 3, 1.0)}
_DOWN = {"north": (0.0, 1 / 3), "middle": (1 / 3, 2 / 3), "south": (2 / 3, 1.0)}
#: The turn that makes a footprint's library top face each edge.
_FACING = {"north": 0.0, "west": 90.0, "south": 180.0, "east": 270.0}


class PlacementError(ValueError):
    """The parts cannot be placed as asked."""


@dataclass(frozen=True, slots=True)
class Outline:
    """A footprint's room and pads, read from its library file.

    Attributes:
        courtyard: Its courtyard's box about its origin, unturned, in mm.
        pads: Each pad's number and centre about the origin, unturned.
        sizes: Each pad's size as a box about its centre, unturned, in the
            order of ``pads``: its width and height with its own angle.
        layers: Each pad's copper: ``all`` for a through pad, the one
            layer it is on, or empty for a pad on no copper layer (a paste
            aperture), in the order of ``pads``.
        body: Its fabrication outline's box (F.Fab), unturned, or None
            where the footprint draws none.
        copper: Its pads' box, unturned, or None where it has no pads.
        silk: The boxes of its top silkscreen drawing (lines, arcs,
            shapes, its stroke included), unturned; its texts left out.
    """

    courtyard: Rect
    pads: tuple[tuple[str, float, float], ...]
    sizes: tuple[tuple[float, float], ...] = ()
    layers: tuple[str, ...] = ()
    body: Rect | None = None
    copper: Rect | None = None
    silk: tuple[Rect, ...] = ()


#: The turn that brings a footprint's side, as the library draws it, to its
#: top: a receptacle drawn mouth down is turned half round first.
_TO_TOP = {"north": 0.0, "south": 180.0, "west": 270.0, "east": 90.0}


def facing(outline: Outline) -> str:
    """The side of a footprint, as the library draws it, that faces out from an edge.

    It is the side where the courtyard reaches furthest past the pads: a
    radio module's antenna, a receptacle's mouth, a terminal's wire
    entry. KiCad's libraries draw these either way (the ESP32 module's
    antenna up, the USB-C receptacle's mouth down), so the side is read
    from the shape, not assumed. A part that overhangs alike on every
    side faces out with its top.

    Returns:
        ``north``, ``south``, ``west`` or ``east``, in the library's frame.
    """
    if outline.copper is None:
        return "north"
    cl, ct, cr, cb = outline.courtyard
    pl, pt, pr, pb = outline.copper
    overhang = {
        "north": pt - ct,
        "south": cb - pb,
        "west": pl - cl,
        "east": cr - pr,
    }
    best = max(overhang.values())
    # A clear lead only: within a millimetre of another side is a tie.
    if best - sorted(overhang.values())[-2] < 1.0 or overhang["north"] == best:
        return "north"
    return max(overhang, key=lambda side: overhang[side])


def _reach(rect: Rect, side: str) -> float:
    """How far a box reaches from the origin toward one side, in the library's frame."""
    left, top, right, bottom = rect
    return {"north": -top, "south": bottom, "west": -left, "east": right}[side]


#: Positions are kept on this grid, in mm.
STEP = 0.25
#: Moves tried for each part.
MOVES_PER_PART = 3000
#: What a millimetre outside its region costs a part.
STRAY = 2.0
#: A local part: at most this many pads (a capacitor, a resistor, a
#: crystal); an IC: at least this many.
LOCAL_PADS = 4
IC_PADS = 8
#: A fine-pitch IC (pins this close or closer, in mm) keeps the ground in
#: front of each pin, this deep, free of parts not on that pin's net.
FINE_PITCH = 0.65
FRONT = 2.0


def read_outline(index: LibraryIndex, footprint: str) -> Outline:
    """The courtyard box and pad centres of a library footprint.

    A footprint with no courtyard is given its pads' box grown by 0.25 mm,
    which is KiCad's own courtyard rule for parts that lack one.
    """
    path = index.footprint_file(footprint)
    if path is None:
        raise PlacementError(f"footprint {footprint} is not in the libraries")
    root = parse(Path(path).read_bytes().decode("utf-8")).root
    xs: list[float] = []
    ys: list[float] = []
    pads: list[tuple[str, float, float]] = []
    sizes: list[tuple[float, float]] = []
    copper: list[str] = []
    metal: list[tuple[float, float]] = []
    fab: list[tuple[float, float]] = []
    silk: list[Rect] = []
    for node in root.items:
        if not isinstance(node, SList):
            continue
        if node.head == "pad":
            at = node.first("at")
            size = node.first("size")
            if at is None:
                continue
            px, py = float(at.values()[0]), float(at.values()[1])
            kind = str(node.items[2].value) if hasattr(node.items[2], "value") else ""  # type: ignore[union-attr]
            listed = node.first("layers")
            names = [str(v) for v in listed.values()] if listed is not None else []
            # A pad on no copper layer (a power package's paste aperture,
            # drawn over its pad) is not copper: its layer is empty.
            on = (
                "all"
                if kind == "thru_hole" or "*.Cu" in names
                else next((n for n in names if n.endswith(".Cu")), "")
            )
            number = str(node.items[1].value) if hasattr(node.items[1], "value") else ""  # type: ignore[union-attr]
            pads.append((number, px, py))
            copper.append(on)
            w = h = 0.0
            if size is not None:
                w, h = (float(v) for v in size.values()[:2])
                if len(node.items) > 3 and str(getattr(node.items[3], "value", "")) == (
                    "custom"
                ):
                    w, h, ox, oy = _custom_box(node, w, h)
                    px, py = px + ox, py + oy
                    pads[-1] = (number, px, py)
                angle = float(at.values()[2]) if len(at.values()) > 2 else 0.0
                if round(angle) % 180 == 90:
                    w, h = h, w
                xs += [px - w / 2, px + w / 2]
                ys += [py - h / 2, py + h / 2]
                metal += [(px - w / 2, py - h / 2), (px + w / 2, py + h / 2)]
            sizes.append((w, h))
            continue
        layer = node.first("layer")
        if layer is None:
            continue
        name = str(layer.values()[0])
        if "CrtYd" in name:
            for x, y in _points(node):
                xs.append(x)
                ys.append(y)
        elif name == "F.Fab":
            fab += _points(node)
        elif name in ("F.SilkS", "F.Silkscreen") and node.head not in (
            "property",
            "fp_text",
        ):
            found = _box(_points(node))
            if found is not None:
                stroke = node.first("stroke")
                width = stroke.first("width") if stroke is not None else None
                half = float(width.values()[0]) / 2 if width is not None else 0.06
                silk.append(
                    (found[0] - half, found[1] - half, found[2] + half, found[3] + half)
                )
    if not xs:
        raise PlacementError(f"footprint {footprint} has neither courtyard nor pads")
    return Outline(
        (min(xs), min(ys), max(xs), max(ys)),
        tuple(pads),
        tuple(sizes),
        tuple(copper),
        _box(fab),
        _box(metal),
        tuple(silk),
    )


def _custom_box(node: SList, w: float, h: float) -> tuple[float, float, float, float]:
    """A custom pad's box: its anchor and every drawn primitive.

    A power package's drain pad is drawn as polygons reaching past its
    anchor rectangle (the TOLL's runs 5.15 mm one way and 2.85 mm the
    other). Its box is where that copper is, and the pad's centre is
    taken as the box's.

    Returns:
        The box's width and height, and its centre's offset from the
        anchor, unturned.
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
            for x, y in _points(item):
                xs += [x - grow, x + grow]
                ys += [y - grow, y + grow]
    return (
        max(xs) - min(xs),
        max(ys) - min(ys),
        (max(xs) + min(xs)) / 2,
        (max(ys) + min(ys)) / 2,
    )


def _points(node: SList) -> list[tuple[float, float]]:
    """The points that bound a drawn line, arc, circle, rectangle or polygon."""
    points: list[tuple[float, float]] = []
    for key in ("start", "end", "center", "mid"):
        point = node.first(key)
        if point is not None:
            points.append((float(point.values()[0]), float(point.values()[1])))
    if node.head == "fp_circle":
        centre, end = node.first("center"), node.first("end")
        if centre is not None and end is not None:
            cx, cy = (float(v) for v in centre.values()[:2])
            ex, ey = (float(v) for v in end.values()[:2])
            r = math.hypot(ex - cx, ey - cy)
            points += [(cx - r, cy - r), (cx + r, cy + r)]
    pts = node.first("pts")
    if pts is not None:
        for xy in pts.lists("xy"):
            points.append((float(xy.values()[0]), float(xy.values()[1])))
    return points


def _box(points: Sequence[tuple[float, float]]) -> Rect | None:
    if not points:
        return None
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


def placed_pads(
    outline: Outline, x: float, y: float, turn: float
) -> list[tuple[str, Rect, str]]:
    """A placed footprint's copper pads: number, box on the board, layer.

    The layer is ``all`` for a through pad; a pad on no copper layer (a
    paste aperture) is left out.
    """
    out: list[tuple[str, Rect, str]] = []
    for k, (number, px, py) in enumerate(outline.pads):
        layer = outline.layers[k] if k < len(outline.layers) else "F.Cu"
        if not layer:
            continue
        w, h = outline.sizes[k] if k < len(outline.sizes) else (0.0, 0.0)
        if round(turn) % 180 == 90:
            w, h = h, w
        dx, dy = rotate(px, py, turn)
        cx, cy = x + dx, y + dy
        out.append((number, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), layer))
    return out


def turned(rect: Rect, turn: float) -> Rect:
    """A box about the origin turned as KiCad turns a footprint."""
    corners = [
        rotate(x, y, turn) for x in (rect[0], rect[2]) for y in (rect[1], rect[3])
    ]
    return (
        min(c[0] for c in corners),
        min(c[1] for c in corners),
        max(c[0] for c in corners),
        max(c[1] for c in corners),
    )


@dataclass(frozen=True, slots=True)
class Item:
    """A part to place.

    Attributes:
        ref: Its reference.
        outline: Its courtyard and pads.
        block: The block it belongs to.
        side: Its block's compass side, such as ``north-east``.
        edge: The board edge it must stand against, if any.
        pads: Each pad's net, by number.
        margin: Room kept around this part's courtyard, in mm, when it
            needs more than the board's clearance gives: a power part,
            whose pours and stitching vias run between it and its
            neighbours.
    """

    ref: str
    outline: Outline
    block: str
    side: str
    edge: str | None = None
    pads: Mapping[str, str] = field(default_factory=dict[str, str])
    margin: float | None = None


def region(side: str, width: float, height: float) -> Rect:
    """The part of the board a compass side names, in mm from its corner."""
    across, down = (0.0, 1.0), (0.0, 1.0)
    for word in side.split("-"):
        if word in _ACROSS:
            across = _ACROSS[word]
        elif word in _DOWN:
            down = _DOWN[word]
        else:
            raise PlacementError(
                f"side {side!r}: {word!r} is not one of {sorted([*_ACROSS, *_DOWN])}"
            )
    return (across[0] * width, down[0] * height, across[1] * width, down[1] * height)


#: The least share of the board's width or a column's height a cell gets,
#: so that a column with little in it still has room for wiring.
FLOOR = 0.15


def floorplan(
    items: Sequence[Item], width: float, height: float, clearance: float
) -> dict[str, Rect]:
    """Each compass side's region, its cells sized by the parts they hold.

    A column's width is the board's width shared out in proportion to the
    area its parts need (a courtyard grown by its margin, and only the part
    of an edge part's courtyard that lies on the board), with no column
    under `FLOOR` of the width; within each column, its cells' heights are
    shared out the same way. A side naming one word spans the other
    direction whole, and its parts' need is split evenly across the cells
    it spans. A side's region is the box over its cells.

    Args:
        items: Every part, with its block's side.
        width: The board's width, in mm.
        height: The board's height, in mm.
        clearance: Room kept between courtyards, in mm: half on each.

    Returns:
        Each side's region, in mm from the board's corner.

    Raises:
        PlacementError: If a side uses a word the compass does not know.
    """
    columns = list(_ACROSS)
    rows = list(_DOWN)
    need = {(c, r): 0.0 for c in columns for r in rows}
    for item in items:
        across, down = _cells(item.side)
        share = _need(item, clearance) / (len(across) * len(down))
        for c in across:
            for r in down:
                need[(c, r)] += share
    lefts = _shares([sum(need[(c, r)] for r in rows) for c in columns], width)
    tops = {c: _shares([need[(c, r)] for r in rows], height) for c in columns}
    plan: dict[str, Rect] = {}
    for side in {item.side for item in items}:
        across, down = _cells(side)
        ks = [columns.index(c) for c in across]
        left, right = lefts[min(ks)], lefts[max(ks) + 1]
        top = min(tops[c][rows.index(r)] for c in across for r in down)
        bottom = max(tops[c][rows.index(r) + 1] for c in across for r in down)
        plan[side] = (left, top, right, bottom)
    return plan


def _cells(side: str) -> tuple[list[str], list[str]]:
    """The columns and rows a compass side spans."""
    across: list[str] = []
    down: list[str] = []
    for word in side.split("-"):
        if word in _ACROSS:
            across.append(word)
        elif word in _DOWN:
            down.append(word)
        else:
            raise PlacementError(
                f"side {side!r}: {word!r} is not one of {sorted([*_ACROSS, *_DOWN])}"
            )
    return across or list(_ACROSS), down or list(_DOWN)


def _need(item: Item, clearance: float) -> float:
    """The board area a part takes: its courtyard grown by its margin."""
    m = item.margin if item.margin is not None else clearance / 2
    left, top, right, bottom = item.outline.courtyard
    if item.edge is not None and item.outline.body is not None:
        # What lies beyond the body on the side that faces out hangs off
        # the board.
        side = facing(item.outline)
        body = item.outline.body
        if side == "north":
            top = max(top, body[1])
        elif side == "south":
            bottom = min(bottom, body[3])
        elif side == "west":
            left = max(left, body[0])
        else:
            right = min(right, body[2])
    return (right - left + 2 * m) * (bottom - top + 2 * m)


def _shares(needs: Sequence[float], length: float) -> list[float]:
    """Cut points along a length, each piece in proportion to its need."""
    total = sum(needs)
    spare = 1 - FLOOR * len(needs)
    cuts = [0.0]
    for need in needs:
        part = need / total if total else 1 / len(needs)
        cuts.append(cuts[-1] + length * (FLOOR + spare * part))
    cuts[-1] = length
    return cuts


@dataclass(slots=True)
class _Part:
    item: Item
    x: float = 0.0
    y: float = 0.0
    turn: float = 0.0
    rect: Rect = (0.0, 0.0, 0.0, 0.0)


def place(
    items: Sequence[Item],
    width: float,
    height: float,
    *,
    clearance: float = 0.5,
    weights: Mapping[str, float] | None = None,
    effort: float = 1.0,
    seat: bool = True,
) -> dict[str, tuple[float, float, float]]:
    """Place parts on a board of the given size.

    Each IC's own support parts (`satellites`) are left out of the anneal:
    the IC keeps room round it for them, and their other nets pull on it
    from the pins they serve. Then each is seated as near its pin as it
    fits (`_seat`), as a designer places an IC's capacitors and resistors
    at its pins before anything else.

    Args:
        items: Every part, with its block's side and any edge.
        width: The board's width, in mm.
        height: The board's height, in mm.
        clearance: Room kept between courtyards and from the board's edge,
            in mm: half on each courtyard.
        weights: A net's weight in the wirelength, 1 if not given.
        effort: A multiplier on the moves tried.
        seat: Whether to seat support parts at their ICs' pins; False
            anneals every part alike.

    Returns:
        Each part's x and y from the board's top-left corner, in mm, and
        its turn in degrees.

    Raises:
        PlacementError: If a region cannot hold its parts.
    """
    weights = weights or {}
    # A bank's members stand in a row as one; seating parts at one member
    # would make it unlike the rest, and the row would not form.
    banked = {
        ref for rows in _banked(items, clearance)[1].values() for ref, _, _ in rows
    }
    bound = (
        {r: s for r, s in satellites(items, weights).items() if s.ic not in banked}
        if seat
        else {}
    )
    if not bound:
        return _anneal(items, width, height, clearance, weights, effort)
    first = _with_halos(items, bound, clearance)
    placed = _anneal(first, width, height, clearance, weights, effort)
    return _seat(items, placed, bound, width, height, clearance, weights)


@dataclass(frozen=True, slots=True)
class Satellite:
    """A local part that serves one IC.

    Attributes:
        ic: The IC's reference.
        pins: The IC's pins on the nets the part shares with it.
        score: How specific the tie is: each shared net counts one over
            the number of other parts on it.
    """

    ic: str
    pins: tuple[str, ...]
    score: float


def satellites(
    items: Sequence[Item], weights: Mapping[str, float] | None = None
) -> dict[str, Satellite]:
    """The IC each local part serves, where it serves one above the others.

    A local part (at most `LOCAL_PADS` pads, on no edge) serves an IC (at
    least `IC_PADS` pads, on no edge) of its own block that it shares a
    net with; a net shared by fewer parts ties more strongly (a decoupler
    alone on its pin's net belongs at that pin; a pull-up on a bus
    belongs nowhere in particular). A net the placer weighs lightly (a
    plane's, such as ground) ties nothing. A part tied equally to two ICs
    serves neither. The block is the designer's word: a thermistor wired
    only to the monitor IC belongs on the FET it measures, and its block
    says so where its nets cannot.

    Returns:
        Each such part's IC, the IC's pins it shares, and the tie's strength.
    """
    weights = weights or {}
    members: dict[str, set[str]] = {}
    for item in items:
        for net in item.pads.values():
            members.setdefault(net, set()).add(item.ref)
    ics = [
        item
        for item in items
        if len(item.outline.pads) >= IC_PADS and item.edge is None
    ]
    out: dict[str, Satellite] = {}
    for item in items:
        if len(item.outline.pads) > LOCAL_PADS or item.edge is not None:
            continue
        nets = {
            net
            for net in item.pads.values()
            if not net.startswith("unconnected-") and weights.get(net, 1.0) >= 0.1
        }
        ties: list[tuple[float, str, tuple[str, ...]]] = []
        for ic in ics:
            if ic.block != item.block:
                continue
            shared = [n for n in nets if n in ic.pads.values()]
            if not shared:
                continue
            score = sum(1 / max(1, len(members[n]) - 1) for n in shared)
            pins = tuple(sorted(num for num, n in ic.pads.items() if n in shared))
            ties.append((score, ic.ref, pins))
        ties.sort(key=lambda t: (-t[0], t[1]))
        if not ties or (len(ties) > 1 and ties[1][0] >= ties[0][0] - 1e-9):
            continue
        score, ic, pins = ties[0]
        out[item.ref] = Satellite(ic, pins, round(score, 6))
    return out


@dataclass(frozen=True, slots=True)
class Stray:
    """A local part that serves one IC only, placed in another block.

    Attributes:
        ref: The part.
        block: Its block.
        ic: The IC every one of its nets reaches.
        ic_block: The IC's block.
    """

    ref: str
    block: str
    ic: str
    ic_block: str


def strays(
    items: Sequence[Item],
    weights: Mapping[str, float] | None = None,
    rails: Collection[str] = (),
) -> list[Stray]:
    """Local parts that serve only an IC of another block.

    Such a part (at most `LOCAL_PADS` pads, on no edge) has every net it
    has (ground and planes aside, as in `satellites`) on one IC's pins,
    and no other IC on any of them: a filter capacitor across two of a
    monitor's inputs, a decoupler on a regulator's pin. In the IC's own
    block it would be seated at those pins; in another it is placed with
    that block, and its tracks cross the board to the IC. It may belong
    where it is (a thermistor on the FET it measures), which is the
    designer's call: this only finds them. A supply rail ties nothing
    either: a buck's output capacitor on +5V serves the rail, not
    whichever IC happens to be the only large one on it.

    Args:
        items: Every part, with its block.
        weights: The nets' weights, as `place` takes them.
        rails: Nets that are supply rails or ground, by name.

    Returns:
        Each such part, in reference order.
    """
    weights = weights or {}
    # An IC on an edge (a radio module) is not seated round, but its nets
    # are still its own: a decoupler it shares serves more than one IC.
    ics = [item for item in items if len(item.outline.pads) >= IC_PADS]
    out: list[Stray] = []
    for item in items:
        if len(item.outline.pads) > LOCAL_PADS or item.edge is not None:
            continue
        nets = {
            net
            for net in item.pads.values()
            if not net.startswith("unconnected-")
            and weights.get(net, 1.0) >= 0.1
            and net not in rails
        }
        if not nets:
            continue
        touching = [ic for ic in ics if nets & set(ic.pads.values())]
        if len(touching) != 1:
            continue
        ic = touching[0]
        if ic.edge is None and nets <= set(ic.pads.values()) and ic.block != item.block:
            out.append(Stray(item.ref, item.block, ic.ref, ic.block))
    return sorted(out, key=lambda s: (s.ref.rstrip("0123456789"), _number(s.ref)))


def _number(ref: str) -> int:
    digits = ref[len(ref.rstrip("0123456789")) :]
    return int(digits) if digits else 0


#: The room an IC keeps round it for its support parts, as a multiple of
#: their area: they need room for their own tracks as well.
HALO = 2.0


def _with_halos(
    items: Sequence[Item], bound: Mapping[str, Satellite], clearance: float
) -> list[Item]:
    """The parts to anneal first: support parts out, their ICs grown to hold them.

    An IC's margin grows so the ring round it holds `HALO` times its
    support parts' area (each courtyard grown by the clearance). Each
    support part's nets that are not the IC's own become pads of the IC
    at the pin the part serves, so they pull on the IC from there.
    """
    by_ic: dict[str, list[Item]] = {}
    for item in items:
        if item.ref in bound:
            by_ic.setdefault(bound[item.ref].ic, []).append(item)
    out: list[Item] = []
    for item in items:
        if item.ref in bound:
            continue
        served = by_ic.get(item.ref)
        if not served:
            out.append(item)
            continue
        area = sum(_need(s, clearance) for s in served)
        m = item.margin if item.margin is not None else clearance / 2
        left, top, right, bottom = item.outline.courtyard
        span = (right - left + 2 * m) + (bottom - top + 2 * m)
        depth = (-span + math.sqrt(span * span + 4 * HALO * area)) / 4
        at = {number: (x, y) for number, x, y in item.outline.pads}
        own = set(item.pads.values())
        pads = list(item.outline.pads)
        nets = dict(item.pads)
        for s in served:
            pin = bound[s.ref].pins[0]
            x, y = at.get(pin, (0.0, 0.0))
            for number, net in s.pads.items():
                if net in own or net.startswith("unconnected-"):
                    continue
                key = f"{s.ref}{_JOIN}{number}"
                pads.append((key, x, y))
                nets[key] = net
        outline = Outline(
            item.outline.courtyard,
            tuple(pads),
            item.outline.sizes,
            item.outline.layers,
            item.outline.body,
            item.outline.copper,
            item.outline.silk,
        )
        out.append(
            Item(item.ref, outline, item.block, item.side, None, nets, m + depth)
        )
    return out


#: How far from its pin a support part is looked for before anywhere, in mm.
SEAT_REACH = 15.0
#: Spots compared for each support part: the nearest free ones.
SEAT_CHOICES = 24
_NOWHERE: Rect = (math.inf, math.inf, math.inf, math.inf)


def _seat(
    items: Sequence[Item],
    placed: Mapping[str, tuple[float, float, float]],
    bound: Mapping[str, Satellite],
    width: float,
    height: float,
    clearance: float,
    weights: Mapping[str, float],
) -> dict[str, tuple[float, float, float]]:
    """Seat each support part at its IC's pins, with every other part fixed.

    Parts go most specific first (a decoupler alone on its pin's net
    before a resistor on a bus). Each takes, of the `SEAT_CHOICES` free
    spots and turns nearest its pins, the one that makes its nets
    shortest; a free spot keeps clear of every courtyard and of other
    pins' fronts, as in the anneal.
    """
    parts = [_Part(item) for item in items]
    state = _State(
        parts,
        width,
        height,
        clearance / 2,
        weights,
        floorplan(items, width, height, clearance),
    )
    index = {p.item.ref: k for k, p in enumerate(parts)}
    waiting: set[int] = set()
    for k, p in enumerate(parts):
        if p.item.ref in bound:
            p.rect = _NOWHERE
            waiting.add(k)
        else:
            state._set(k, *placed[p.item.ref])  # pyright: ignore[reportPrivateUsage]
    offsets = _rings(SEAT_REACH)
    wide: list[tuple[float, float]] = []
    order = sorted(bound, key=lambda r: (bound[r].ic, -bound[r].score, r))
    for ref in order:
        k = index[ref]
        sat = bound[ref]
        ic = index[sat.ic]
        pins = [state.pad(ic, pin) for pin in sat.pins]
        tx = sum(x for x, _ in pins) / len(pins)
        ty = sum(y for _, y in pins) / len(pins)
        spot = _nearest_spot(state, k, (tx, ty), offsets, waiting)
        if spot is None:
            wide = wide or _rings(3 * SEAT_REACH)
            spot = _nearest_spot(state, k, (tx, ty), wide, waiting, 3 * SEAT_REACH)
        if spot is None:
            spot = state._free_spot(k, [0.0, 90.0])  # pyright: ignore[reportPrivateUsage]
        if spot is None:
            raise PlacementError(f"{ref} finds no room near {sat.ic} or anywhere")
        state._set(k, *spot)  # pyright: ignore[reportPrivateUsage]
        waiting.discard(k)
    return {p.item.ref: (round(p.x, 3), round(p.y, 3), p.turn % 360) for p in parts}


def _rings(reach: float) -> list[tuple[float, float]]:
    """Grid offsets within a reach, nearest first."""
    n = int(reach / STEP)
    spots = [
        (i * STEP, j * STEP)
        for i in range(-n, n + 1)
        for j in range(-n, n + 1)
        if (i * i + j * j) * STEP * STEP <= reach * reach
    ]
    spots.sort(key=lambda s: (s[0] * s[0] + s[1] * s[1], s))
    return spots


def _nearest_spot(
    state: _State,
    k: int,
    target: tuple[float, float],
    offsets: Sequence[tuple[float, float]],
    waiting: set[int],
    reach: float = SEAT_REACH,
) -> tuple[float, float, float] | None:
    """Of the free spots nearest a target, the one that makes a part's nets shortest."""
    item = state.parts[k].item
    # Only parts that could touch a spot within reach need checking.
    span = reach + 20
    near = [
        j
        for j, p in enumerate(state.parts)
        if j != k
        and j not in waiting
        and p.rect[0] < target[0] + span
        and p.rect[2] > target[0] - span
        and p.rect[1] < target[1] + span
        and p.rect[3] > target[1] - span
    ]
    # Each of its nets' extent over the parts already placed.
    spans: dict[str, tuple[float, float, float, float]] = {}
    for number, net in item.pads.items():
        if net not in state.nets:
            continue
        pts = [
            state.pad(j, n) for j, n in state.nets[net] if j != k and j not in waiting
        ]
        if pts:
            spans[number] = (
                min(x for x, _ in pts),
                min(y for _, y in pts),
                max(x for x, _ in pts),
                max(y for _, y in pts),
            )
    pads = state.pad_at[k]
    found: list[tuple[float, float, float, float]] = []
    bx, by = _snap(target[0]), _snap(target[1])
    for dx, dy in offsets:
        x, y = bx + dx, by + dy
        for turn in (0.0, 90.0, 180.0, 270.0):
            rect = state.box(k, x, y, turn)
            if not state.inside(rect) or any(
                _overlap(rect, state.parts[j].rect) for j in near
            ):
                continue
            if not state.fronts_clear(k, x, y, turn):
                continue
            cost = 0.0
            for number, (left, top, right, bottom) in spans.items():
                px, py = pads.get(number, (0.0, 0.0))
                ox, oy = rotate(px, py, turn)
                ax, ay = x + ox, y + oy
                cost += state.weights.get(item.pads[number], 1.0) * (
                    max(right, ax) - min(left, ax) + max(bottom, ay) - min(top, ay)
                )
            found.append((cost + math.hypot(dx, dy) * 0.01, x, y, turn))
        if len(found) >= SEAT_CHOICES:
            break
    if not found:
        return None
    _, x, y, turn = min(found)
    return (x, y, turn)


def _anneal(
    items: Sequence[Item],
    width: float,
    height: float,
    clearance: float,
    weights: Mapping[str, float],
    effort: float,
) -> dict[str, tuple[float, float, float]]:
    """Pack the parts in their regions, then anneal them; see the module's notes."""
    units, rows = _banked(items, clearance)
    parts = [_Part(item) for item in units]
    margin = clearance / 2
    seed = hashlib.sha256(
        "|".join(f"{p.item.ref}:{p.item.outline.courtyard}" for p in parts).encode()
    ).digest()
    rng = random.Random(seed)
    plan = floorplan(units, width, height, clearance)
    state = _State(parts, width, height, margin, weights, plan)
    state.start()
    state.anneal(rng, max(1, round(MOVES_PER_PART * effort * len(parts))))
    placed: dict[str, tuple[float, float, float]] = {}
    for p in parts:
        turn = p.turn % 360
        for ref, dx, dy in rows.get(p.item.ref, [(p.item.ref, 0.0, 0.0)]):
            ox, oy = rotate(dx, dy, turn)
            placed[ref] = (round(p.x + ox, 3), round(p.y + oy, 3), turn)
    return placed


#: The separator between a bank member's reference and its pad number.
_JOIN = "|"


def _banked(
    items: Sequence[Item], clearance: float
) -> tuple[list[Item], dict[str, list[tuple[str, float, float]]]]:
    """Parts in parallel made one row each, as a designer lays out a FET bank.

    A bank is two or more parts of one block with the same footprint whose
    pads, number by number, either all meet one net or each meet a net of
    their own, with at least one net shared: four FETs in parallel, drains
    together and sources together, each gate its own; eight TVS diodes
    across one rail. The members stand side by side, all turned alike, the
    row running across the line that divides their shared nets' pads, so
    each shared net's pads line up along one edge of the row, where a pour
    can take them all.

    Returns:
        The items to place, a bank as one item, and for each bank its
        members' offsets from its origin, unturned.
    """
    groups: dict[tuple[str, str, Outline, tuple[str, ...]], list[Item]] = {}
    for item in items:
        if item.edge is not None or not item.pads:
            continue
        key = (
            item.block,
            item.ref.rstrip("0123456789"),
            item.outline,
            tuple(sorted(item.pads)),
        )
        groups.setdefault(key, []).append(item)
    banks: list[list[Item]] = []
    for members in groups.values():
        # One group may hold several banks: back-to-back FETs share their
        # drains but not their sources, so each source net makes its own
        # row. Members go together when every net they meet that another
        # member also meets on that pad is the same.
        numbers = sorted(members[0].pads)
        meets = {n: Counter(m.pads[n] for m in members) for n in numbers}
        rows_of: dict[tuple[str, ...], list[Item]] = {}
        for m in members:
            sign = tuple(m.pads[n] if meets[n][m.pads[n]] > 1 else "" for n in numbers)
            rows_of.setdefault(sign, []).append(m)
        for sign, row in rows_of.items():
            if len(row) >= 2 and any(sign):
                banks.append(row)
    banked = {m.ref for bank in banks for m in bank}
    units: list[Item] = [item for item in items if item.ref not in banked]
    rows: dict[str, list[tuple[str, float, float]]] = {}
    for bank in banks:
        first = bank[0]
        left, top, right, bottom = first.outline.courtyard
        shared = sorted(
            {
                first.pads[n]
                for n in first.pads
                if all(m.pads[n] == first.pads[n] for m in bank)
            }
        )
        centres: list[tuple[float, float]] = []
        for net in shared:
            spots = [
                (x, y) for n, x, y in first.outline.pads if first.pads.get(n) == net
            ]
            centres.append(
                (
                    sum(x for x, _ in spots) / len(spots),
                    sum(y for _, y in spots) / len(spots),
                )
            )
        spread_x = max(c[0] for c in centres) - min(c[0] for c in centres)
        spread_y = max(c[1] for c in centres) - min(c[1] for c in centres)
        across = spread_y >= spread_x
        pitch = (right - left if across else bottom - top) + clearance
        offsets = [
            (m.ref, k * pitch if across else 0.0, 0.0 if across else k * pitch)
            for k, m in enumerate(bank)
        ]
        last = len(bank) - 1
        courtyard = (
            left,
            top,
            right + (last * pitch if across else 0.0),
            bottom + (0.0 if across else last * pitch),
        )
        pads: list[tuple[str, float, float]] = []
        nets: dict[str, str] = {}
        for (ref, dx, dy), member in zip(offsets, bank, strict=True):
            for number, x, y in member.outline.pads:
                key = f"{ref}{_JOIN}{number}"
                pads.append((key, x + dx, y + dy))
                if number in member.pads:
                    nets[key] = member.pads[number]
        name = f"bank:{','.join(m.ref for m in bank)}"
        units.append(
            Item(
                name,
                Outline(courtyard, tuple(pads)),
                first.block,
                first.side,
                None,
                nets,
                first.margin,
            )
        )
        rows[name] = offsets
    return units, rows


class _State:
    """The placement while it is improved: positions, occupancy and cost."""

    def __init__(
        self,
        parts: list[_Part],
        width: float,
        height: float,
        margin: float,
        weights: Mapping[str, float],
        plan: Mapping[str, Rect],
    ) -> None:
        self.parts = parts
        self.width = width
        self.height = height
        self.margin = margin
        self.regions = [plan[p.item.side] for p in parts]
        nets: dict[str, list[tuple[int, str]]] = {}
        for i, p in enumerate(parts):
            for number, net in p.item.pads.items():
                if not net.startswith("unconnected-"):
                    nets.setdefault(net, []).append((i, number))
        # A net on one part only joins nothing on the board.
        self.nets = {
            name: members
            for name, members in nets.items()
            if len({i for i, _ in members}) > 1
        }
        self.weights = {name: weights.get(name, 1.0) for name in self.nets}
        self.nets_of: list[list[str]] = [[] for _ in parts]
        for name, members in self.nets.items():
            for i in {i for i, _ in members}:
                self.nets_of[i].append(name)
        self.pad_at = [
            {number: (x, y) for number, x, y in p.item.outline.pads} for p in parts
        ]
        self.cost_of: dict[str, float] = {}
        self.fronts = {
            k: zones for k, p in enumerate(parts) if (zones := _fronts(p.item))
        }
        self.nets_on = [set(p.item.pads.values()) for p in parts]

    # -- geometry -------------------------------------------------------

    def box(self, i: int, x: float, y: float, turn: float) -> Rect:
        item = self.parts[i].item
        left, top, right, bottom = turned(item.outline.courtyard, turn)
        m = item.margin if item.margin is not None else self.margin
        rect = (x + left - m, y + top - m, x + right + m, y + bottom + m)
        # Courtyard beyond the edge an edge part stands on hangs off the board.
        if item.edge == "north":
            rect = (rect[0], max(rect[1], 0.0), rect[2], rect[3])
        elif item.edge == "south":
            rect = (rect[0], rect[1], rect[2], min(rect[3], self.height))
        elif item.edge == "west":
            rect = (max(rect[0], 0.0), rect[1], rect[2], rect[3])
        elif item.edge == "east":
            rect = (rect[0], rect[1], min(rect[2], self.width), rect[3])
        return rect

    def inside(self, rect: Rect) -> bool:
        return (
            rect[0] >= 0
            and rect[1] >= 0
            and rect[2] <= self.width
            and rect[3] <= self.height
        )

    def clear(self, i: int, rect: Rect, ignore: tuple[int, ...] = ()) -> bool:
        for j, other in enumerate(self.parts):
            if j == i or j in ignore:
                continue
            o = other.rect
            if rect[0] < o[2] and o[0] < rect[2] and rect[1] < o[3] and o[1] < rect[3]:
                return False
        return True

    def pad(self, i: int, number: str) -> tuple[float, float]:
        p = self.parts[i]
        x, y = self.pad_at[i].get(number, (0.0, 0.0))
        dx, dy = rotate(x, y, p.turn)
        return (p.x + dx, p.y + dy)

    # -- cost -----------------------------------------------------------

    def net_cost(self, name: str) -> float:
        points = [self.pad(i, number) for i, number in self.nets[name]]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return self.weights[name] * ((max(xs) - min(xs)) + (max(ys) - min(ys)))

    def stray(self, i: int) -> float:
        rect = self.parts[i].rect
        left, top, right, bottom = self.regions[i]
        cx = (rect[0] + rect[2]) / 2
        cy = (rect[1] + rect[3]) / 2
        dx = max(left - cx, 0.0, cx - right)
        dy = max(top - cy, 0.0, cy - bottom)
        return STRAY * (dx + dy)

    def fronts_clear(
        self, i: int, x: float, y: float, turn: float, ignore: tuple[int, ...] = ()
    ) -> bool:
        """Whether a part there keeps out of pin fronts, and keeps its own free."""
        rect = self.box(i, x, y, turn)
        nets = self.nets_on[i]
        for j, zones in self.fronts.items():
            if j == i or j in ignore:
                continue
            p = self.parts[j]
            for zone, net in zones:
                if net not in nets and _overlap(rect, _placed(zone, p.x, p.y, p.turn)):
                    return False
        for zone, net in self.fronts.get(i, ()):
            spot = _placed(zone, x, y, turn)
            for k, other in enumerate(self.parts):
                if k == i or k in ignore or net in self.nets_on[k]:
                    continue
                if _overlap(spot, other.rect):
                    return False
        return True

    def total(self) -> float:
        return sum(self.cost_of.values()) + sum(
            self.stray(i) for i in range(len(self.parts))
        )

    # -- building -------------------------------------------------------

    def start(self) -> None:
        """Pack each region's parts in shelves, edge parts first against their edge."""
        order = sorted(
            range(len(self.parts)),
            key=lambda i: (
                self.parts[i].item.edge is None,
                -_area(self.parts[i].item.outline.courtyard),
                self.parts[i].item.ref,
            ),
        )
        for i in order:
            item = self.parts[i].item
            turns = (
                [(_FACING[item.edge] + _TO_TOP[facing(item.outline)]) % 360]
                if item.edge
                else [0.0, 90.0]
            )
            spot = self._free_spot(i, turns)
            if spot is None:
                raise PlacementError(
                    f"{item.ref} ({item.block}) finds no room on the board; "
                    "give its block a larger side or the board more room"
                )
            x, y, turn = spot
            self._set(i, x, y, turn)
        for name in self.nets:
            self.cost_of[name] = self.net_cost(name)

    def _free_spot(
        self, i: int, turns: list[float]
    ) -> tuple[float, float, float] | None:
        """The first free spot in reading order: in its region, else anywhere."""
        item = self.parts[i].item
        left, top, right, bottom = self.regions[i]
        for area in ((left, top, right, bottom), (0.0, 0.0, self.width, self.height)):
            for turn in turns:
                box = turned(item.outline.courtyard, turn)
                for x, y in self._scan(area, box, item):
                    rect = self.box(i, x, y, turn)
                    if (
                        self.inside(rect)
                        and self.clear(i, rect)
                        and self.fronts_clear(i, x, y, turn)
                    ):
                        return (x, y, turn)
        return None

    def _scan(self, area: Rect, box: Rect, item: Item) -> list[tuple[float, float]]:
        """Candidate origins inside an area, on the grid, in reading order."""
        m = item.margin if item.margin is not None else self.margin
        edge = item.edge
        left, top, right, bottom = area
        xs = _steps(left - box[0] + m, right - box[2] - m)
        ys = _steps(top - box[1] + m, bottom - box[3] - m)
        if edge is None:
            return [(x, y) for y in ys for x in xs]
        # How far in the origin sits from the edge: the body flush with it,
        # or the copper the board's clearance in, whichever is further in;
        # a footprint that draws neither keeps its courtyard and margin in.
        outline = item.outline
        side = facing(outline)
        reach: list[float] = []
        if outline.body is not None:
            reach.append(_reach(outline.body, side))
        if outline.copper is not None:
            reach.append(_reach(outline.copper, side) + 2 * self.margin)
        inset = max(reach) if reach else _reach(outline.courtyard, side) + m
        # The part is turned so its outward side faces the edge, so the
        # inset is measured from that side whichever edge it is.
        if edge == "north":
            ys = [_snap(inset)]
        elif edge == "south":
            ys = [_snap_down(self.height - inset)]
        elif edge == "west":
            xs = [_snap(inset)]
        elif edge == "east":
            xs = [_snap_down(self.width - inset)]
        return [(x, y) for y in ys for x in xs]

    def _set(self, i: int, x: float, y: float, turn: float) -> None:
        p = self.parts[i]
        p.x, p.y, p.turn = x, y, turn
        p.rect = self.box(i, x, y, turn)

    # -- improving ------------------------------------------------------

    def anneal(self, rng: random.Random, moves: int) -> None:
        n = len(self.parts)
        span = max(self.width, self.height) / 4
        hot = max(1.0, sum(self.cost_of.values()) / max(1, len(self.cost_of)) / 4)
        cold = 0.01
        cooling = (cold / hot) ** (1 / moves)
        heat = hot
        for _ in range(moves):
            heat *= cooling
            i = rng.randrange(n)
            item = self.parts[i].item
            kind = rng.random()
            p = self.parts[i]
            if kind < 0.6:
                reach = max(STEP, span * heat / hot)
                dx = (
                    0.0 if item.edge in ("west", "east") else rng.uniform(-reach, reach)
                )
                dy = (
                    0.0
                    if item.edge in ("north", "south")
                    else rng.uniform(-reach, reach)
                )
                self._try(rng, heat, [(i, _snap(p.x + dx), _snap(p.y + dy), p.turn)])
            elif kind < 0.8 and item.edge is None:
                turn = (p.turn + rng.choice((90.0, 180.0, 270.0))) % 360
                self._try(rng, heat, [(i, p.x, p.y, turn)])
            elif kind >= 0.8:
                j = rng.randrange(n)
                other = self.parts[j]
                if j == i or item.edge or other.item.edge:
                    continue
                self._try(
                    rng,
                    heat,
                    [(i, other.x, other.y, p.turn), (j, p.x, p.y, other.turn)],
                )

    def _try(
        self,
        rng: random.Random,
        heat: float,
        moves: list[tuple[int, float, float, float]],
    ) -> None:
        moved = tuple(m[0] for m in moves)
        rects = [self.box(i, x, y, t) for i, x, y, t in moves]
        for (i, x, y, t), rect in zip(moves, rects, strict=True):
            if not self.inside(rect) or not self.clear(i, rect, moved):
                return
            if self.fronts and not self.fronts_clear(i, x, y, t, moved):
                return
        if len(rects) == 2:
            a, b = rects
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                return
        old = [(i, self.parts[i].x, self.parts[i].y, self.parts[i].turn) for i in moved]
        before = sum(self.stray(i) for i in moved)
        touched = {name for i in moved for name in self.nets_of[i]}
        before += sum(self.cost_of[name] for name in touched)
        for i, x, y, t in moves:
            self._set(i, x, y, t)
        after_nets = {name: self.net_cost(name) for name in touched}
        after = sum(self.stray(i) for i in moved) + sum(after_nets.values())
        change = after - before
        if change <= 0 or rng.random() < math.exp(-change / heat):
            self.cost_of.update(after_nets)
            return
        for i, x, y, t in old:
            self._set(i, x, y, t)


def placement_report(
    items: Sequence[Item],
    places: Mapping[str, tuple[float, float, float]],
    weights: Mapping[str, float] | None = None,
) -> dict[str, object]:
    """How near each local part sits to the IC pin it serves, as placed.

    Args:
        items: The parts, as `place` took them.
        places: Where they went.
        weights: The nets' weights, as `place` took them.

    Returns:
        ``pairs`` (local pads with a pin to serve), ``mean_mm`` and
        ``max_mm`` (pad-to-pin distance), and ``worst`` (the three
        furthest, as ``REF.PAD to IC: mm``).
    """
    parts = [_Part(item) for item in items if item.ref in places]
    for p in parts:
        p.x, p.y, p.turn = places[p.item.ref]
    pairs = _affinities(parts, dict(weights or {}))

    def at(k: int, number: str) -> tuple[float, float]:
        p = parts[k]
        x, y = next(
            ((px, py) for n, px, py in p.item.outline.pads if n == number), (0.0, 0.0)
        )
        dx, dy = rotate(x, y, p.turn)
        return (p.x + dx, p.y + dy)

    gaps = sorted(
        (
            min(math.dist(at(i, number), at(ic, pin)) for pin in pins),
            f"{parts[i].item.ref}.{number} to {parts[ic].item.ref}",
        )
        for i, number, ic, pins in pairs.values()
    )
    if not gaps:
        return {"pairs": 0}
    return {
        "pairs": len(gaps),
        "mean_mm": round(sum(g for g, _ in gaps) / len(gaps), 2),
        "max_mm": round(gaps[-1][0], 2),
        "worst": [f"{name}: {gap:.1f} mm" for gap, name in gaps[-3:][::-1]],
    }


def _affinities(
    parts: Sequence[_Part], weights: Mapping[str, float]
) -> dict[str, tuple[int, str, int, tuple[str, ...]]]:
    """Each local part's pull to the IC pins it serves.

    A local part (at most `LOCAL_PADS` pads) is pulled, by each of its pads
    on a net that also reaches an IC (at least `IC_PADS` pads), to the
    nearest of that IC's pins on the net. A net the placer weighs lightly
    (a plane's, such as ground) pulls nothing: the plane serves those
    pads wherever they are.

    Returns:
        By a key of its own: the part, its pad, the IC and the IC's pins.
    """
    ics = [k for k, p in enumerate(parts) if len(p.item.outline.pads) >= IC_PADS]
    out: dict[str, tuple[int, str, int, tuple[str, ...]]] = {}
    for i, p in enumerate(parts):
        if len(p.item.outline.pads) > LOCAL_PADS or p.item.edge is not None:
            continue
        for number, net in p.item.pads.items():
            if net.startswith("unconnected-") or weights.get(net, 1.0) < 0.1:
                continue
            for ic in ics:
                pins = tuple(
                    n for n, other in parts[ic].item.pads.items() if other == net
                )
                if pins:
                    out[f"@{p.item.ref}.{number}>{parts[ic].item.ref}"] = (
                        i,
                        number,
                        ic,
                        pins,
                    )
    return out


def _fronts(item: Item) -> list[tuple[Rect, str]]:
    """A fine-pitch IC's pin fronts: the strip off each pin's outer end, unturned.

    Pins closer together than `FINE_PITCH` make a part fine-pitch. Each
    connected pin's front is as wide as the pin and `FRONT` deep, outward
    from the part's centre along the pin's long axis. A large pad (an
    exposed pad, a tab) has no front.
    """
    outline = item.outline
    pads = [
        (number, x, y, size)
        for (number, x, y), size in zip(outline.pads, outline.sizes, strict=False)
    ]
    if len(pads) < IC_PADS:
        return []
    centres = [(x, y) for _, x, y, _ in pads]
    pitch = min(
        (
            math.hypot(ax - bx, ay - by)
            for k, (ax, ay) in enumerate(centres)
            for bx, by in centres[k + 1 :]
            if (ax, ay) != (bx, by)
        ),
        default=math.inf,
    )
    if pitch > FINE_PITCH + 1e-6:
        return []
    cl, ct, cr, cb = outline.courtyard
    mx, my = (cl + cr) / 2, (ct + cb) / 2
    zones: list[tuple[Rect, str]] = []
    for number, x, y, (w, h) in pads:
        net = item.pads.get(number, "")
        if not net or net.startswith("unconnected-") or (w > 1.0 and h > 1.0):
            continue
        dx, dy = x - mx, y - my
        if abs(dx) >= abs(dy):
            edge = x + w / 2 if dx > 0 else x - w / 2
            near, far = sorted((edge, edge + (FRONT if dx > 0 else -FRONT)))
            zones.append(((near, y - h / 2, far, y + h / 2), net))
        else:
            edge = y + h / 2 if dy > 0 else y - h / 2
            near, far = sorted((edge, edge + (FRONT if dy > 0 else -FRONT)))
            zones.append(((x - w / 2, near, x + w / 2, far), net))
    return zones


def _placed(rect: Rect, x: float, y: float, turn: float) -> Rect:
    """A box about a part's origin, turned and moved where the part stands."""
    left, top, right, bottom = turned(rect, turn)
    return (x + left, y + top, x + right, y + bottom)


def _overlap(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _area(rect: Rect) -> float:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


def _snap(value: float) -> float:
    return round(math.ceil(round(value / STEP, 6)) * STEP, 4)


def _snap_down(value: float) -> float:
    return round(math.floor(round(value / STEP, 6)) * STEP, 4)


def _steps(low: float, high: float) -> list[float]:
    start, end = _snap(low), _snap_down(high)
    count = round((end - start) / STEP) + 1
    # Coarser than the grid while looking for a first spot; the anneal
    # refines it.
    stride = 4
    return [round(start + k * STEP, 4) for k in range(0, max(count, 0), stride)]
