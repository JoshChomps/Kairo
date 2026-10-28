"""Write a KiCad 10 board file from placed footprints, copper and an outline.

The board is built from the same tokens KiCad 10.0.7 saves (its upgrade of
the pic_programmer and video demos):

- each part is its library footprint, copied whole from the ``.kicad_mod``
  file the library index holds, with its place (``at``), its reference and
  value, the path of the schematic symbol it belongs to (``path``,
  ``sheetname``, ``sheetfile``) and each pad's net named on the pad, as
  KiCad 10 names it (there is no net table);
- a pad's and a text's angle is written as KiCad stores it, the library's
  angle plus the footprint's turn; positions inside a footprint stay as
  the library has them;
- every item gets a uuid derived from its owner's name, so the same board
  written twice is the same file;
- the stackup is two or four copper layers, with the copper weights given.

What lands on the board, where, and on which nets is the caller's business
(`kairo.pcb.place`, `kairo.pcb.zones`); KiCad's DRC with its schematic
parity check judges the result. This module returns text: the transaction
module puts it on disk.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kairo import __version__
from kairo.kicad.emit import copy, lay_out, lst, sym
from kairo.kicad.sexpr import Atom, SList, parse, to_text
from kairo.library.index import LibraryIndex

#: The board format KiCad 10 writes.
FORMAT_VERSION = 20260206
#: Identifiers are derived from names under this namespace.
NAMESPACE = uuid.UUID("2b8e4f1a-7c3d-5e9b-a1f0-6d2c8b4e7a91")
#: One ounce of copper per square foot, in mm.
OUNCE = 0.035
#: KiCad's layer numbers for copper, outermost first (KiCad 10).
_INNER_IDS = {"In1.Cu": 4, "In2.Cu": 6}
#: The user layers every KiCad 10 board lists, with their numbers and names.
_USER_LAYERS: tuple[tuple[int, str, str], ...] = (
    (9, "F.Adhes", "F.Adhesive"),
    (11, "B.Adhes", "B.Adhesive"),
    (13, "F.Paste", ""),
    (15, "B.Paste", ""),
    (5, "F.SilkS", "F.Silkscreen"),
    (7, "B.SilkS", "B.Silkscreen"),
    (1, "F.Mask", ""),
    (3, "B.Mask", ""),
    (17, "Dwgs.User", "User.Drawings"),
    (19, "Cmts.User", "User.Comments"),
    (21, "Eco1.User", "User.Eco1"),
    (23, "Eco2.User", "User.Eco2"),
    (25, "Edge.Cuts", ""),
    (27, "Margin", ""),
    (31, "F.CrtYd", "F.Courtyard"),
    (29, "B.CrtYd", "B.Courtyard"),
    (35, "F.Fab", ""),
    (33, "B.Fab", ""),
)


class BoardError(ValueError):
    """A board cannot be written as asked."""


@dataclass(frozen=True, slots=True)
class Stackup:
    """The board's layers.

    Attributes:
        copper_layers: 2 or 4.
        outer_oz: Copper weight of the outer layers, in ounces.
        inner_oz: Copper weight of the inner layers, in ounces.
        thickness: The finished board's thickness, in mm.
    """

    copper_layers: int = 4
    outer_oz: float = 1.0
    inner_oz: float = 1.0
    thickness: float = 1.6

    @property
    def copper(self) -> tuple[str, ...]:
        """The copper layers' names, top to bottom."""
        if self.copper_layers == 2:
            return ("F.Cu", "B.Cu")
        if self.copper_layers == 4:
            return ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        raise BoardError(
            f"a board of {self.copper_layers} copper layers is not written"
        )


