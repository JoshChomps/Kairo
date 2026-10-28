"""Write a grounded design as a KiCad schematic.

Two styles are drawn from one design. In the ``labels`` style, the one
every check is built on, pins are joined by name, not by drawn wires:
every connected pin gets a short wire stub ending in a label or a power
symbol, so connectivity never depends on routing wires around parts. A
net with a power or ground role uses the library's power symbol of that
exact name when there is one (``GND``, ``+3V3``), and otherwise a global
label; a signal net uses local labels.

The ``wired`` style is for a person to read. Supplies and grounds keep
their power symbols, as drawn schematics do, and each other net's pins in
one block are joined by wires (`kairo.kicad.routing`), with one label per
block naming the net. A net that runs between blocks is joined by those
labels; a group the router cannot join keeps a label on every pin. Its
parts are placed for wiring (`kairo.kicad.arrange`): each block's parts
close to what they connect to, turned so the pins that meet face each
other, and each block drawn in a frame under its name. KiCad's netlist of
a wired sheet must equal the design just as the labelled one's must.

In the labelled style, a design too wide for one row of blocks on the
largest sheet is wrapped into bands, one under the other, on the smallest
sheet that holds them.

The file is built from the same tokens KiCad writes and laid out by
`kairo.kicad.emit`. Whether KiCad agrees is not left to this module: the
tests re-save the result with ``kicad-cli sch upgrade`` and require the
same content back, and take the netlist and the rule check from KiCad
itself.

This module returns text. Putting it on disk is the transaction module's
job.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kairo import __version__
from kairo.ir import Design, split_pin
from kairo.kicad.arrange import (
    Block,
    Piece,
    PinEnd,
    Shape,
    Spot,
    arrange_block,
    pack,
    structure,
)
from kairo.kicad.emit import copy, lay_out, lst, sym
from kairo.kicad.field_placement import (
    FieldLayout,
    FieldSpot,
    FieldText,
    Prefer,
    label_box,
    pin_text_boxes,
    place_fields,
)
from kairo.kicad.geometry import (
    GRID,
    Box,
    Placement,
    Rotation,
    direction,
    pin_outward,
    placed_box,
    symbol_box,
    to_sheet,
)
from kairo.kicad.routing import (
    Board,
    Group,
    Route,
    Terminal,
    board_for,
    route_groups,
)
from kairo.kicad.sexpr import Atom, SList, parse, to_text
from kairo.library.embed import embedded_symbol
from kairo.library.grounding import Grounded, GroundedPin
from kairo.library.index import LibraryIndex
from kairo.library.reader import Pin
from kairo.library.resolve import PinError, resolve_pin
from kairo.spice.kicad_fields import sim_fields
from kairo.spice.registry import ModelRegistry
from kairo.text_placement import Score
from kairo.verify.findings import Finding, Where
from kairo.verify.layout import TOUCH, Drawing, DrawnText, drawing_of

#: The schematic format written for each KiCad major version.
FORMAT_VERSION = {"10": 20260306}
#: Identifiers are derived from names under this namespace, so writing an
#: unchanged design again gives the same file.
NAMESPACE = uuid.UUID("6f0f3c5e-2d3b-5a70-9c1e-4b6a8d2e9f10")
#: The power library's flag that tells ERC a net is driven from outside.
POWER_FLAG = "power:PWR_FLAG"

# Sheet layout, in mm; every length is a whole number of 1.27 mm grid steps.
_STUB = 2 * GRID
# A pin that carries a power flag gets the flag at the end of its stub and
# then a second, longer stub to its label, so the two do not sit on one spot.
_FLAG_STUB = 4 * GRID
#: The pitch of a connector's pins, in mm, with a little slack for rounding.
_PITCH = 2.54 + 0.01
#: How much longer the stub of a power symbol is when its pin is hemmed in.
_CROWDED_STUB = 6 * GRID
_MARGIN = 20 * GRID
_COLUMN_GAP = 8 * GRID
_ROW_GAP = 6 * GRID
# The gaps are widened by these factors, in turn, while a text has no spot.
_SPREADS = (1.0, 1.5, 2.0)
# The sheet's drawing frame keeps this much of each edge.
_FRAME = 12.0
# A column is this tall at most, which fits an A4 sheet above its title block.
_COLUMN_HEIGHT = 100 * GRID
# Room allowed for a label's text until the text placer measures it: KiCad's
# default text is 1.27 mm high and its characters are narrower than that.
_CHARACTER = GRID
_TEXT_HEIGHT = 2 * GRID
#: Half the side of the square kept clear around a no-connect cross. The
#: cross's own size is not measured here; more clearance only costs room.
_MARK = GRID / 2
#: How much wider the gaps are in a wired drawing, so wires have room.
_WIRED_SPREAD = 1.5
#: A net whose pins' box measures no more than this, width plus height,
#: is wired whole; a more spread one block by block, in mm. Wires much
#: longer than a hand's width across a sheet read worse than a label.
_JOIN_SPAN = 150.0
#: Pins of a spread net in different blocks are joined when they lie
#: within this of each other, width plus height, in mm (A206): about the
#: gap two neighbouring blocks leave between pins that face each other.
_BRIDGE = 100.0
#: Room between a block's frame and its parts, in mm.
_FRAME_PAD = 2 * GRID
#: Height of the band at a frame's top that holds the block's name, in mm.
_TITLE = 4 * GRID
#: Size of a block's name, in mm: twice KiCad's default text.
_TITLE_SIZE = 2.54
#: Parts with at most this many pins may be turned to suit their wires.
_TURNABLE_PINS = 3
#: The drawing styles: joined by labels, or by wires for a person to read.
Style = Literal["labels", "wired"]
#: Sheet sizes KiCad offers, smallest first, as width and height in mm.
PAPERS: tuple[tuple[str, float, float], ...] = (
    ("A4", 297.0, 210.0),
    ("A3", 420.0, 297.0),
    ("A2", 594.0, 420.0),
    ("A1", 841.0, 594.0),
    ("A0", 1189.0, 841.0),
)


class WriterError(ValueError):
    """The design cannot be drawn as asked."""


@dataclass(frozen=True, slots=True)
class SheetPlace:
    """Where a sheet sits in a project of several (`kairo.kicad.project_writer`).

    Attributes:
        project: The project's name, which every symbol's instance names.
        path: The instance path below ``/``: the root sheet's uuid, then
            this sheet's symbol's uuid, joined by ``/``.
        page: The sheet's page number. Power and flag references are
            numbered from ``page x 1000 + 1``, the scheme KiCad's own
            annotation offers, so no two sheets share one.
        ports: The nets that leave the sheet, each with the shape of its
            hierarchical label (``input``, ``output``, ``bidirectional``
            or ``passive``).
        title: The sheet's title, for its title block.
    """

    project: str
    path: str
    page: int
    ports: Mapping[str, str]
    title: str


@dataclass(frozen=True, slots=True)
class WrittenSchematic:
    """A schematic ready to save.

    Attributes:
        text: The ``.kicad_sch`` file's content.
        uuid: The sheet's identifier.
        net_names: The name KiCad's netlist gives each of the design's nets.
        placements: Where each part was put, by reference.
        paper: The sheet size chosen, such as ``A4``.
        findings: Warnings about the drawing, such as a text with no spot.
        text_score: How the placed text scores under the text placer's
            judge; zero overlapping and zero ambiguous is the aim.
        drawing: Where every symbol, wire and text was put, for the
            layout checks.
    """

    text: str
    uuid: str
    net_names: dict[str, str]
    placements: dict[str, Placement]
    paper: str
    findings: tuple[Finding, ...]
    text_score: Score
    drawing: Drawing


def write_schematic(
    grounded: Grounded,
    index: LibraryIndex,
    *,
    newline: str | None = None,
    models: ModelRegistry | None = None,
    style: Style = "labels",
    sheet: SheetPlace | None = None,
    endings: Mapping[tuple[str, str], _Ending] | None = None,
) -> WrittenSchematic:
    """Draw a grounded design as one schematic sheet.

    Args:
        grounded: The design with its symbols and pins resolved.
        index: The library index the design was grounded against.
        newline: The line ending. KiCad writes the platform's own, so that
            is the default.
        models: The models folder. A part with a registered model gets
            KiCad's simulation fields naming it (`sim_fields`), so KiCad's
            simulator runs the model Kairo did.
        style: ``labels`` to join every pin by name, or ``wired`` to draw
            wires between a net's pins within a block, for a person.
        sheet: For one sheet of a project of several: where it sits. Its
            ports end in hierarchical labels, and it carries no sheet
            instances of its own, which the root sheet holds.
        endings: What each pin's stub ends in, worked out on the whole
            design (`endings_of`) so that a net's one power flag lands on
            one sheet only. Worked out here when not given.

    Returns:
        The schematic text and what the verifier needs to check it.

    Raises:
        WriterError: If two nets would touch on the sheet, or a part has
            no place.
    """
    design = grounded.design
    if design.kicad_version != "10":
        # KiCad 10 added tokens to every placed symbol, and only its re-save
        # has been compared with what this module writes.
        raise WriterError(
            f"writing for KiCad {design.kicad_version} is not supported yet; "
            "only KiCad 10 output has been checked against KiCad"
        )
    newline = newline or ("\r\n" if os.name == "nt" else "\n")
    ids = _Ids(design.design)
    libraries = Libraries(index)
    items = _Items()
    net_names: dict[str, str] = {}

    if endings is None:
        endings = _endings(grounded, index, libraries)
    else:
        endings = {
            (pin.ref, pin.pin.number): endings[(pin.ref, pin.pin.number)]
            for pin in grounded.pins
        }
    project = sheet.project if sheet else design.design
    path = sheet.path if sheet else ids.root
    ports: Mapping[str, str] = sheet.ports if sheet else {}

    def numbered(reference: str) -> str:
        # #PWR05 on page 3 is #PWR3005.
        if sheet is None:
            return reference
        head = reference.rstrip("0123456789")
        return f"{head}{sheet.page * 1000 + int(reference[len(head) :])}"

    def named_label(
        net: str, at: tuple[float, float], step: tuple[int, int], uid: str
    ) -> tuple[list[SList], SList]:
        if net in ports:
            return items.hierarchical_labels, hierarchical_label(
                net, ports[net], at, step, uid
            )
        return items.labels, label(net, at, step, uid)

    for lib_id in sorted({e.symbol for e in endings.values() if e.symbol}):
        libraries.embedded(lib_id)
    if any(e.flag for e in endings.values()):
        libraries.embedded(POWER_FLAG)

    # Place the parts, then the text; if a text has no clear spot, spread
    # the parts and try again before giving up on it.
    widen = _WIRED_SPREAD if style == "wired" else 1.0

    def drawn(spread: float) -> _Geometry:
        geometry = _geometry(grounded, libraries, endings, spread * widen, style)
        return _wire(grounded, libraries, geometry) if style == "wired" else geometry

    geometry = drawn(1.0)
    layout, drawing = _place_text(grounded, libraries, geometry)
    for spread in _SPREADS[1:]:
        if not layout.unplaced:
            break
        try:
            wider = drawn(spread)
        except WriterError:
            # Too wide for the largest sheet: the closer drawing stands,
            # with its unplaced texts reported.
            break
        geometry = wider
        layout, drawing = _place_text(grounded, libraries, geometry)
    simulation, skipped = sim_fields(grounded, models)
    findings = tuple(
        Finding(
            "L2",
            "W_TEXT_NO_SPOT",
            "warning",
            f"the {name.lower()} of {owner} has no clear spot on the sheet and "
            "is hidden, not drawn over something else",
            Where(ref=owner if owner in design.components else None),
        )
        for owner, name in layout.unplaced
    ) + tuple(
        Finding(
            "L2",
            "W_NO_SIM_FIELDS",
            "warning",
            f"{line}; KiCad's simulator will not have this part's model",
        )
        for line in skipped
    )

    def fields_of(owner: str) -> dict[str, FieldSpot | None]:
        return {
            name: spot for (key, name), spot in layout.spots.items() if key == owner
        }

    for ref, part in design.components.items():
        pins = [
            p.number for p in grounded.symbols[ref].pins if p.unit in (0, part.unit)
        ]
        items.symbols.append(
            placed_symbol(
                libraries.embedded(part.lib_id),
                part.lib_id,
                geometry.placements[ref],
                reference=ref,
                value=part.value,
                footprint=part.footprint,
                unit=part.unit,
                pins=pins,
                project=project,
                root=path,
                uuid_of=lambda what, ref=ref: ids.of(ref, what),
                fields=fields_of(ref),
                simulation=simulation.get(ref, ()),
            )
        )
    for net in design.connections:
        net_names[net] = net if design.nets[net].role != "signal" else f"/{net}"
    for stub in geometry.stubs:
        for number, (start, end) in enumerate(
            zip(stub.path.points, stub.path.points[1:], strict=False)
        ):
            name = "wire" if number == 0 else f"wire{number}"
            items.wires.append(wire(start, end, ids.of(stub.tag, name)))
        end = stub.path.points[-1]
        if stub.ending.symbol:
            continue
        if stub.ending.is_global:
            items.global_labels.append(
                global_label(
                    stub.ending.net, end, stub.path.step, ids.of(stub.tag, "label")
                )
            )
        else:
            target, made = named_label(
                stub.ending.net, end, stub.path.step, ids.of(stub.tag, "label")
            )
            target.append(made)
    for route in geometry.routes:
        tag = f"{route.net}/{route.segments[0][0]}"
        for number, (start, end) in enumerate(route.segments):
            items.wires.append(wire(start, end, ids.of(tag, f"route{number}")))
        for number, point in enumerate(route.junctions):
            items.junctions.append(junction(point, ids.of(tag, f"junction{number}")))
    for named in geometry.wire_labels:
        uid = ids.of(f"{named.net}/{named.at}", "label")
        if named.is_global:
            items.global_labels.append(
                global_label(named.net, named.at, named.step, uid)
            )
        else:
            target, made = named_label(named.net, named.at, named.step, uid)
            target.append(made)
    for frame in geometry.frames:
        items.rectangles.append(
            rectangle(frame.box, ids.of(f"frame/{frame.name}", "box"))
        )
        items.texts.append(
            text(
                frame.title,
                frame.title_at,
                _TITLE_SIZE,
                ids.of(f"frame/{frame.name}", "title"),
            )
        )
    for mark in geometry.open_pins:
        tag = f"{mark.ref}.{mark.pin.number}"
        at = to_sheet(geometry.placements[mark.ref], mark.pin.x, mark.pin.y)
        items.no_connects.append(
            lst("no_connect", lst("at", *at), lst("uuid", ids.of(tag, "no_connect")))
        )
    for power in geometry.powers:
        items.symbols.append(
            placed_symbol(
                libraries.embedded(power.lib_id),
                power.lib_id,
                Placement(*power.at, power.turn),
                reference=numbered(power.reference),
                value=None,
                footprint=None,
                unit=1,
                pins=["1"],
                project=project,
                root=path,
                uuid_of=lambda what, key=power.key: ids.of(key, what),
                fields=fields_of(power.key),
            )
        )

    root = lst(
        "kicad_sch",
        lst("version", FORMAT_VERSION[design.kicad_version]),
        lst("generator", "kairo"),
        lst("generator_version", __version__),
        lst("uuid", ids.root),
        lst("paper", geometry.paper),
        title_block(design, sheet.title if sheet else design.design),
        SList([sym("lib_symbols"), *libraries.used()]),
        *items.in_file_order(),
        *(
            []
            if sheet
            else [lst("sheet_instances", lst("path", "/", lst("page", "1")))]
        ),
        lst("embedded_fonts", sym("no")),
    )
    lay_out(root, newline=newline)
    return WrittenSchematic(
        to_text(root) + newline,
        ids.root,
        net_names,
        geometry.placements,
        geometry.paper,
        findings,
        layout.score,
        drawing,
    )


class _Ids:
    """Identifiers derived from names, the same on every run."""

    def __init__(self, design: str) -> None:
        self._design = design
        self.root = str(uuid.uuid5(NAMESPACE, design))

    def of(self, owner: str, what: str) -> str:
        return str(uuid.uuid5(NAMESPACE, f"{self._design}/{owner}/{what}"))


class Libraries:
    """Symbols read from library files, each file parsed once."""

    def __init__(self, index: LibraryIndex) -> None:
        """Start with nothing read."""
        self._index = index
        self._roots: dict[Path, SList] = {}
        self._embedded: dict[str, SList] = {}

    def embedded(self, lib_id: str) -> SList:
        """A symbol as a schematic embeds it; the same list on every call."""
        if lib_id not in self._embedded:
            path = self._index.symbol_file(lib_id)
            if path is None:
                raise WriterError(f"the libraries have no symbol {lib_id!r}")
            if path not in self._roots:
                self._roots[path] = parse(path.read_bytes().decode("utf-8")).root
            nickname, _, name = lib_id.partition(":")
            self._embedded[lib_id] = embedded_symbol(self._roots[path], name, nickname)
        return self._embedded[lib_id]

    def used(self) -> list[SList]:
        """Every symbol asked for so far, in the order KiCad keeps them."""
        # KiCad keeps lib_symbols in name order.
        return [self._embedded[lib_id] for lib_id in sorted(self._embedded)]


class _Items:
    """What goes on the sheet, kept by kind because KiCad saves by kind."""

    def __init__(self) -> None:
        self.rectangles: list[SList] = []
        self.texts: list[SList] = []
        self.junctions: list[SList] = []
        self.no_connects: list[SList] = []
        self.wires: list[SList] = []
        self.labels: list[SList] = []
        self.global_labels: list[SList] = []
        self.hierarchical_labels: list[SList] = []
        self.symbols: list[SList] = []

    def in_file_order(self) -> list[SList]:
        # KiCad saves each kind in the order of its identifiers, drawings
        # and texts first (the pedalboard board's sheets), hierarchical
        # labels after the others (KiCad 10.0.7's save of cm5_minima).
        kinds = (
            self.rectangles,
            self.texts,
            self.junctions,
            self.no_connects,
            self.wires,
            self.labels,
            self.global_labels,
            self.hierarchical_labels,
            self.symbols,
        )
        return [item for kind in kinds for item in sorted(kind, key=_uuid_of)]


def _uuid_of(item: SList) -> str:
    node = item.first("uuid")
    assert node is not None
    return node.values()[0]


@dataclass(frozen=True, slots=True)
class _Stub:
    """One pin's wire and what it ends in."""

    tag: str
    path: _Path
    ending: _Ending


@dataclass(frozen=True, slots=True)
class _Power:
    """A power symbol or power flag to place."""

    key: str
    lib_id: str
    at: tuple[float, float]
    reference: str
    turn: Rotation = 0


@dataclass(frozen=True, slots=True)
class _OpenPin:
    """A pin the design leaves open on purpose, to be marked no-connect."""

    ref: str
    pin: Pin


@dataclass(frozen=True, slots=True)
class _Geometry:
    """Where everything on the sheet goes, before any text is placed."""

    placements: dict[str, Placement]
    paper: str
    stubs: tuple[_Stub, ...]
    powers: tuple[_Power, ...]
    open_pins: tuple[_OpenPin, ...]
    routes: tuple[Route, ...] = ()
    wire_labels: tuple[_WireLabel, ...] = ()
    frames: tuple[_Frame, ...] = ()


@dataclass(frozen=True, slots=True)
class _Frame:
    """A block's frame on a wired sheet, with its name in a band at the top."""

    name: str
    box: Box

    @property
    def title(self) -> str:
        """The block's name as a person reads it: ``cell_inputs`` as ``Cell inputs``."""
        words = self.name.replace("_", " ").strip()
        return words[:1].upper() + words[1:]

    @property
    def title_at(self) -> tuple[float, float]:
        """Where the name's text is anchored: its bottom left, in the band."""
        return (round(self.box.left + GRID, 4), round(self.box.top + _TITLE - GRID, 4))

    @property
    def title_box(self) -> Box:
        """The room the name takes, measured as for any text of its size."""
        x, y = self.title_at
        return Box(x, y - _TITLE_SIZE * 1.2, x + len(self.title) * _TITLE_SIZE, y)