@dataclass(frozen=True, slots=True)
class PlacedPart:
    """A footprint on the board.

    Attributes:
        ref: Its reference.
        footprint: The library footprint, as ``Library:Name``.
        value: The value shown.
        x: Its origin's x, in mm.
        y: Its origin's y, in mm.
        rotation: Its turn, in degrees.
        path: The schematic symbol it is drawn from, as KiCad writes it:
            the sheet symbols' uuids then the symbol's, joined by ``/``.
        sheetname: The sheet's path, such as ``/power/``.
        sheetfile: The sheet's file name.
        pads: Each pad's net, by pad number.
        fields: The symbol's other fields the footprint carries, such as
            ``Datasheet`` and ``Description``, as KiCad's update from the
            schematic copies them.
        in_bom: Whether the symbol is in the bill of materials; the
            footprint's own flag follows it, as KiCad's update sets it.
        label: Where its reference prints (`kairo.pcb.silk`): its centre
            in the footprint's own frame, its angle on the board, its text
            size, and whether it is on the silkscreen or only the
            fabrication layer. None keeps the library's.
    """

    ref: str
    footprint: str
    value: str
    x: float
    y: float
    rotation: float
    path: str
    sheetname: str
    sheetfile: str
    pads: Mapping[str, str] = field(default_factory=dict[str, str])
    fields: Mapping[str, str] = field(default_factory=dict[str, str])
    in_bom: bool = True
    label: tuple[float, float, float, float, bool] | None = None


@dataclass(frozen=True, slots=True)
class Track:
    """A straight run of copper on one layer, in mm."""

    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str
    net: str


@dataclass(frozen=True, slots=True)
class Via:
    """A through via, in mm."""

    at: tuple[float, float]
    size: float
    drill: float
    net: str


@dataclass(frozen=True, slots=True)
class Zone:
    """A copper pour, filled by KiCad.

    Attributes:
        net: Its net.
        layer: Its copper layer.
        outline: Its corners, in mm.
        priority: A higher one is filled first where zones overlap.
        clearance: Its clearance to other copper, in mm.
        min_width: The narrowest copper it leaves, in mm.
        solid: Pads of its net joined solid rather than by thermal spokes,
            as a high-current pour wants.
    """

    net: str
    layer: str
    outline: tuple[tuple[float, float], ...]
    priority: int = 0
    clearance: float = 0.5
    min_width: float = 0.25
    solid: bool = False


def write_board(
    name: str,
    parts: Sequence[PlacedPart],
    outline: Sequence[tuple[float, float]],
    index: LibraryIndex,
    *,
    stackup: Stackup | None = None,
    tracks: Sequence[Track] = (),
    vias: Sequence[Via] = (),
    zones: Sequence[Zone] = (),
    title: Mapping[str, str] | None = None,
    newline: str | None = None,
) -> str:
    """A board file with the parts placed, its outline and its copper.

    Args:
        name: The board's name, for its uuids and its title.
        parts: Every footprint, placed.
        outline: The board edge's corners, in mm, in order.
        index: The library index, which says where each footprint's file is.
        stackup: The layers; four of 1 oz if not given.
        tracks: Copper runs.
        vias: Vias.
        zones: Copper pours, filled later by KiCad.
        title: The title block's fields: ``title``, ``date``, ``rev``,
            ``company``.
        newline: The line ending; the platform's own by default.

    Returns:
        The ``.kicad_pcb`` file's text.

    Raises:
        BoardError: If a footprint is not in the libraries.
    """
    stackup = stackup or Stackup()
    newline = newline or ("\r\n" if os.name == "nt" else "\n")

    def uid(*what: str) -> str:
        return str(uuid.uuid5(NAMESPACE, "/".join((name, *what))))

    footprints = [_footprint(part, index, uid) for part in parts]
    edges = [
        lst(
            "gr_line",
            lst("start", *outline[i]),
            lst("end", *outline[(i + 1) % len(outline)]),
            lst("stroke", lst("width", 0.1), lst("type", sym("default"))),
            lst("layer", "Edge.Cuts"),
            lst("uuid", uid("edge", str(i))),
        )
        for i in range(len(outline))
    ]
    segments = [
        lst(
            "segment",
            lst("start", *t.start),
            lst("end", *t.end),
            lst("width", t.width),
            lst("layer", t.layer),
            lst("net", t.net),
            lst("uuid", uid("track", t.net, str(n))),
        )
        for n, t in enumerate(tracks)
    ]
    via_items = [
        lst(
            "via",
            lst("at", *v.at),
            lst("size", v.size),
            lst("drill", v.drill),
            lst("layers", stackup.copper[0], stackup.copper[-1]),
            lst("net", v.net),
            lst("uuid", uid("via", v.net, str(n))),
        )
        for n, v in enumerate(vias)
    ]
    zone_items = [
        _zone(z, uid("zone", z.net, z.layer, str(n))) for n, z in enumerate(zones)
    ]
    fields = title or {}
    block = [lst("title", fields.get("title", name))]
    for key in ("date", "rev", "company"):
        if fields.get(key):
            block.append(lst(key, fields[key]))
    root = lst(
        "kicad_pcb",
        lst("version", FORMAT_VERSION),
        lst("generator", "kairo"),
        lst("generator_version", __version__),
        lst(
            "general",
            lst("thickness", stackup.thickness),
            lst("legacy_teardrops", sym("no")),
        ),
        lst("paper", "A3"),
        lst("title_block", *block),
        _layers(stackup),
        _setup(stackup),
        *footprints,
        *edges,
        *segments,
        *via_items,
        *zone_items,
        lst("embedded_fonts", sym("no")),
    )
    lay_out(root, newline=newline)
    return to_text(root) + newline