def _geometry(
    grounded: Grounded,
    libraries: Libraries,
    endings: dict[tuple[str, str], _Ending],
    spread: float,
    style: Style = "labels",
) -> _Geometry:
    """Place the parts and work out every wire, label and power symbol."""
    frames: tuple[_Frame, ...] = ()
    if style == "wired":
        placements, paper, frames = _arrange(grounded, libraries, endings, spread)
    else:
        placements, paper = _place(grounded, libraries, endings, spread)
    points: dict[tuple[float, float], str] = {}
    stubs: list[_Stub] = []
    powers: list[_Power] = []
    flags: list[_Power] = []
    drawn: set[tuple[str, str, float, float]] = set()
    for net in grounded.design.connections:
        for pin in grounded.net_pins(net):
            # Library symbols stack pins of one name on one spot (a USB-C
            # receptacle's four VBUS pins). One wire there connects them
            # all, so only the first is given a stub and its ending.
            spot = (net, pin.ref, pin.pin.x, pin.pin.y)
            if spot in drawn:
                continue
            drawn.add(spot)
            ending = endings[(pin.ref, pin.pin.number)]
            path = _path(placements[pin.ref], pin, ending)
            for point in path.points:
                if points.setdefault(point, net) != net:
                    raise WriterError(
                        f"{pin.ref} pin {pin.pin.number} on {net} would touch "
                        f"net {points[point]} at {point}"
                    )
            tag = f"{pin.ref}.{pin.pin.number}"
            stubs.append(_Stub(tag, path, ending))
            if ending.symbol:
                powers.append(
                    _Power(
                        f"{tag}/power",
                        ending.symbol,
                        path.points[-1],
                        f"#PWR{len(powers) + 1:02d}",
                        ending.turn,
                    )
                )
            if ending.flag:
                flags.append(
                    _Power(
                        f"{tag}/flag",
                        POWER_FLAG,
                        path.points[1],
                        f"#FLG{len(flags) + 1:02d}",
                    )
                )
    open_pins: list[_OpenPin] = []
    design = grounded.design
    for written in design.no_connect:
        ref, name = split_pin(written, design.components)
        try:
            found = resolve_pin(
                grounded.symbols[ref], ref, name, unit=design.components[ref].unit
            )
        except PinError as exc:
            raise WriterError(str(exc)) from exc
        for pin in found:
            point = to_sheet(placements[ref], pin.x, pin.y)
            if point in points:
                raise WriterError(
                    f"{ref} pin {pin.number} is listed under no_connect and "
                    f"is on net {points[point]}"
                )
            open_pins.append(_OpenPin(ref, pin))
    return _Geometry(
        placements,
        paper,
        tuple(stubs),
        (*powers, *flags),
        tuple(open_pins),
        frames=frames,
    )


def _wire(grounded: Grounded, libraries: Libraries, geometry: _Geometry) -> _Geometry:
    """Join each signal net's pins within a block by wires.

    Every pin keeps the stub a labelled drawing would give it until its
    group is joined: the stubs are the only room the router may not take
    from another net, so a group it cannot join falls back to labels with
    nothing in the way of their ends. A joined group loses its stubs and
    is named by one label on its own wire (`_label_on`), so the net has
    its name on the sheet and in KiCad's netlist.
    """
    design = grounded.design
    block_of = {ref: name for name, refs in design.blocks.items() for ref in refs}
    width, height = next((w, h) for name, w, h in PAPERS if name == geometry.paper)
    board = board_for(Box(_FRAME, _FRAME, width - _FRAME, height - _FRAME))
    for ref, part in design.components.items():
        symbol = libraries.embedded(part.lib_id)
        placement = geometry.placements[ref]
        board.block(placed_box(placement, symbol_box(symbol, part.unit)))
        for _, box in pin_text_boxes(symbol, placement, part.unit):
            board.block(box)
    for power in geometry.powers:
        body = symbol_box(libraries.embedded(power.lib_id))
        # The body alone: grown by a step, a symbol turned along a
        # connector's stub covered the next pin's row, and its wire had no
        # way out.
        board.block(placed_box(Placement(*power.at, power.turn), body))
    for mark in geometry.open_pins:
        x, y = to_sheet(geometry.placements[mark.ref], mark.pin.x, mark.pin.y)
        board.block(Box(x - _MARK, y - _MARK, x + _MARK, y + _MARK))
    for frame in geometry.frames:
        board.block(frame.title_box)

    # Every net that would end in labels is joined where it can be: a
    # supply with no power symbol of its name as much as a signal, since a
    # global label on each of its pins reads no better than a local one.
    signal: dict[str, list[_Stub]] = {}
    for stub in geometry.stubs:
        board.keep(stub.ending.net, stub.path.points)
        if stub.ending.symbol:
            continue
        signal.setdefault(stub.ending.net, []).append(stub)
    # Labels drawn whatever the router does, a net's lone pin's and every
    # global label, are kept clear of wires.
    for stub in geometry.stubs:
        if not stub.ending.symbol and len(signal.get(stub.ending.net, [])) < 2:
            board.block(
                label_box(
                    stub.ending.net,
                    stub.path.points[-1],
                    stub.path.step,
                    framed=stub.ending.is_global,
                )
            )
    for stub in geometry.stubs:
        if not stub.ending.symbol:
            board.free(stub.path.points)

    def by_block(stubs: list[_Stub]) -> list[list[_Stub]]:
        parts: dict[str, list[_Stub]] = {}
        for stub in stubs:
            block = block_of.get(stub.tag.rsplit(".", 1)[0], "")
            parts.setdefault(block, []).append(stub)
        return [part for part in parts.values() if len(part) >= 2]

    # A net whose pins lie close together is joined whole. A spread one is
    # joined block by block, and across neighbouring blocks where their
    # pins lie close; a group the router could not join falls back to its
    # blocks, which then share the net through their labels.
    whole: list[list[_Stub]] = []
    split: list[list[_Stub]] = []
    for stubs in signal.values():
        if len(stubs) >= 2:
            if _span(stubs) <= _JOIN_SPAN:
                whole.append(stubs)
                continue
            for group in _bridged(stubs, block_of):
                blocks = {block_of.get(s.tag.rsplit(".", 1)[0], "") for s in group}
                if len(group) >= 2:
                    (split if len(blocks) == 1 else whole).append(group)
    attempts: list[tuple[list[_Stub], Route | None]] = []
    for stubs, route in _join(board, whole):
        parts = by_block(stubs) if route is None else []
        if route is None and parts and parts != [stubs]:
            split += parts
        else:
            attempts.append((stubs, route))
    attempts += _join(board, split)
    members = [stubs for stubs, _ in attempts]
    routes = {n: route for n, (_, route) in enumerate(attempts) if route is not None}

    # Each joined group is named by one label on its own wire, where the
    # label's text clears every body, pin text, power symbol and other
    # label; failing that, its first pin keeps its stub and label.
    kept = [
        stub
        for number, stubs in enumerate(members)
        for stub in stubs
        if number not in routes
    ]
    kept += [s for s in geometry.stubs if s.ending.symbol]
    avoid = list(_obstacles(grounded, libraries, geometry))
    joined = {stub.tag for number in routes for stub in members[number]}
    for stub in geometry.stubs:
        if not stub.ending.symbol and stub.tag not in joined:
            avoid.append(
                label_box(
                    stub.ending.net,
                    stub.path.points[-1],
                    stub.path.step,
                    framed=stub.ending.is_global,
                )
            )
    named: list[_WireLabel] = []
    wires = {
        number: [_segment_box(s) for s in route.segments]
        for number, route in routes.items()
    }
    # KiCad joins a label to every wire through its anchor, so a label
    # anchored where another net's wire or stub passes would join the two.
    drawn: list[tuple[str, Box]] = [
        (route.net, box) for number, route in routes.items() for box in wires[number]
    ]
    drawn += [
        (stub.ending.net, _segment_box(segment))
        for stub in geometry.stubs
        for segment in zip(stub.path.points, stub.path.points[1:], strict=False)
    ]
    for number in sorted(routes):
        stubs = members[number]
        if stubs[0].ending.flag:
            kept.append(stubs[0])
            continue
        others = [box for n, boxes in wires.items() if n != number for box in boxes]
        foreign = [box for net, box in drawn if net != routes[number].net]
        found = _label_on(
            routes[number], avoid, others, stubs[0].ending.is_global, foreign
        )
        if found is None:
            # The label goes on one pin's stub instead: the first whose
            # label clears everything, so a short route between two parts
            # packed close does not put its name over one of them.
            def box_of(stub: _Stub) -> Box:
                return label_box(
                    stub.ending.net,
                    stub.path.points[-1],
                    stub.path.step,
                    framed=stub.ending.is_global,
                )

            chosen = next(
                (
                    stub
                    for stub in stubs
                    if not any(box_of(stub).overlaps(o, TOUCH) for o in avoid)
                ),
                stubs[0],
            )
            kept.append(chosen)
            avoid.append(box_of(chosen))
            continue
        named.append(found)
        avoid.append(label_box(found.net, found.at, found.step, framed=found.is_global))
    # Only the stubs of joined groups go, and of those only the ones no
    # label stayed on; every other stub is drawn as in a labelled sheet.
    keep = {stub.tag for stub in kept}
    dropped = {
        stub.tag
        for number in routes
        for stub in members[number]
        if stub.tag not in keep
    }
    return _Geometry(
        geometry.placements,
        geometry.paper,
        tuple(stub for stub in geometry.stubs if stub.tag not in dropped),
        geometry.powers,
        geometry.open_pins,
        tuple(routes[number] for number in sorted(routes)),
        tuple(named),
        geometry.frames,
    )


def _join(
    board: Board, parts: list[list[_Stub]]
) -> list[tuple[list[_Stub], Route | None]]:
    """Route groups of stubs; a flagged stub comes first in its group."""
    ordered = [sorted(stubs, key=lambda s: not s.ending.flag) for stubs in parts]
    groups = [
        Group(
            stubs[0].ending.net,
            tuple(Terminal(s.tag, s.path.points[0], s.path.step) for s in stubs),
        )
        for stubs in ordered
    ]
    routes = route_groups(board, groups)
    return [(stubs, routes.get(number)) for number, stubs in enumerate(ordered)]


def _bridged(stubs: list[_Stub], block_of: dict[str, str]) -> list[list[_Stub]]:
    """A spread net's pins in groups: one per block, joined where blocks meet.

    Pins of one block always share a group, as a block is drawn as one
    piece. Two blocks' groups become one when a pin of each lies within
    `_BRIDGE` of the other, so a net that runs between neighbouring blocks
    is drawn as a wire and one between far ones keeps its labels.

    Returns:
        The groups, each in the order its pins came, in the order of
        their first pins; a group may have a single pin.
    """

    def block(stub: _Stub) -> str:
        return block_of.get(stub.tag.rsplit(".", 1)[0], "")

    root: dict[int, int] = {}

    def find(n: int) -> int:
        while root.get(n, n) != n:
            n = root[n]
        return n

    def union(a: int, b: int) -> None:
        root[find(b)] = find(a)

    first: dict[str, int] = {}
    for n, stub in enumerate(stubs):
        union(first.setdefault(block(stub), n), n)
    for a, one in enumerate(stubs):
        for b in range(a + 1, len(stubs)):
            other = stubs[b]
            if block(one) == block(other) or find(a) == find(b):
                continue
            (x1, y1), (x2, y2) = one.path.points[0], other.path.points[0]
            if abs(x1 - x2) + abs(y1 - y2) <= _BRIDGE:
                union(a, b)
    groups: dict[int, list[_Stub]] = {}
    for n, stub in enumerate(stubs):
        groups.setdefault(find(n), []).append(stub)
    return list(groups.values())


def _span(stubs: list[_Stub]) -> float:
    """How far apart a net's pins lie: the width plus the height of their box."""
    xs = [s.path.points[0][0] for s in stubs]
    ys = [s.path.points[0][1] for s in stubs]
    return max(xs) - min(xs) + max(ys) - min(ys)


@dataclass(frozen=True, slots=True)
class _WireLabel:
    """A label that names a wired net, anchored on one of its wires."""

    net: str
    at: tuple[float, float]
    step: tuple[int, int]
    is_global: bool = False


def _obstacles(
    grounded: Grounded, libraries: Libraries, geometry: _Geometry
) -> list[Box]:
    """What a wire's label must not cover: bodies, pin texts, power symbols."""
    boxes: list[Box] = []
    for ref, part in grounded.design.components.items():
        symbol = libraries.embedded(part.lib_id)
        placement = geometry.placements[ref]
        boxes.append(placed_box(placement, symbol_box(symbol, part.unit)))
        boxes += [box for _, box in pin_text_boxes(symbol, placement, part.unit)]
    for power in geometry.powers:
        body = symbol_box(libraries.embedded(power.lib_id))
        boxes.append(
            _grown(placed_box(Placement(*power.at, power.turn), body), _TEXT_HEIGHT)
        )
    for mark in geometry.open_pins:
        x, y = to_sheet(geometry.placements[mark.ref], mark.pin.x, mark.pin.y)
        boxes.append(Box(x - _MARK, y - _MARK, x + _MARK, y + _MARK))
    return boxes


def _segment_box(segment: tuple[tuple[float, float], tuple[float, float]]) -> Box:
    (x0, y0), (x1, y1) = segment
    return Box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _on(point: tuple[float, float], segment: Box) -> bool:
    """Whether a point lies on a wire, given as its box, ends included."""
    x, y = point
    return (
        segment.left - 1e-6 <= x <= segment.right + 1e-6
        and segment.top - 1e-6 <= y <= segment.bottom + 1e-6
    )


def _label_on(
    route: Route,
    avoid: list[Box],
    others: list[Box],
    is_global: bool = False,
    foreign: Sequence[Box] = (),
) -> _WireLabel | None:
    """A spot on a route's wires for its net's label, longest wire first.

    A spot whose text lies along its own wire and clears other nets' wires
    is taken first; then one that only clears what it must.
    """
    runs: list[tuple[float, bool, tuple[float, float], tuple[float, float]]] = []
    for a, b in route.segments:
        low, high = sorted((a, b))
        across = a[1] == b[1]
        runs.append(
            ((high[0] - low[0]) if across else (high[1] - low[1]), across, low, high)
        )
    runs.sort(key=lambda run: (-run[0], not run[1]))
    for strict in (True, False):
        for length, across, low, high in runs:
            for k in range(1, round(length / GRID)):
                if across:
                    at = (round(low[0] + k * GRID, 4), low[1])
                    step = (1, 0)
                else:
                    # Upright text reads upward, from the lower end.
                    at = (low[0], round(high[1] - k * GRID, 4))
                    step = (0, -1)
                if any(_on(at, wire) for wire in foreign):
                    continue
                box = label_box(route.net, at, step, framed=is_global)
                if any(box.overlaps(other, TOUCH) for other in avoid):
                    continue
                if strict:
                    inside = (box.right <= high[0]) if across else (box.top >= low[1])
                    if not inside or any(box.overlaps(o, TOUCH) for o in others):
                        continue
                return _WireLabel(route.net, at, step, is_global)
    return None