def _layers(stackup: Stackup) -> SList:
    copper = [
        lst(
            str(0 if layer == "F.Cu" else 2 if layer == "B.Cu" else _INNER_IDS[layer]),
            layer,
            sym("signal"),
        )
        for layer in stackup.copper
    ]
    users = [
        lst(str(number), layer, sym("user"), *([alias] if alias else []))
        for number, layer, alias in _USER_LAYERS
    ]
    # KiCad writes a layer's number as the list's head: (0 "F.Cu" signal).
    return lst("layers", *copper, *users)


def _setup(stackup: Stackup) -> SList:
    """The setup block, with KiCad 10's defaults beside the stackup."""
    copper = stackup.copper
    gaps = len(copper) - 1
    outer = round(stackup.outer_oz * OUNCE, 4)
    inner = round(stackup.inner_oz * OUNCE, 4)
    metal = 2 * outer + (len(copper) - 2) * inner
    dielectric = round((stackup.thickness - metal - 0.02) / gaps, 4)
    layers: list[SList] = [
        lst("layer", "F.SilkS", lst("type", "Top Silk Screen"), lst("color", "White")),
        lst("layer", "F.Paste", lst("type", "Top Solder Paste")),
        lst(
            "layer",
            "F.Mask",
            lst("type", "Top Solder Mask"),
            lst("color", "Green"),
            lst("thickness", 0.01),
        ),
    ]
    for i, layer in enumerate(copper):
        weight = outer if layer in ("F.Cu", "B.Cu") else inner
        layers.append(
            lst("layer", layer, lst("type", "copper"), lst("thickness", weight))
        )
        if i < gaps:
            layers.append(
                lst(
                    "layer",
                    f"dielectric {i + 1}",
                    lst("type", "core" if i % 2 == 0 else "prepreg"),
                    lst("thickness", dielectric),
                    lst("material", "FR4"),
                    lst("epsilon_r", 4.5),
                    lst("loss_tangent", 0.02),
                )
            )
    layers += [
        lst(
            "layer",
            "B.Mask",
            lst("type", "Bottom Solder Mask"),
            lst("color", "Green"),
            lst("thickness", 0.01),
        ),
        lst("layer", "B.Paste", lst("type", "Bottom Solder Paste")),
        lst(
            "layer", "B.SilkS", lst("type", "Bottom Silk Screen"), lst("color", "White")
        ),
        lst("copper_finish", "HAL lead-free"),
        lst("dielectric_constraints", sym("no")),
    ]
    return lst(
        "setup",
        lst("stackup", *layers),
        lst("pad_to_mask_clearance", 0),
        lst("allow_soldermask_bridges_in_footprints", sym("no")),
        lst("tenting", lst("front", sym("yes")), lst("back", sym("yes"))),
        lst("covering", lst("front", sym("no")), lst("back", sym("no"))),
        lst("plugging", lst("front", sym("no")), lst("back", sym("no"))),
        lst("capping", sym("no")),
        lst("filling", sym("no")),
    )


def _footprint(
    part: PlacedPart, index: LibraryIndex, make_uid: Callable[..., str]
) -> SList:
    """A library footprint placed on the board, as KiCad 10 saves one."""
    path = index.footprint_file(part.footprint)
    if path is None:
        raise BoardError(
            f"{part.ref}: footprint {part.footprint} is not in the libraries"
        )
    source = parse(Path(path).read_bytes().decode("utf-8")).root
    turn = part.rotation
    head: list[SList | Atom] = [sym("footprint"), Atom(_quote(part.footprint))]
    body: list[SList] = []
    properties: list[SList] = []
    rest: list[SList] = []
    layer: SList | None = None
    count = 0
    for node in source.items[2:]:
        if not isinstance(node, SList):
            continue
        kind = node.head
        if kind in ("version", "generator", "generator_version"):
            continue
        item = copy(node)
        assert isinstance(item, SList)
        count += 1
        if kind == "layer":
            layer = item
            continue
        if kind in ("descr", "tags"):
            body.append(item)
            continue
        if kind == "property":
            name = _text(item, 1)
            if name == "Reference":
                _set(item, 2, part.ref)
                if part.label is not None:
                    _relabel(item, part.label)
                    _give_uuid(item, make_uid(part.ref, "property", name))
                    properties.append(item)
                    continue
            elif name == "Value":
                _set(item, 2, part.value)
            elif name in part.fields:
                _set(item, 2, part.fields[name])
            _turn(item, turn)
            _give_uuid(item, make_uid(part.ref, "property", name))
            properties.append(item)
            continue
        if kind == "attr":
            # The footprint's own BOM flag follows the symbol's.
            flags = [a for a in item.items[1:] if isinstance(a, Atom)]
            kept = [a for a in flags if a.raw != "exclude_from_bom"]
            if not part.in_bom:
                kept.append(sym("exclude_from_bom"))
            item = SList([sym("attr"), *kept])
            rest.append(item)
            continue
        if kind == "zone":
            # A footprint's zone is saved in board coordinates, unlike its
            # drawings: turned and moved with the part.
            _place_zone(item, part.x, part.y, turn)
            _give_uuid(item, make_uid(part.ref, "zone", str(count)))
            rest.append(item)
            continue
        if kind == "pad":
            number = _text(item, 1)
            _turn(item, turn)
            net = part.pads.get(number)
            if net is not None:
                _insert_before(item, "uuid", lst("net", net))
            _give_uuid(item, make_uid(part.ref, "pad", number, str(count)))
            rest.append(item)
            continue
        if kind == "fp_text":
            _turn(item, turn)
        if kind in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_text"):
            _give_uuid(item, make_uid(part.ref, kind, str(count)))
        rest.append(item)
    present = {_text(p, 1) for p in properties}
    for name, value in part.fields.items():
        if name in present or name in ("Reference", "Value", "Footprint"):
            continue
        properties.append(
            lst(
                "property",
                name,
                value,
                lst("at", 0, 0, *([turn] if turn else [])),
                lst("layer", "F.Fab"),
                lst("hide", sym("yes")),
                lst("uuid", make_uid(part.ref, "property", name)),
                lst("effects", lst("font", lst("size", 1.27, 1.27))),
            )
        )
    placement = [
        lst("path", f"/{part.path}"),
        lst("sheetname", part.sheetname),
        lst("sheetfile", part.sheetfile),
    ]
    return SList(
        [
            *head,
            layer or lst("layer", "F.Cu"),
            lst("uuid", make_uid(part.ref, "footprint")),
            lst("at", part.x, part.y, *([turn] if turn else [])),
            *body,
            *properties,
            *placement,
            *rest,
        ]
    )