def _place_text(
    grounded: Grounded, libraries: Libraries, geometry: _Geometry
) -> tuple[FieldLayout, Drawing]:
    """Find spots for every visible reference and value on the sheet.

    Returns the spots, and a record of where everything is drawn.
    """
    design = grounded.design
    owners: dict[str, Box] = {}
    fields: list[FieldText] = []
    pins: list[DrawnText] = []
    for ref, part in design.components.items():
        symbol = libraries.embedded(part.lib_id)
        owners[ref] = placed_box(
            geometry.placements[ref], symbol_box(symbol, part.unit)
        )
        # Pin numbers lie outside the symbol's box on its outer pins, so a
        # connector's reference placed just above it covered pin 1's number.
        pins += [
            DrawnText(ref, "pin", text, box, True)
            for text, box in pin_text_boxes(symbol, geometry.placements[ref], part.unit)
        ]
        for name, text in _visible(symbol, ref, part.value):
            prefer = "above" if name == "Reference" else "below"
            fields.append(FieldText(ref, name, text, prefer))
    for power in geometry.powers:
        symbol = libraries.embedded(power.lib_id)
        body = symbol_box(symbol)
        owners[power.key] = placed_box(Placement(*power.at, power.turn), body)
        # The name goes on the far side from the wire: below a ground,
        # above a supply, and past the tip of a symbol turned along its
        # stub.
        stands_up = -body.top <= body.bottom
        side: Prefer = "above" if stands_up else "below"
        if power.turn:
            tip, _ = direction(
                Placement(0.0, 0.0, power.turn), 0, -1 if stands_up else 1
            )
            side = "left" if tip < 0 else "right"
        for name, text in _visible(symbol, power.reference, None):
            fields.append(FieldText(power.key, name, text, side))
    wires: list[Box] = []
    labels: list[DrawnText] = []
    for route in geometry.routes:
        wires += [_segment_box(segment) for segment in route.segments]
    for named in geometry.wire_labels:
        box = label_box(named.net, named.at, named.step, framed=named.is_global)
        labels.append(DrawnText(named.net, "label", named.net, box, False))
    wires += [frame.title_box for frame in geometry.frames]
    for stub in geometry.stubs:
        for (x0, y0), (x1, y1) in zip(
            stub.path.points, stub.path.points[1:], strict=False
        ):
            wires.append(Box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        if not stub.ending.symbol:
            box = label_box(
                stub.ending.net,
                stub.path.points[-1],
                stub.path.step,
                framed=stub.ending.is_global,
            )
            labels.append(
                DrawnText(stub.ending.net, "label", stub.ending.net, box, False)
            )
    # A no-connect cross sits on a pin's end and reaches past the body's
    # box, so text placed just outside the box could cover it.
    for open_pin in geometry.open_pins:
        x, y = to_sheet(
            geometry.placements[open_pin.ref], open_pin.pin.x, open_pin.pin.y
        )
        wires.append(Box(x - _MARK, y - _MARK, x + _MARK, y + _MARK))
    width, height = next((w, h) for name, w, h in PAPERS if name == geometry.paper)
    sheet = Box(_FRAME, _FRAME, width - _FRAME, height - _FRAME)
    layout = place_fields(
        owners,
        [*wires, *(label.box for label in labels), *(pin.box for pin in pins)],
        fields,
        sheet,
    )
    texts = list(labels)
    for field in fields:
        spot = layout.spots[(field.owner, field.name)]
        if spot is not None:
            texts.append(
                DrawnText(
                    field.owner,
                    field.name,
                    field.text,
                    spot.box,
                    field.owner in design.components,
                )
            )
    return layout, drawing_of(
        sheet, owners, list(design.components), wires, texts, pins
    )


def _visible(symbol: SList, reference: str, value: str | None) -> list[tuple[str, str]]:
    """The reference and value of a symbol instance, if they are shown."""
    shown: list[tuple[str, str]] = []
    for node in symbol.lists("property"):
        name_atom, value_atom = node.items[1], node.items[2]
        assert isinstance(name_atom, Atom) and isinstance(value_atom, Atom)
        name = name_atom.value
        if name not in ("Reference", "Value"):
            continue
        hide = node.first("hide")
        if hide is not None and hide.values() == ["yes"]:
            continue
        if name == "Reference":
            # The reference of a power symbol is never shown.
            if not reference.startswith("#"):
                shown.append((name, reference))
        else:
            shown.append((name, value or value_atom.value))
    return shown


@dataclass(frozen=True, slots=True)
class _Ending:
    """What a pin's stub ends in.

    Attributes:
        net: The net's name.
        symbol: The power symbol to end in, or empty for a label.
        is_global: For a label, whether it is a global one.
        flag: Whether the stub carries a power flag.
        crowded: Whether the stub ends in a power symbol and the pin has
            other connected pins of its part within one pin pitch on both
            sides, so the stub is drawn longer to clear their labels.
        turn: The power symbol's rotation. A symbol upright would face a
            connected neighbour one pitch away (`_facing`), so it is
            turned to point along its stub instead.
    """

    net: str
    symbol: str
    is_global: bool
    flag: bool
    crowded: bool = False
    turn: Rotation = 0


@dataclass(frozen=True, slots=True)
class _Path:
    """A pin's wire: its points from the pin outward, and the way it runs."""

    points: tuple[tuple[float, float], ...]
    step: tuple[int, int]


def endings_of(
    grounded: Grounded, index: LibraryIndex
) -> dict[tuple[str, str], _Ending]:
    """What every connected pin's stub ends in, decided on the whole design.

    A design drawn on several sheets decides this once, so each net that
    needs a power flag gets exactly one, on whichever sheet holds the pin
    chosen for it.
    """
    return _endings(grounded, index, Libraries(index))


def _endings(
    grounded: Grounded, index: LibraryIndex, libraries: Libraries
) -> dict[tuple[str, str], _Ending]:
    """Decide, for every connected pin, what its stub ends in."""
    design = grounded.design
    endings: dict[tuple[str, str], _Ending] = {}
    parts_with_flag: set[str] = set()
    for net in design.connections:
        role = design.nets[net].role
        pins = grounded.net_pins(net)
        symbol = f"power:{net}"
        uses_symbol = role != "signal" and index.symbol(symbol) is not None
        # A power symbol's own pin is a supply input too, so a net drawn
        # with power symbols needs driving even if no part on it does.
        has_input = uses_symbol or any(
            p.pin.electrical_type == "power_in" for p in pins
        )
        needs_flag = has_input and not any(
            p.pin.electrical_type == "power_out" for p in pins
        )
        # One flag per net, on the pin with the most room: on a part that
        # carries no flag yet, and farthest from that part's other pins. A
        # flag between two pins of a connector 2.54 mm apart has nowhere
        # to put its name.
        flagged = None
        if needs_flag:
            flagged = max(
                pins,
                key=lambda p: (
                    p.ref not in parts_with_flag,
                    _room(grounded, p),
                    -pins.index(p),
                ),
            )
            parts_with_flag.add(flagged.ref)
        # Upright, a ground hangs below its pin and a supply stands above
        # it, whichever way the pin faces.
        stands_up = False
        if uses_symbol:
            body = symbol_box(libraries.embedded(symbol))
            stands_up = -body.top <= body.bottom
        for pin in pins:
            crowded = uses_symbol and _hemmed_in(grounded, pin)
            turn: Rotation = 0
            if uses_symbol and not crowded and _facing(grounded, pin, stands_up):
                turn = _turn_along(pin.pin.angle, stands_up)
            endings[(pin.ref, pin.pin.number)] = _Ending(
                net,
                symbol if uses_symbol else "",
                role != "signal",
                pin is flagged,
                crowded,
                turn,
            )
    return endings


def _neighbours(grounded: Grounded, pin: GroundedPin) -> list[Pin]:
    """The connected pins of a pin's part one pin pitch away on its side."""
    return [
        other.pin
        for other in grounded.pins
        if other.ref == pin.ref
        and other.pin.number != pin.pin.number
        and other.pin.angle == pin.pin.angle
        and abs(other.pin.x - pin.pin.x) + abs(other.pin.y - pin.pin.y) <= _PITCH
    ]


def _hemmed_in(grounded: Grounded, pin: GroundedPin) -> bool:
    """Whether a pin has connected neighbours one pin pitch away on both sides.

    Such a pin is in the middle of a connector's row. A power symbol at
    the usual distance would sit between its neighbours' labels, with
    nowhere to put its name.
    """
    return len(_neighbours(grounded, pin)) >= 2


def _facing(grounded: Grounded, pin: GroundedPin, stands_up: bool) -> bool:
    """Whether an upright power symbol on a side pin faces a neighbour.

    A supply stands above its stub's end and a ground hangs below it. On a
    pin at a part's left or right side, a connected neighbour one pitch
    above a supply, or below a ground, has its own stub where the symbol
    and its name would be. Two such pins facing each other (a ground on
    one, a supply on the next) would put their symbols on top of each
    other, which no stub length cures.

    Args:
        grounded: The design with its pins resolved.
        pin: The pin.
        stands_up: Whether its power symbol stands above the pin, as a
            supply does, rather than hanging below it, as a ground does.
    """
    if pin.pin.angle % 180:
        # A pin on the top or bottom: its symbol already runs along the stub.
        return False
    # The library's y grows upward, so a supply stands toward larger y.
    toward = 1 if stands_up else -1
    return any(
        other.x == pin.pin.x and 0 < (other.y - pin.pin.y) * toward <= _PITCH
        for other in _neighbours(grounded, pin)
    )


def _turn_along(angle: float, stands_up: bool) -> Rotation:
    """The rotation that points a power symbol along a side pin's stub.

    Measured with kicad-cli's render of KiCad 10.0.6: a supply turned 90
    degrees points left and turned 270 points right; a ground is the
    other way round.

    Args:
        angle: The pin's angle in the library, 0 or 180.
        stands_up: Whether the symbol stands above its pin upright.
    """
    away = (0, -1) if stands_up else (0, 1)
    outward = pin_outward(Placement(0.0, 0.0), angle)
    for rotation in (90, 270):
        dx, dy = direction(Placement(0.0, 0.0, rotation), *away)
        if (round(dx), round(dy)) == outward:
            return rotation
    raise WriterError(f"no turn points a power symbol along a pin at {angle}")


def _room(grounded: Grounded, pin: GroundedPin) -> float:
    """How far a pin is from the nearest other pin of its part, in mm."""
    part = grounded.design.components[pin.ref]
    others = [
        abs(other.x - pin.pin.x) + abs(other.y - pin.pin.y)
        for other in grounded.symbols[pin.ref].pins
        if other.number != pin.pin.number and other.unit in (0, part.unit)
    ]
    return min(others, default=0.0)


def _path(placement: Placement, grounded_pin: GroundedPin, ending: _Ending) -> _Path:
    """A pin's end point and the points of the wire leading away from it."""
    pin = grounded_pin.pin
    start = to_sheet(placement, pin.x, pin.y)
    step = pin_outward(placement, pin.angle)

    def along(distance: float) -> tuple[float, float]:
        return (
            round(start[0] + step[0] * distance, 4),
            round(start[1] + step[1] * distance, 4),
        )

    reach = _STUB + _CROWDED_STUB if ending.crowded else _STUB
    if ending.flag:
        return _Path((start, along(reach), along(reach + _FLAG_STUB)), step)
    return _Path((start, along(reach)), step)


def _outline(
    grounded: Grounded,
    libraries: Libraries,
    endings: dict[tuple[str, str], _Ending],
    ref: str,
    rotation: Rotation = 0,
    wired: frozenset[str] = frozenset(),
    rounded: bool = False,
) -> Box:
    """The room a part needs around its origin: body, stubs and their ends.

    A pin on a net in ``wired`` will be joined by a wire, so only its stub
    is kept clear, not the room its label would take. With ``rounded``, a
    label's room is rounded up as `_letters` says.
    """
    part = grounded.design.components[ref]
    origin = Placement(0.0, 0.0, rotation)
    boxes = [placed_box(origin, symbol_box(libraries.embedded(part.lib_id), part.unit))]
    for pin in grounded.pins:
        if pin.ref != ref:
            continue
        ending = endings[(ref, pin.pin.number)]
        path = _path(origin, pin, ending)
        (x, y), (dx, dy) = path.points[-1], path.step
        if ending.symbol:
            body = placed_box(
                Placement(x, y, ending.turn),
                symbol_box(libraries.embedded(ending.symbol)),
            )
            boxes.append(_grown(body, _TEXT_HEIGHT))
        elif ending.net in wired:
            pass
        else:
            # A global label has a frame around its text.
            length = _letters(ending.net, rounded) * _CHARACTER + (
                2 * GRID if ending.is_global else 0
            )
            reach = (x + dx * length, y + dy * length)
            boxes.append(
                _grown(
                    Box(
                        min(x, reach[0]),
                        min(y, reach[1]),
                        max(x, reach[0]),
                        max(y, reach[1]),
                    ),
                    _TEXT_HEIGHT / 2,
                )
            )
        if ending.flag:
            flag = placed_box(
                Placement(*path.points[1]), symbol_box(libraries.embedded(POWER_FLAG))
            )
            boxes.append(_grown(flag, _TEXT_HEIGHT))
        for px, py in path.points:
            boxes.append(Box(px, py, px, py))
    return Box(
        min(b.left for b in boxes),
        min(b.top for b in boxes),
        max(b.right for b in boxes),
        max(b.bottom for b in boxes),
    )


def _grown(box: Box, by: float) -> Box:
    return Box(box.left - by, box.top - by, box.right + by, box.bottom + by)


@dataclass(frozen=True, slots=True)
class _Item:
    """Parts placed as one: a main part with its support parts, or one part.

    Attributes:
        members: Each part and its offset from the item's origin, in mm.
        outline: The room the whole item needs about its origin.
    """

    members: tuple[tuple[str, float, float], ...]
    outline: Box


def _pin_count(grounded: Grounded, ref: str) -> int:
    """How many pins a part's unit has, of its first body style."""
    part = grounded.design.components[ref]
    return len(
        {
            pin.number
            for pin in grounded.symbols[ref].pins
            if pin.unit in (0, part.unit) and pin.body_style in (0, 1)
        }
    )


def _items(
    grounded: Grounded, outlines: dict[str, Box], refs: list[str], spread: float
) -> list[_Item]:
    """A block's parts as items: its main part with its support parts, then the rest.

    The main part is the block's first part of more than two pins. A
    support part is a two-pin part of the block that shares a net other
    than ground with it, such as a decoupling capacitor, a pull-up or a
    feedback resistor. Each is drawn beside the main part's pin on that
    net, on the side the pin leaves, level with it, and below the one
    before where two would meet; a signal net is preferred to a supply as
    the pin it serves. This is the plan's rule for placing inside a block.
    A support that would be pushed below the main part's own height is
    no longer beside it, and stays with the rest of the block, which
    wraps into further columns as before.
    """
    design = grounded.design
    grounds = {name for name, net in design.nets.items() if net.role == "ground"}
    main = next((ref for ref in refs if _pin_count(grounded, ref) > 2), None)
    single = [_Item(((ref, 0.0, 0.0),), outlines[ref]) for ref in refs]
    if main is None:
        return single
    main_pins = [
        pin for pin in grounded.pins if pin.ref == main and pin.net not in grounds
    ]
    sides: dict[int, list[tuple[float, int, str]]] = {-1: [], 1: []}
    for order, ref in enumerate(refs):
        if ref == main or _pin_count(grounded, ref) != 2:
            continue
        nets = {pin.net for pin in grounded.pins if pin.ref == ref}
        shared = [pin for pin in main_pins if pin.net in nets]
        if not shared:
            continue
        anchor = min(
            shared,
            key=lambda pin: (
                design.nets[pin.net].role == "power",
                main_pins.index(pin),
            ),
        )
        origin = Placement(0.0, 0.0)
        y = to_sheet(origin, anchor.pin.x, anchor.pin.y)[1]
        side = pin_outward(origin, anchor.pin.angle)[0] or 1
        sides[side].append((y, order, ref))
    if not sides[-1] and not sides[1]:
        return single
    gap = _ROW_GAP * spread
    body = outlines[main]
    members: list[tuple[str, float, float]] = [(main, 0.0, 0.0)]
    for side, supports in sides.items():
        if not supports:
            continue
        if side == 1:
            x = _snap_up(
                body.right + gap - min(outlines[r].left for _, _, r in supports)
            )
        else:
            x = -_snap_up(
                max(outlines[r].right for _, _, r in supports) + gap - body.left
            )
        below = -math.inf
        for level, _, ref in sorted(supports):
            box = outlines[ref]
            y = round(round((level - (box.top + box.bottom) / 2) / GRID) * GRID, 4)
            # Never above the main part's own room, so the item is no
            # taller at the top than the main part alone.
            if y + box.top < max(below, body.top):
                y = _snap_up(max(below, body.top) - box.top)
            if y + box.top > body.bottom:
                # Pushed below the main part, it is no longer beside it.
                break
            members.append((ref, x, y))
            below = y + box.bottom + gap
    beside = {ref for ref, _, _ in members}
    outline = Box(
        min(dx + outlines[r].left for r, dx, _ in members),
        min(dy + outlines[r].top for r, _, dy in members),
        max(dx + outlines[r].right for r, dx, _ in members),
        max(dy + outlines[r].bottom for r, _, dy in members),
    )
    return [
        _Item(tuple(members), outline),
        *(item for item in single if item.members[0][0] not in beside),
    ]


def _place(
    grounded: Grounded,
    libraries: Libraries,
    endings: dict[tuple[str, str], _Ending],
    spread: float = 1.0,
) -> tuple[dict[str, Placement], str]:
    """Put each block in a column, in reading order, and choose the paper.

    Each part is given the room its body, stubs, labels and power symbols
    need, so no two parts' drawings can overlap, and columns and rows are
    spaced from those sizes. Inside a block the main part comes first,
    with its support parts beside the pins they serve (`_items`).
    """
    design = grounded.design
    order = list(design.placement.order)
    order += [block for block in design.blocks if block not in order]
    columns = [list(design.blocks[block]) for block in order]
    placed = {ref for column in columns for ref in column}
    rest = [ref for ref in design.components if ref not in placed]
    if rest:
        columns.append(rest)

    outlines = {
        ref: _outline(grounded, libraries, endings, ref) for ref in design.components
    }
    # A block with many parts continues in a further column beside its
    # first, so a sheet grows sideways as it reads, not downward.
    wrapped: list[list[_Item]] = []
    owner: list[int] = []
    for block, refs in enumerate(columns):
        current: list[_Item] = []
        height = 0.0
        for item in _items(grounded, outlines, refs, spread):
            needed = item.outline.height + _ROW_GAP * spread
            if current and height + needed > _COLUMN_HEIGHT:
                wrapped.append(current)
                owner.append(block)
                current, height = [], 0.0
            current.append(item)
            height += needed
        wrapped.append(current)
        owner.append(block)

    # One row of blocks on the smallest sheet that holds it, as always;
    # failing that, the blocks wrapped into bands on the smallest sheet
    # that holds those.
    placements, right, lowest = _lay(wrapped, owner, spread, None)
    for name, width, height in PAPERS:
        if _fits(right, lowest, width, height):
            return placements, name
    for name, width, height in PAPERS:
        placements, right, lowest = _lay(wrapped, owner, spread, width - _MARGIN)
        if _fits(right, lowest, width, height):
            return placements, name
    raise WriterError(
        f"the design needs {right:.0f} x {lowest:.0f} mm even in bands on the "
        "largest sheet; split it into more sheets"
    )


def _arrange(
    grounded: Grounded,
    libraries: Libraries,
    endings: dict[tuple[str, str], _Ending],
    spread: float,
) -> tuple[dict[str, Placement], str, tuple[_Frame, ...]]:
    """Place each block's parts for wiring, frame the blocks and choose the paper.

    Inside a block the parts are arranged by `arrange_block`; a part of
    three pins or fewer may be turned. The framed blocks are then packed in
    reading order onto the smallest sheet that holds them. ``spread``
    widens the gap kept around each part, for a drawing whose texts found
    no room.
    """
    design = grounded.design
    order = list(design.placement.order)
    order += [block for block in design.blocks if block not in order]
    groups = [(name, list(design.blocks[name])) for name in order]
    placed = {ref for _, refs in groups for ref in refs}
    rest = [ref for ref in design.components if ref not in placed]
    if rest:
        groups.append(("", rest))
    block_of = {ref: name for name, refs in groups for ref in refs}
    pad = max(1, math.floor(spread))
    pins_by_ref: dict[str, list[GroundedPin]] = {}
    for pin in grounded.pins:
        pins_by_ref.setdefault(pin.ref, []).append(pin)
    # A net joined by wire within a block: one that ends in labels and has
    # another pin in the same block.
    in_block: dict[tuple[str, str], int] = {}
    # Each wired group keeps one label, on its wires or failing that on its
    # first pin's stub, and a flagged pin keeps its stub and label whatever
    # happens: those pins keep room for their label.
    labelled: set[tuple[str, str]] = set()
    for pin in grounded.pins:
        ending = endings[(pin.ref, pin.pin.number)]
        if not ending.symbol:
            key = (block_of[pin.ref], pin.net)
            if key not in in_block or ending.flag:
                labelled.add((pin.ref, pin.pin.number))
            in_block[key] = in_block.get(key, 0) + 1

    def part_of(ref: str) -> str:
        return design.components[ref].lib_id

    def value_of(ref: str) -> str | None:
        return design.components[ref].value

    def piece(ref: str) -> Piece:
        wired = frozenset(
            pin.net
            for pin in pins_by_ref.get(ref, [])
            if in_block.get((block_of[ref], pin.net), 0) >= 2
            and (ref, pin.pin.number) not in labelled
        )
        turns: tuple[Rotation, ...] = (
            (0, 90, 180, 270) if _pin_count(grounded, ref) <= _TURNABLE_PINS else (0,)
        )
        shapes: list[Shape] = []
        for turn in turns:
            placement = Placement(0.0, 0.0, turn)
            box = _with_texts(
                _outline(grounded, libraries, endings, ref, turn, wired, rounded=True),
                placed_box(
                    placement,
                    symbol_box(
                        libraries.embedded(part_of(ref)), design.components[ref].unit
                    ),
                ),
                [
                    t
                    for _, t in _visible(
                        libraries.embedded(part_of(ref)), ref, value_of(ref)
                    )
                ],
            )
            pins: list[PinEnd] = []
            for pin in pins_by_ref.get(ref, []):
                x, y = to_sheet(placement, pin.pin.x, pin.pin.y)
                ending = endings[(ref, pin.pin.number)]
                points = None
                if ending.symbol:
                    body = symbol_box(libraries.embedded(ending.symbol))
                    points = (0, -1) if -body.top <= body.bottom else (0, 1)
                pins.append(
                    PinEnd(
                        pin.net,
                        (round(x / GRID), round(y / GRID)),
                        pin_outward(placement, pin.pin.angle),
                        points,
                    )
                )
            room = (
                math.floor(box.left / GRID + 1e-6),
                math.floor(box.top / GRID + 1e-6),
                math.ceil(box.right / GRID - 1e-6),
                math.ceil(box.bottom / GRID - 1e-6),
            )
            shapes.append(Shape(turn, room, tuple(pins)))
        return Piece(ref, tuple(shapes))

    # Blocks built alike are arranged alike (`structure`), so each such
    # structure is arranged once and its spots given to the others in order.
    done: dict[str, tuple[list[Spot], int, int]] = {}
    blocks: list[tuple[str, Block]] = []
    for name, refs in groups:
        pieces = [piece(ref) for ref in refs]
        key = structure(pieces)
        if key not in done:
            first = arrange_block(pieces, pad=pad)
            done[key] = ([first.spots[ref] for ref in refs], first.width, first.height)
        spots, width, height = done[key]
        blocks.append((name, Block(dict(zip(refs, spots, strict=True)), width, height)))
    sizes = [
        (
            block.width * GRID + 2 * _FRAME_PAD,
            block.height * GRID + 2 * _FRAME_PAD + _TITLE,
        )
        for _, block in blocks
    ]
    paper, corners = _paper_for(sizes)
    placements: dict[str, Placement] = {}
    frames: list[_Frame] = []
    for (name, block), (x, y), (w, h) in zip(blocks, corners, sizes, strict=True):
        left = _snap_up(_MARGIN + x)
        top = _snap_up(_MARGIN + y)
        frames.append(
            _Frame(
                name or "other", Box(left, top, round(left + w, 4), round(top + h, 4))
            )
        )
        origin_x = _snap_up(left + _FRAME_PAD)
        origin_y = _snap_up(top + _FRAME_PAD + _TITLE)
        pieces = {ref: spot for ref, spot in block.spots.items()}
        for ref, spot in pieces.items():
            turn = _turns_of(grounded, ref)[spot.shape]
            placements[ref] = Placement(
                round(origin_x + spot.x * GRID, 4),
                round(origin_y + spot.y * GRID, 4),
                turn,
            )
    return placements, paper, tuple(frames)


def _letters(text: str, rounded: bool) -> int:
    """How many characters of room a text is given.

    For a wired drawing the count is rounded up to a whole four, so blocks
    built alike whose names differ by a digit (CELL9, CELL10) need the
    same room and are arranged alike.
    """
    return -(-len(text) // 4) * 4 if rounded else len(text)


def _with_texts(outline: Box, body: Box, texts: list[str]) -> Box:
    """A part's room with space beside its body for its reference and value.

    The text placer puts them beside the body, so a part packed close to
    its neighbours needs that space kept: to the right of a body taller
    than it is wide, which is how a turned resistor or an upright
    capacitor stands, and above and below one wider than tall, such as a
    resistor laid along its wires or a chip.
    """
    if not texts:
        return outline
    longest = max(_letters(t, True) for t in texts) * _CHARACTER + GRID
    if body.height > body.width:
        return Box(
            outline.left,
            min(outline.top, body.top),
            max(outline.right, body.right + longest),
            max(outline.bottom, body.bottom),
        )
    return Box(
        min(outline.left, (body.left + body.right - longest) / 2),
        min(outline.top, body.top - _TEXT_HEIGHT),
        max(outline.right, (body.left + body.right + longest) / 2),
        max(outline.bottom, body.bottom + _TEXT_HEIGHT),
    )


def _paper_for(
    sizes: list[tuple[float, float]],
) -> tuple[str, list[tuple[float, float]]]:
    """The smallest sheet that holds framed blocks packed in reading order.

    Returns the sheet's name and each block's top-left corner from the
    drawing's own corner, which sits at the margin.

    Raises:
        WriterError: If not even the largest sheet holds them.
    """
    padded = [(w + _COLUMN_GAP, h + _COLUMN_GAP) for w, h in sizes]
    right = lowest = 0.0
    for paper, width, height in PAPERS:
        corners = pack(padded, width - 2 * _MARGIN)
        right = max(x + w for (x, _), (w, _) in zip(corners, sizes, strict=True))
        lowest = max(y + h for (_, y), (_, h) in zip(corners, sizes, strict=True))
        if _fits(_MARGIN + right, _MARGIN + lowest, width, height):
            return paper, corners
    raise WriterError(
        f"the design needs {right:.0f} x {lowest:.0f} mm even packed on the "
        "largest sheet; split it into more sheets"
    )


def _turns_of(grounded: Grounded, ref: str) -> tuple[Rotation, ...]:
    """The turns `_arrange` offers a part, in its order."""
    if _pin_count(grounded, ref) <= _TURNABLE_PINS:
        return (0, 90, 180, 270)
    return (0,)


def _fits(right: float, lowest: float, width: float, height: float) -> bool:
    """Whether a drawing fits a sheet, clear of the title block's corner."""
    return right + _MARGIN <= width and lowest + _MARGIN + 40 <= height


def _lay(
    wrapped: list[list[_Item]],
    owner: list[int],
    spread: float,
    limit: float | None,
) -> tuple[dict[str, Placement], float, float]:
    """Lay columns left to right, starting a new band below at ``limit``.

    A block's columns go in one band, unless the block alone is wider
    than a band. Returns the placements, the rightmost and the lowest
    extent.
    """
    placements: dict[str, Placement] = {}
    cursor_x = _MARGIN
    band_top = _MARGIN
    lowest = _MARGIN
    right = _MARGIN
    first = 0
    while first < len(wrapped):
        last = first
        while last + 1 < len(wrapped) and owner[last + 1] == owner[first]:
            last += 1
        block = wrapped[first : last + 1]
        end = _columns_end(block, cursor_x, spread)
        if limit is not None and end > limit and cursor_x > _MARGIN:
            cursor_x = _MARGIN
            band_top = lowest + _ROW_GAP * spread
        for items in block:
            if (
                limit is not None
                and cursor_x > _MARGIN
                and _columns_end([items], cursor_x, spread) > limit
            ):
                cursor_x = _MARGIN
                band_top = lowest + _ROW_GAP * spread
            # Every item of a column shares one x, far enough right that
            # the widest reach to the left clears the column before.
            x = _snap_up(cursor_x - min(item.outline.left for item in items))
            cursor_y = band_top
            for item in items:
                y = _snap_up(cursor_y - item.outline.top)
                for ref, dx, dy in item.members:
                    placements[ref] = Placement(round(x + dx, 4), round(y + dy, 4))
                cursor_y = y + item.outline.bottom + _ROW_GAP * spread
            lowest = max(lowest, cursor_y - _ROW_GAP * spread)
            edge = x + max(item.outline.right for item in items)
            right = max(right, edge)
            cursor_x = edge + _COLUMN_GAP * spread
        first = last + 1
    return placements, right, lowest


def _columns_end(columns: list[list[_Item]], cursor_x: float, spread: float) -> float:
    """Where the last of these columns would end, laid from ``cursor_x``."""
    edge = cursor_x
    for items in columns:
        x = _snap_up(cursor_x - min(item.outline.left for item in items))
        edge = x + max(item.outline.right for item in items)
        cursor_x = edge + _COLUMN_GAP * spread
    return edge


def _snap_up(value: float) -> float:
    """The first grid point at or after a length."""
    steps = -(-round(value, 4) // GRID)
    return round(steps * GRID, 4)


def rectangle(box: Box, uid: str) -> SList:
    """A drawn rectangle, as KiCad 10 saves one (the pedalboard's Pico sheet)."""
    return lst(
        "rectangle",
        lst("start", box.left, box.top),
        lst("end", box.right, box.bottom),
        lst("stroke", lst("width", 0), lst("type", sym("default"))),
        lst("fill", lst("type", sym("none"))),
        lst("uuid", uid),
    )


def text(words: str, at: tuple[float, float], size: float, uid: str) -> SList:
    """A free text anchored at its bottom left, as KiCad 10 saves one."""
    return lst(
        "text",
        words,
        lst("exclude_from_sim", sym("no")),
        lst("at", *at, 0),
        lst(
            "effects",
            lst("font", lst("size", size, size)),
            lst("justify", sym("left"), sym("bottom")),
        ),
        lst("uuid", uid),
    )


def junction(at: tuple[float, float], uid: str) -> SList:
    """A junction dot, as KiCad 10 saves one (the pedalboard board's sheets)."""
    return lst(
        "junction",
        lst("at", *at),
        lst("diameter", 0),
        lst("color", 0, 0, 0, 0),
        lst("uuid", uid),
    )


def wire(start: tuple[float, float], end: tuple[float, float], uid: str) -> SList:
    """A wire between two points, as KiCad 10 saves one."""
    return lst(
        "wire",
        lst("pts", lst("xy", *start), lst("xy", *end)),
        lst("stroke", lst("width", 0), lst("type", sym("default"))),
        lst("uuid", uid),
    )


# A label's angle and the side its text is anchored, for each way a stub
# can run; these are the pairs KiCad saves (counted on the real boards).
_LABEL_TURN: dict[tuple[int, int], tuple[int, str]] = {
    (1, 0): (0, "left"),
    (-1, 0): (180, "right"),
    (0, -1): (90, "left"),
    (0, 1): (270, "right"),
}


def _font() -> SList:
    return lst("font", lst("size", 1.27, 1.27))


def label(net: str, at: tuple[float, float], step: tuple[int, int], uid: str) -> SList:
    """A local label at the end of a stub that runs the way ``step`` says."""
    angle, side = _LABEL_TURN[step]
    return lst(
        "label",
        net,
        lst("at", *at, angle),
        lst("effects", _font(), lst("justify", sym(side), sym("bottom"))),
        lst("uuid", uid),
    )


def global_label(
    net: str, at: tuple[float, float], step: tuple[int, int], uid: str
) -> SList:
    """A global label at the end of a stub that runs the way ``step`` says."""
    angle, side = _LABEL_TURN[step]
    return lst(
        "global_label",
        net,
        lst("shape", sym("passive")),
        lst("at", *at, angle),
        lst("effects", _font(), lst("justify", sym(side))),
        lst("uuid", uid),
        lst(
            "property",
            "Intersheetrefs",
            "${INTERSHEET_REFS}",
            lst("at", *at, 0),
            lst("hide", sym("yes")),
            lst("show_name", sym("no")),
            lst("do_not_autoplace", sym("no")),
            lst("effects", _font(), lst("justify", sym(side))),
        ),
    )


def hierarchical_label(
    net: str, shape: str, at: tuple[float, float], step: tuple[int, int], uid: str
) -> SList:
    """A hierarchical label at the end of a stub, in the form KiCad 10.0.7 saves."""
    angle, side = _LABEL_TURN[step]
    return lst(
        "hierarchical_label",
        net,
        lst("shape", sym(shape)),
        lst("at", *at, angle),
        lst("effects", _font(), lst("justify", sym(side))),
        lst("uuid", uid),
    )


def title_block(design: Design, title: str) -> SList:
    """A sheet's title block, its fields in the order KiCad saves them.

    A design that states nothing beyond its name writes the title alone,
    as before.
    """
    stated = design.title_block
    fields: list[SList] = [lst("title", title)]
    if stated.date:
        fields.append(lst("date", stated.date))
    if stated.revision:
        fields.append(lst("rev", stated.revision))
    if stated.company:
        fields.append(lst("company", stated.company))
    for number, line in enumerate(stated.comments, start=1):
        fields.append(lst("comment", number, line))
    return lst("title_block", *fields)


def _flag(symbol: SList, head: str, default: str) -> Atom:
    node = symbol.first(head)
    if node is not None and len(node.items) > 1 and isinstance(node.items[1], Atom):
        return sym(node.items[1].raw)
    return sym(default)


def placed_symbol(
    symbol: SList,
    lib_id: str,
    placement: Placement,
    *,
    reference: str,
    value: str | None,
    footprint: str | None,
    unit: int,
    pins: list[str],
    project: str,
    root: str,
    uuid_of: Callable[[str], str],
    fields: Mapping[str, FieldSpot | None],
    simulation: Sequence[tuple[str, str]] = (),
) -> SList:
    """A placed symbol, with each shown field at the spot found for it.

    A field with a spot of ``None`` had no clear place and is hidden. A
    field not in ``fields`` keeps the library's position, which only
    hidden fields do. Each ``simulation`` field replaces the library's
    field of that name, or is added after them, hidden at the symbol's
    position as KiCad's re-save puts its own.
    """
    if placement.mirror:
        raise WriterError("only unmirrored placement is written so far")
    given = {
        "Reference": reference,
        "Value": value,
        "Footprint": footprint,
        **dict(simulation),
    }
    properties: list[SList] = []
    for node in symbol.lists("property"):
        name_atom, value_atom = node.items[1], node.items[2]
        assert isinstance(name_atom, Atom) and isinstance(value_atom, Atom)
        name = name_atom.value
        if name.startswith("ki_"):
            continue
        text = given.get(name) or value_atom.value
        at = node.first("at")
        assert at is not None
        x, y, angle = (float(v) for v in at.values())
        hide = node.first("hide")
        hidden = hide is not None and hide.values() == ["yes"]
        if name == "Reference" and reference.startswith("#"):
            hidden = True
        spot = fields.get(name) if name in fields and not hidden else None
        if name in fields and spot is None:
            hidden = True
        if spot is not None:
            # KiCad draws a field at its own angle plus the symbol's
            # (measured with kicad-cli's render of KiCad 10.0.6), so the
            # angle written is the one wanted less the symbol's turn.
            written = (spot.angle - placement.rotation) % 180
            parts: list[SList] = [lst("at", spot.x, spot.y, written)]
        else:
            parts = [lst("at", *to_sheet(placement, x, y), angle)]
        if hidden:
            parts.append(lst("hide", sym("yes")))
        for head in ("show_name", "do_not_autoplace"):
            parts.append(lst(head, _flag(node, head, "no")))
        if spot is not None:
            # Centred on its spot, which is how the spot was measured.
            parts.append(lst("effects", _font()))
        else:
            effects = node.first("effects")
            assert effects is not None
            duplicate = copy(effects)
            assert isinstance(duplicate, SList)
            parts.append(duplicate)
        properties.append(lst("property", name, text, *parts))
    present = {
        atom.value
        for node in symbol.lists("property")
        if isinstance(atom := node.items[1], Atom)
    }
    for name, text in simulation:
        if name in present:
            continue
        properties.append(
            lst(
                "property",
                name,
                text,
                lst("at", placement.x, placement.y, 0),
                lst("hide", sym("yes")),
                lst("show_name", sym("no")),
                lst("do_not_autoplace", sym("no")),
                lst("effects", _font()),
            )
        )
    return lst(
        "symbol",
        lst("lib_id", lib_id),
        lst("at", placement.x, placement.y, placement.rotation),
        lst("unit", unit),
        lst("body_style", 1),
        lst("exclude_from_sim", _flag(symbol, "exclude_from_sim", "no")),
        lst("in_bom", _flag(symbol, "in_bom", "yes")),
        lst("on_board", _flag(symbol, "on_board", "yes")),
        lst("in_pos_files", _flag(symbol, "in_pos_files", "yes")),
        lst("dnp", sym("no")),
        lst("uuid", uuid_of("symbol")),
        *properties,
        *[lst("pin", number, lst("uuid", uuid_of(f"pin/{number}"))) for number in pins],
        lst(
            "instances",
            lst(
                "project",
                project,
                lst("path", f"/{root}", lst("reference", reference), lst("unit", unit)),
            ),
        ),
    )