def _zone(zone: Zone, uid: str) -> SList:
    connect: list[SList | Atom] = [sym("connect_pads")]
    if zone.solid:
        connect.append(sym("yes"))
    connect.append(lst("clearance", zone.clearance))
    return lst(
        "zone",
        lst("net", zone.net),
        lst("layer", zone.layer),
        lst("uuid", uid),
        lst("hatch", sym("edge"), 0.5),
        *([lst("priority", zone.priority)] if zone.priority else []),
        SList(connect),
        lst("min_thickness", zone.min_width),
        lst(
            "fill",
            sym("yes"),
            lst("thermal_gap", 0.5),
            lst("thermal_bridge_width", 0.5),
            lst("island_removal_mode", 0),
        ),
        lst("polygon", lst("pts", *[lst("xy", x, y) for x, y in zone.outline])),
    )


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _text(node: SList, i: int) -> str:
    atom = node.items[i]
    assert isinstance(atom, Atom)
    return atom.value


def _set(node: SList, i: int, text: str) -> None:
    node.items[i] = Atom(_quote(text), node.items[i].before)  # type: ignore[union-attr]


def _relabel(node: SList, label: tuple[float, float, float, float, bool]) -> None:
    """Move a reference to its label: place, angle, size, and layer."""
    x, y, angle, size, printed = label
    at = node.first("at")
    if at is not None:
        at.items[1:] = lst("at", round(x, 4), round(y, 4), angle).items[1:]
    layer = node.first("layer")
    if layer is not None and not printed:
        layer.items[1:] = lst("layer", "F.Fab").items[1:]
    effects = node.first("effects")
    if effects is not None:
        font = effects.first("font")
        if font is not None:
            font_size = font.first("size")
            if font_size is not None:
                font_size.items[1:] = lst("size", size, size).items[1:]
            thickness = font.first("thickness")
            if thickness is not None:
                thickness.items[1:] = lst("thickness", round(size * 0.15, 3)).items[1:]
        # Centred, so the label's box is where it was measured.
        effects.items = [
            i
            for i in effects.items
            if not (isinstance(i, SList) and i.head == "justify")
        ]


def _turn(node: SList, turn: float) -> None:
    """Add the footprint's turn to an item's own angle, as KiCad stores it."""
    if not turn:
        return
    at = node.first("at")
    if at is None:
        return
    values = at.values()
    angle = (float(values[2]) if len(values) > 2 else 0.0) + turn
    angle = round(angle % 360, 4)
    at.items[1:] = lst("at", float(values[0]), float(values[1]), angle).items[1:]


def _place_zone(node: SList, x: float, y: float, turn: float) -> None:
    """Turn and move a footprint zone's corners onto the board."""
    radians = math.radians(turn)
    cos, sin = math.cos(radians), math.sin(radians)
    for polygon in node.lists("polygon"):
        pts = polygon.first("pts")
        if pts is None:
            continue
        for xy in pts.lists("xy"):
            px, py = (float(v) for v in xy.values()[:2])
            # KiCad turns counter-clockwise as seen, with y pointing down.
            bx = round(x + px * cos + py * sin, 6)
            by = round(y - px * sin + py * cos, 6)
            xy.items[1:] = lst("xy", bx, by).items[1:]


def _give_uuid(node: SList, value: str) -> None:
    if node.first("uuid") is None:
        node.items.append(lst("uuid", value))


def _insert_before(node: SList, head: str, item: SList) -> None:
    for i, child in enumerate(node.items):
        if isinstance(child, SList) and child.head == head:
            node.items.insert(i, item)
            return
    node.items.append(item)
