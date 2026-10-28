"""Add a part to, or remove one from, a schematic a person drew.

Each change touches only the subtrees it must: a new part is one placed
symbol with a short wire and a label or power symbol on each pin, set
down in free space; a removed part takes with it only the wires, labels
and power symbols that then lead nowhere. Nothing a person placed is
moved. The caller writes the changed files through
`kairo.kicad.transaction` and has KiCad confirm the connections, since
this module works out connections from the drawing and KiCad is the
judge of them.

What was read from files KiCad 10 saved (``tests/fixtures/real``,
pedalboard and the CM5 carrier): a placed symbol's ``instances`` path is
the root sheet's identifier followed by the identifier of each
``sheet`` box on the way down; a symbol drawn from a renamed embedded
copy carries ``lib_name``; a net local to a sheet is named in KiCad's
netlist by the sheet's name path and the label's text.

A part is added only to a file in KiCad 10's format, because the symbol
written is the one `kairo.kicad.schematic_writer` writes, and only its
KiCad 10 output has been compared with KiCad's own.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from kairo.kicad.edit import EditError, read_sheet, references
from kairo.kicad.emit import copy, lay_out, lst
from kairo.kicad.emit import number as length_atom
from kairo.kicad.geometry import (
    GRID,
    Box,
    Mirror,
    Placement,
    Rotation,
    pin_outward,
    placed_box,
    symbol_box,
    to_sheet,
)
from kairo.kicad.schematic_writer import (
    FORMAT_VERSION,
    NAMESPACE,
    PAPERS,
    Libraries,
    WriterError,
    global_label,
    label,
    placed_symbol,
    wire,
)
from kairo.kicad.sexpr import Atom, Document, SList
from kairo.library import LibraryIndex
from kairo.library.reader import Symbol
from kairo.library.resolve import PinError, pin_label, resolve_pin

#: Paper sizes beside the writer's, as width and height in mm, landscape.
_MORE_PAPERS: dict[str, tuple[float, float]] = {
    "A5": (210.0, 148.0),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4),
}
#: The margin kept clear inside the paper's edge, in mm.
_FRAME = 12.0
#: The corner kept clear for the title block, as width and height in mm.
#: KiCad's default title block is 108 mm by 32 mm; this is a little more.
_TITLE_BLOCK = (112.0, 36.0)
#: How long a new pin's wire is, in mm.
_STUB = 2 * GRID
#: The pitch of the positions tried for a new part, in mm.
_STEP = 4 * GRID
#: The order KiCad saves a sheet's items in, by kind.
_KINDS = (
    "junction",
    "no_connect",
    "bus_entry",
    "wire",
    "bus",
    "polyline",
    "label",
    "global_label",
    "hierarchical_label",
    "symbol",
    "sheet",
    "sheet_instances",
    "embedded_fonts",
)
_UNIT_NAME = re.compile(r"_(\d+)_(\d+)$")
_LABELS = ("label", "global_label", "hierarchical_label")
_COORDINATES = ("at", "xy", "start", "end", "mid", "center")
_NOT_DRAWN = (
    "version",
    "generator",
    "generator_version",
    "uuid",
    "paper",
    "title_block",
    "lib_symbols",
    "sheet_instances",
    "symbol_instances",
    "embedded_fonts",
)
Point = tuple[float, float]


@dataclass(slots=True)
class Sheet:
    """One sheet file of a project.

    Attributes:
        path: The file.
        document: Its lossless tree, changed in place by an edit.
        instances: The identifier path of each place the sheet is used,
            such as ``/<root>/<sheet box>``.
        names: The name path of each place, as KiCad's netlist prefixes a
            local net: ``/`` for the root, ``/Power Supply/`` below it.
        changed: True once an edit has changed the tree.
        original: The file's text as it was read.
    """

    path: Path
    document: Document
    instances: list[str]
    names: list[str]
    changed: bool = False
    original: str = ""


class Project:
    """Every sheet of a drawn project, parsed once and edited in memory.

    Args:
        root: The root ``.kicad_sch`` file.

    Raises:
        EditError: ``E_NOT_A_SCHEMATIC`` if a sheet does not parse.
    """

    def __init__(self, root: Path) -> None:
        """Read the root sheet and every sheet below it."""
        self.root = root.resolve()
        self.folder = self.root.parent
        self.name = self.root.stem
        self.sheets: dict[Path, Sheet] = {}
        document = read_sheet(self.root)
        self._walk(self.root, document, f"/{_uuid(document.root)}", "/", 0)

    def _walk(
        self, path: Path, document: Document, instance: str, name: str, depth: int
    ) -> None:
        sheet = self.sheets.setdefault(
            path, Sheet(path, document, [], [], original=document.to_text())
        )
        sheet.instances.append(instance)
        sheet.names.append(name)
        if depth > 32:
            raise EditError("E_NOT_A_SCHEMATIC", f"{path.name} contains itself")
        for box in sheet.document.root.lists("sheet"):
            properties = {
                values[0]: values[1]
                for prop in box.lists("property")
                if len(values := prop.values()) >= 2
            }
            file_name = properties.get("Sheetfile") or properties.get("Sheet file")
            sheet_name = properties.get("Sheetname") or properties.get("Sheet name")
            if not file_name or sheet_name is None:
                continue
            child = (path.parent / file_name).resolve()
            if not child.is_file():
                continue
            known = self.sheets.get(child)
            self._walk(
                child,
                known.document if known else read_sheet(child),
                f"{instance}/{_uuid(box)}",
                f"{name}{sheet_name}/",
                depth + 1,
            )

    def files(self) -> dict[str, str]:
        """The changed files, by name within the project folder."""
        return {
            sheet.path.relative_to(self.folder).as_posix(): sheet.document.to_text()
            for sheet in self.sheets.values()
            if sheet.changed
        }

    def all_references(self) -> set[str]:
        """Every reference in the project, power symbols included."""
        return {
            ref
            for sheet in self.sheets.values()
            for symbol in _placed(sheet.document)
            for ref in references(symbol, self.name)
        }

    def sheet_of(self, ref: str) -> Sheet | None:
        """The sheet a part is drawn on, or ``None`` if no sheet has it."""
        for sheet in self.sheets.values():
            if any(ref in references(s, self.name) for s in _placed(sheet.document)):
                return sheet
        return None


@dataclass(frozen=True, slots=True)
class Added:
    """What adding a part did.

    Attributes:
        ref: The new part's reference.
        file: The sheet file it was drawn on, within the project folder.
        at: Where its origin was put, in mm; for a part of several
            units, where unit 1's was.
        endings: For each connected pin number, the net and what its wire
            ends in: ``label``, ``global label`` or ``power symbol``.
        open_pins: The pin numbers marked not connected.
        embedded: True if the symbol was added to the file's own symbols.
        power: The references of the power symbols added.
        units: Where each unit's origin was put, unit 1 first; one entry
            for a part of one unit.
    """

    ref: str
    file: str
    at: Point
    endings: dict[str, tuple[str, str]]
    open_pins: tuple[str, ...]
    embedded: bool
    power: tuple[str, ...]
    units: tuple[Point, ...] = ()


@dataclass(frozen=True, slots=True)
class Removed:
    """What removing a part did.

    Attributes:
        ref: The part's reference.
        file: The sheet file it was on, within the project folder.
        symbols: How many placed symbols were removed; more than one for
            a part drawn as several units.
        wires: How many wires that then led nowhere were removed.
        labels: The text of each label removed with them.
        power: The references of the power symbols removed with them.
        marks: How many no-connect marks and junction dots were removed.
        left: What was left that a person may want to look at.
        shortened: How many wires were cut back to where something else
            joins them.
    """

    ref: str
    file: str
    symbols: int
    wires: int
    labels: tuple[str, ...]
    power: tuple[str, ...]
    marks: int
    left: tuple[str, ...]
    shortened: int = 0


@dataclass(frozen=True, slots=True)
class Moved:
    """What moving a pin to another net did.

    Attributes:
        pin: The pin, as ``REF.NUMBER``.
        file: The sheet file it is on, within the project folder.
        net: The net it was joined to.
        ending: What the new wire ends in.
        wires: How many wires that then led nowhere were removed.
        labels: The text of each label removed with them.
        power: The references of the power symbols removed with them.
        added_power: The references of the power symbols added.
        left: What was left that a person may want to look at.
        shortened: How many wires were cut back to where something else
            joins them.
    """

    pin: str
    file: str
    net: str
    ending: str
    wires: int
    labels: tuple[str, ...]
    power: tuple[str, ...]
    added_power: tuple[str, ...]
    left: tuple[str, ...]
    shortened: int = 0


def add_part(
    project: Project,
    index: LibraryIndex,
    *,
    ref: str,
    lib_id: str,
    value: str,
    footprint: str | None,
    pins: Mapping[str, str],
    no_connect: Sequence[str] = (),
    beside: str | None = None,
    sheet: str | None = None,
) -> Added:
    """Draw a new part in free space on a sheet, joined to nets by name.

    Each connected pin gets a short wire that ends in a power symbol, if
    the project already draws that net with one; else in a global label
    for a net with a project-wide name, or a local label for a net of
    this sheet.

    A part of several units (an op amp's two amplifiers and its supply)
    is drawn whole, each unit in free space near the one before, so
    every pin of every unit needs a net or a place under ``no_connect``.
    A pin the symbol shares between all its units is drawn on each, and
    each gets its own wire to the same net, so it is joined whatever
    KiCad makes of the copies.

    Args:
        project: The project, changed in memory.
        index: The library index, for the symbol and its pins.
        ref: The new part's reference, which no part may have yet.
        lib_id: Its symbol, as ``library:name``.
        value: Its value.
        footprint: Its footprint, or ``None`` for the symbol's own.
        pins: For each pin, by name or number, the net it joins, named as
            KiCad's netlist names it. A name found in more than one unit
            must be given by number, or written ``NAME*`` for all.
        no_connect: The pins left open on purpose.
        beside: A part already on the sheet to draw the new one near.
        sheet: The sheet file to draw on, when ``beside`` is not given.

    Returns:
        Where the part went and what each pin ends in.

    Raises:
        EditError: With a code that says what stands in the way; see the
            messages. Nothing is changed when it is raised.
    """
    if ref in project.all_references():
        raise EditError("E_REF_EXISTS", f"the project already has a part {ref}")
    target = _target_sheet(project, beside, sheet)
    root = target.document.root
    _need_format(target)
    if len(target.instances) != 1:
        raise EditError(
            "E_SHARED_SHEET",
            f"{target.path.name} is used {len(target.instances)} times, so one "
            "symbol drawn there would be several parts",
        )
    symbol = index.symbol(lib_id)
    if symbol is None:
        raise EditError("E_SYMBOL_UNKNOWN", f"the libraries have no symbol {lib_id}")
    nets: dict[str, str] = {}
    left_open: set[str] = set()
    for name, net in pins.items():
        for number in _pin_numbers(symbol, ref, name):
            if nets.setdefault(number, net) != net:
                raise EditError(
                    "E_PIN_TWICE",
                    f"{ref} pin {number} is given both {nets[number]} and {net}",
                )
    for name in no_connect:
        left_open.update(_pin_numbers(symbol, ref, name))
    both = sorted(left_open & nets.keys())
    if both:
        raise EditError(
            "E_PIN_TWICE",
            f"{ref} pin {', '.join(both)} is given a net and listed under no_connect",
        )
    missing = [
        pin_label(pin)
        for pin in symbol.pins
        if pin.number not in nets and pin.number not in left_open
    ]
    if missing:
        raise EditError(
            "E_PIN_UNCONNECTED",
            f"{ref} has pins with no net: {', '.join(missing)}. Give each a net, "
            "or list it under no_connect",
        )
    defs = _defs(target.document)
    embedded = lib_id not in defs
    if embedded:
        try:
            definition = copy(Libraries(index).embedded(lib_id))
        except WriterError as exc:
            raise EditError("E_SYMBOL_UNKNOWN", str(exc)) from exc
        assert isinstance(definition, SList)
    else:
        definition = defs[lib_id]
    units = range(1, symbol.unit_count + 1)
    unit_pins = {unit: _definition_pins(definition, unit, 1) for unit in units}
    if set().union(*unit_pins.values()) != {pin.number for pin in symbol.pins}:
        raise EditError(
            "E_SYMBOL_DIFFERS",
            f"{target.path.name} holds its own copy of {lib_id} whose pins are "
            "not the library's, so the library's pin names cannot be trusted "
            "for it",
        )
    endings = {
        number: _ending(project, target, net) for number, net in sorted(nets.items())
    }
    origin = Placement(0.0, 0.0)
    centre = _centre(target, beside, project.name)
    area, corner = _usable(root)
    obstacles = [*_obstacles(target.document, defs), corner]
    base = _uuid(root)
    newline = _newline(target.document)

    def uid(owner: str, what: str) -> str:
        return str(uuid.uuid5(NAMESPACE, f"{base}/{owner}/{what}"))

    new: list[SList] = []
    spots_taken: list[Point] = []
    power_refs: list[str] = []
    power_numbers = _power_numbers(project)
    spots: dict[Point, str] = {}
    for unit in units:
        drawn_pins = unit_pins[unit]
        # Unit 1 keeps the names a single-unit part has always had, so an
        # add of one is written as it was; a pin common to every unit is
        # drawn on each, so each further unit needs names of its own.
        suffix = "" if unit == 1 else f"/unit{unit}"
        here = {n: e for n, e in endings.items() if n in drawn_pins}
        own = _own_box(definition, ref, value, here, unit, symbol.unit_count > 1)
        spot = _free_spot(own, centre, area, obstacles)
        if spot is None:
            which = f"unit {unit} of {ref}" if symbol.unit_count > 1 else ref
            raise EditError(
                "E_NO_ROOM",
                f"{target.path.name} has no free space for {which} "
                f"({own.width:.0f} mm by {own.height:.0f} mm with its labels)",
            )
        spots_taken.append(spot)
        # The next unit goes near this one, clear of it and its labels.
        centre = spot
        obstacles.append(
            Box(
                spot[0] + own.left,
                spot[1] + own.top,
                spot[0] + own.right,
                spot[1] + own.bottom,
            )
        )
        placement = Placement(spot[0], spot[1])
        new.append(
            placed_symbol(
                definition,
                lib_id,
                placement,
                reference=ref,
                value=value,
                footprint=footprint,
                unit=unit,
                pins=list(drawn_pins),
                project=project.name,
                root=target.instances[0][1:],
                uuid_of=lambda what, owner=f"{ref}{suffix}": uid(owner, what),
                fields={},
            )
        )
        for number, ending in here.items():
            x, y, angle = drawn_pins[number]
            start = to_sheet(placement, x, y)
            if start in spots:
                if spots[start] != ending.net:
                    raise EditError(
                        "E_PIN_TWICE",
                        f"{ref} pin {number} shares its place with a pin given "
                        f"{spots[start]}, so both are on one net",
                    )
                # A stacked pin: the wire already drawn there serves it too.
                continue
            spots[start] = ending.net
            step = pin_outward(origin, angle)
            end = (
                round(start[0] + step[0] * _STUB, 4),
                round(start[1] + step[1] * _STUB, 4),
            )
            tag = f"{ref}.{number}{suffix}"
            new.append(wire(start, end, uid(tag, "wire")))
            if ending.kind == "power symbol":
                assert ending.definition is not None
                if ending.symbol not in defs:
                    held = copy(ending.definition)
                    assert isinstance(held, SList)
                    _embed(root, held, newline)
                    defs[ending.symbol] = held
                power_ref = _next_power(power_numbers)
                power_refs.append(power_ref)
                new.append(
                    placed_symbol(
                        defs[ending.symbol],
                        ending.symbol,
                        Placement(*end),
                        reference=power_ref,
                        value=None,
                        footprint=None,
                        unit=1,
                        pins=list(_definition_pins(defs[ending.symbol], 1, 1)),
                        project=project.name,
                        root=target.instances[0][1:],
                        uuid_of=lambda what, tag=tag: uid(f"{tag}/power", what),
                        fields={},
                    )
                )
            elif ending.kind == "global label":
                new.append(global_label(ending.text, end, step, uid(tag, "label")))
            else:
                new.append(label(ending.text, end, step, uid(tag, "label")))
        for number in sorted(left_open & drawn_pins.keys()):
            x, y, _ = drawn_pins[number]
            at = to_sheet(placement, x, y)
            if at in spots:
                raise EditError(
                    "E_PIN_TWICE",
                    f"{ref} pin {number} is listed under no_connect and shares "
                    f"its place with a pin on {spots[at]}",
                )
            new.append(
                lst(
                    "no_connect",
                    lst("at", *at),
                    lst("uuid", uid(f"{ref}.{number}{suffix}", "nc")),
                )
            )
    if embedded:
        _embed(root, definition, newline)
    for node in new:
        _insert(root, node, newline)
    target.changed = True
    return Added(
        ref,
        target.path.relative_to(project.folder).as_posix(),
        spots_taken[0],
        {number: (ending.net, ending.kind) for number, ending in endings.items()},
        tuple(sorted(left_open)),
        embedded,
        tuple(power_refs),
        tuple(spots_taken),
    )


def remove_part(project: Project, ref: str) -> Removed:
    """Take a part off its sheet, with the wires that then lead nowhere.

    From each of the part's pins the drawing is followed outward: a wire
    whose end held only that pin is removed, and so on along the run,
    until a point where something else is attached. A label or power
    symbol left at the very end of such a run, serving nothing, goes too.
    A hierarchical label is left, since it is the sheet's interface.

    Args:
        project: The project, changed in memory.
        ref: The part's reference.

    Returns:
        What was removed and what was left for a person to look at.

    Raises:
        EditError: ``E_REF_NOT_FOUND``; ``E_SHARED_SHEET`` if the part is
            on a sheet used more than once; ``E_SYMBOL_MISSING`` if the
            sheet draws a symbol whose pins cannot be read, so what is
            attached where cannot be known.
    """
    target = project.sheet_of(ref)
    if target is None:
        raise EditError("E_REF_NOT_FOUND", f"no symbol in the project is {ref}")
    if len(target.instances) != 1:
        raise EditError(
            "E_SHARED_SHEET",
            f"{ref} is on a sheet used {len(target.instances)} times, where "
            "removing it would remove several parts",
        )
    root = target.document.root
    defs = _defs(target.document)
    doomed = [s for s in _placed(target.document) if ref in references(s, project.name)]
    frontier: list[Point] = []
    for symbol in doomed:
        frontier.extend(_pin_points(symbol, defs, target.path.name).values())
    drawing = _Drawing(target, defs, doomed)
    cut = _follow(drawing, frontier, target.path.name, project.name)
    gone = [*doomed, *cut.nodes]
    doomed_ids = {id(node) for node in gone}
    root.items[:] = [item for item in root.items if id(item) not in doomed_ids]
    target.changed = True
    return Removed(
        ref,
        target.path.relative_to(project.folder).as_posix(),
        len(doomed),
        cut.wires,
        tuple(cut.labels),
        tuple(cut.power),
        cut.marks,
        tuple(cut.left),
        cut.shortened,
    )


def reconnect(project: Project, pin: str, net: str) -> Moved:
    """Take one pin of a part off its net and join it to another by name.

    The wire that served only that pin is removed as `remove_part` removes
    one, and a short new wire is drawn from the pin to a label or power
    symbol for the net. A pin that wires pass through, or that touches
    another part's pin directly, is refused: freeing it would mean
    redrawing what a person drew.

    Args:
        project: The project, changed in memory.
        pin: The pin as ``REF.NUMBER``.
        net: The net to join, named as KiCad's netlist names it.

    Returns:
        What was removed and what the new wire ends in.

    Raises:
        EditError: ``E_REF_NOT_FOUND``, ``E_PIN``, ``E_SHARED_SHEET``,
            ``E_FORMAT_UNSUPPORTED``, ``E_RECONNECT_UNSUPPORTED`` for a pin
            that cannot be freed, ``E_NO_ROOM`` if the new wire would
            touch something, or a code from naming the net.
    """
    ref, _, number = pin.rpartition(".")
    target = project.sheet_of(ref) if ref else None
    if target is None:
        raise EditError("E_REF_NOT_FOUND", f"no symbol in the project is {ref or pin}")
    if len(target.instances) != 1:
        raise EditError(
            "E_SHARED_SHEET",
            f"{ref} is on a sheet used {len(target.instances)} times, where "
            "one change would change several parts",
        )
    root = target.document.root
    _need_format(target)
    defs = _defs(target.document)
    found: tuple[SList, Point, float] | None = None
    for symbol in _placed(target.document):
        if ref not in references(symbol, project.name):
            continue
        definition = _definition_of(symbol, defs)
        points = _pin_points(symbol, defs, target.path.name)
        if definition is not None and number in points:
            style = symbol.first("body_style") or symbol.first("convert")
            angle = _definition_pins(
                definition, _unit(symbol), int(style.values()[0]) if style else 1
            )[number][2]
            found = (symbol, points[number], angle)
    if found is None:
        raise EditError("E_PIN", f"{ref} has no pin numbered {number}")
    symbol, point, angle = found
    ending = _ending(project, target, net)

    drawing = _Drawing(target, defs, [], free={point})
    own = sum(
        1 for at in _pin_points(symbol, defs, target.path.name).values() if at == point
    )
    if own > 1:
        raise EditError(
            "E_RECONNECT_UNSUPPORTED",
            f"{own} pins of {ref} share the place of pin {number}, so they "
            "move together or not at all",
        )
    if drawing.held(point) or drawing.pins.get(point, 0) > own:
        raise EditError(
            "E_RECONNECT_UNSUPPORTED",
            f"{pin} touches another part's pin or lies on a wire that passes "
            "it, so it cannot be freed without redrawing",
        )
    if len(drawing.ends.get(point, [])) >= 2:
        raise EditError(
            "E_RECONNECT_UNSUPPORTED",
            f"{len(drawing.ends[point])} wires meet at {pin}, so freeing it "
            "would part what they join",
        )
    cut = _follow(drawing, [point], target.path.name, project.name)

    placement = _placement(symbol)
    step = pin_outward(placement, angle)
    end = (round(point[0] + step[0] * _STUB, 4), round(point[1] + step[1] * _STUB, 4))
    middle = (round(point[0] + step[0] * GRID, 4), round(point[1] + step[1] * GRID, 4))
    for spot in (middle, end):
        if drawing.touched(spot):
            raise EditError(
                "E_NO_ROOM",
                f"a new wire from {pin} would touch what is drawn at "
                f"({spot[0]}, {spot[1]}) on {target.path.name}",
            )
    base = _uuid(root)
    newline = _newline(target.document)
    tag = f"{pin}/moved/{net}"

    def uid(what: str) -> str:
        return str(uuid.uuid5(NAMESPACE, f"{base}/{tag}/{what}"))

    new: list[SList] = [wire(point, end, uid("wire"))]
    power_refs: list[str] = []
    if ending.kind == "power symbol":
        assert ending.definition is not None
        if ending.symbol not in defs:
            held = copy(ending.definition)
            assert isinstance(held, SList)
            _embed(root, held, newline)
            defs[ending.symbol] = held
        power_refs.append(_next_power(_power_numbers(project)))
        new.append(
            placed_symbol(
                defs[ending.symbol],
                ending.symbol,
                Placement(*end),
                reference=power_refs[0],
                value=None,
                footprint=None,
                unit=1,
                pins=list(_definition_pins(defs[ending.symbol], 1, 1)),
                project=project.name,
                root=target.instances[0][1:],
                uuid_of=lambda what: uid(f"power/{what}"),
                fields={},
            )
        )
    elif ending.kind == "global label":
        new.append(global_label(ending.text, end, step, uid("label")))
    else:
        new.append(label(ending.text, end, step, uid("label")))
    doomed_ids = {id(node) for node in cut.nodes}
    root.items[:] = [item for item in root.items if id(item) not in doomed_ids]
    for node in new:
        _insert(root, node, newline)
    target.changed = True
    return Moved(
        pin,
        target.path.relative_to(project.folder).as_posix(),
        net,
        ending.kind,
        cut.wires,
        tuple(cut.labels),
        tuple(cut.power),
        tuple(power_refs),
        tuple(cut.left),
        cut.shortened,
    )


@dataclass(slots=True)
class _Cut:
    """What following the wires from freed pins found to remove."""

    nodes: list[SList]
    wires: int
    labels: list[str]
    power: list[str]
    marks: int
    left: list[str]
    shortened: int = 0


def _follow(drawing: _Drawing, frontier: list[Point], where: str, project: str) -> _Cut:
    """Collect the wires, labels and power symbols that now lead nowhere."""
    cut = _Cut([], 0, [], [], 0, [])
    frontier = list(frontier)
    seen: set[Point] = set()
    while frontier:
        point = frontier.pop()
        if point in seen:
            continue
        seen.add(point)
        if drawing.held(point):
            continue
        ends = drawing.ends.get(point, [])
        if len(ends) >= 2:
            continue
        here_labels = drawing.labels.get(point, [])
        here_power = drawing.power.get(point, [])
        if len(ends) == 1:
            if here_labels or here_power:
                continue
            segment = ends[0]
            for mark in drawing.marks.pop(point, []):
                cut.nodes.append(mark)
                cut.marks += 1
            joined = drawing.along(segment)
            if joined:
                # Something joins this wire part-way: it stays from there on.
                nearest = min(
                    joined,
                    key=lambda at: abs(at[0] - point[0]) + abs(at[1] - point[1]),
                )
                drawing.shorten(segment, point, nearest)
                cut.shortened += 1
                continue
            drawing.drop_wire(segment)
            cut.nodes.append(segment.node)
            cut.wires += 1
            other = segment.b if segment.a == point else segment.a
            seen.discard(other)
            frontier.append(other)
            continue
        for node in here_labels:
            if node.head == "hierarchical_label":
                cut.left.append(
                    f"hierarchical label {node.values()[0]} now joins nothing "
                    f"on {where}"
                )
                continue
            cut.nodes.append(node)
            cut.labels.append(node.values()[0])
        drawing.labels.pop(point, None)
        for symbol in here_power:
            cut.nodes.append(symbol)
            cut.power.extend(references(symbol, project))
        drawing.power.pop(point, None)
        for mark in drawing.marks.pop(point, []):
            cut.nodes.append(mark)
            cut.marks += 1
    return cut


@dataclass(frozen=True, slots=True)
class _Ending:
    """What a new pin's wire ends in."""

    net: str
    kind: str
    text: str
    symbol: str = ""
    definition: SList | None = None


@dataclass(frozen=True, slots=True)
class _Segment:
    """A wire with its two ends."""

    node: SList
    a: Point
    b: Point


class _Drawing:
    """What is attached at each point of a sheet, for following wires."""

    def __init__(
        self,
        sheet: Sheet,
        defs: dict[str, SList],
        without: list[SList],
        free: set[Point] | None = None,
    ) -> None:
        skip = {id(node) for node in without}
        self._free = free or set()
        self.pins: dict[Point, int] = {}
        root = sheet.document.root
        self.ends: dict[Point, list[_Segment]] = {}
        self.labels: dict[Point, list[SList]] = {}
        self.power: dict[Point, list[SList]] = {}
        self.marks: dict[Point, list[SList]] = {}
        self._segments: list[_Segment] = []
        self._fixed: set[Point] = set()
        for node in root.lists():
            if id(node) in skip:
                continue
            if node.head == "wire":
                points = _points(node)
                if len(points) != 2:
                    self._fixed.update(points)
                    continue
                segment = _Segment(node, points[0], points[1])
                self._segments.append(segment)
                self.ends.setdefault(segment.a, []).append(segment)
                self.ends.setdefault(segment.b, []).append(segment)
            elif node.head in _LABELS:
                self.labels.setdefault(_at(node), []).append(node)
            elif node.head in ("junction", "no_connect"):
                self.marks.setdefault(_at(node), []).append(node)
            elif node.head == "symbol" and node.first("lib_id") is not None:
                points = _pin_points(node, defs, sheet.path.name).values()
                definition = _definition_of(node, defs)
                if definition is not None and definition.first("power") is not None:
                    for point in points:
                        self.power.setdefault(point, []).append(node)
                else:
                    self._fixed.update(points)
                    for point in points:
                        self.pins[point] = self.pins.get(point, 0) + 1
            elif node.head == "sheet":
                self._fixed.update(_at(pin) for pin in node.lists("pin"))
            elif node.head not in _NOT_DRAWN:
                # A bus, a bus entry or anything else: whatever it touches
                # is taken to be attached.
                self._fixed.update(_coordinates(node))

    def touched(self, point: Point) -> bool:
        """Whether anything at all is drawn at a point."""
        return bool(
            self.held(point)
            or self.ends.get(point)
            or self.labels.get(point)
            or self.power.get(point)
            or self.marks.get(point)
        )

    def held(self, point: Point) -> bool:
        """Whether something that stays is attached at a point."""
        if point in self._fixed and point not in self._free:
            return True
        ends = self.ends.get(point, [])
        return any(
            segment not in ends and _on_segment(point, segment)
            for segment in self._segments
        )

    def along(self, segment: _Segment) -> list[Point]:
        """The points strictly inside a wire where something else is attached."""
        candidates: set[Point] = set(self._fixed)
        candidates.update(at for at, held in self.ends.items() if held)
        candidates.update(at for at, held in self.labels.items() if held)
        candidates.update(at for at, held in self.power.items() if held)
        candidates.update(at for at, held in self.marks.items() if held)
        return [
            at
            for at in candidates
            if at not in (segment.a, segment.b) and _on_segment(at, segment)
        ]

    def shorten(self, segment: _Segment, end: Point, to: Point) -> None:
        """Move one end of a wire along itself, in the file's tree too."""
        points = segment.node.first("pts")
        assert points is not None
        for xy in points.lists("xy"):
            values = xy.values()
            if (round(float(values[0]), 4), round(float(values[1]), 4)) == end:
                for index, value in ((1, to[0]), (2, to[1])):
                    old = xy.items[index]
                    fresh = length_atom(value)
                    fresh.before = old.before
                    xy.items[index] = fresh
                break
        self.drop_wire(segment)
        other = segment.b if segment.a == end else segment.a
        moved = _Segment(segment.node, to, other)
        self._segments.append(moved)
        self.ends.setdefault(to, []).append(moved)
        self.ends.setdefault(other, []).append(moved)

    def drop_wire(self, segment: _Segment) -> None:
        """Forget a wire that is being removed."""
        self._segments.remove(segment)
        for end in (segment.a, segment.b):
            self.ends[end] = [s for s in self.ends[end] if s is not segment]


def _pin_numbers(symbol: Symbol, ref: str, name: str) -> list[str]:
    """The numbers of the pins a caller means, looked for in every unit.

    A name is looked up as `resolve_pin` looks it up, unit by unit. One
    that names pins in more than one unit (an op amp's ``+`` in units A
    and B) is refused unless written ``NAME*``, since which unit is meant
    cannot be told. Failing a name, a pin is found by its number, which is
    how KiCad's netlist names it.
    """
    found: dict[str, int] = {}
    for unit in range(1, symbol.unit_count + 1):
        try:
            matched = resolve_pin(symbol, ref, name, unit=unit)
        except PinError as exc:
            if exc.code == "E_PIN_AMBIGUOUS":
                raise EditError("E_PIN", str(exc)) from exc
            continue
        for pin in matched:
            found.setdefault(pin.number, pin.unit)
    if not found:
        if any(pin.number == name for pin in symbol.pins):
            return [name]
        shown = dict.fromkeys(f"{pin_label(pin)} ({pin.number})" for pin in symbol.pins)
        raise EditError(
            "E_PIN",
            f"{ref} ({symbol.name}) has no pin {name!r}. Its pins: "
            f"{', '.join(shown) or 'none'}.",
        )
    units = sorted({unit for unit in found.values() if unit})
    if len(units) > 1 and not name.endswith("*"):
        raise EditError(
            "E_PIN",
            f"{ref} ({symbol.name}) has a pin {name!r} in units "
            f"{', '.join(_unit_letter(unit) for unit in units)} (numbers "
            f"{', '.join(found)}): give the number, or write {name}* to mean "
            "all of them",
        )
    return list(found)


def _unit_letter(unit: int) -> str:
    """The letter KiCad shows for a unit: A for 1, B for 2."""
    return chr(ord("A") + unit - 1) if 1 <= unit <= 26 else str(unit)


def _need_format(sheet: Sheet) -> None:
    """Refuse a file that is not in the format the writer's output was checked in."""
    version = sheet.document.root.first("version")
    wanted = FORMAT_VERSION["10"]
    if version is None or version.values() != [str(wanted)]:
        raise EditError(
            "E_FORMAT_UNSUPPORTED",
            f"{sheet.path.name} is in schematic format "
            f"{version.values()[0] if version else 'unknown'}, and Kairo draws "
            f"only in KiCad 10's format ({wanted}): open the project in KiCad 10 "
            "and save it",
        )


def _on_segment(point: Point, segment: _Segment) -> bool:
    """Whether a point lies on a wire, its ends included."""
    (ax, ay), (bx, by), (px, py) = segment.a, segment.b, point
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > 1e-6:
        return False
    return (
        min(ax, bx) - 1e-6 <= px <= max(ax, bx) + 1e-6
        and min(ay, by) - 1e-6 <= py <= max(ay, by) + 1e-6
    )


def _target_sheet(project: Project, beside: str | None, sheet: str | None) -> Sheet:
    """The sheet a new part goes on."""
    if beside is not None:
        found = project.sheet_of(beside)
        if found is None:
            raise EditError("E_REF_NOT_FOUND", f"no symbol in the project is {beside}")
        return found
    if sheet is not None:
        for candidate in project.sheets.values():
            names = (
                candidate.path.name,
                candidate.path.relative_to(project.folder).as_posix(),
            )
            if sheet in names:
                return candidate
        raise EditError(
            "E_SHEET_UNKNOWN",
            f"the project has no sheet file {sheet}; it has "
            f"{', '.join(sorted(s.path.name for s in project.sheets.values()))}",
        )
    if len(project.sheets) == 1:
        return next(iter(project.sheets.values()))
    raise EditError(
        "E_SHEET_NEEDED",
        f"the project has {len(project.sheets)} sheets: name a part to draw "
        "the new one beside, or the sheet file",
    )


def _ending(project: Project, sheet: Sheet, net: str) -> _Ending:
    """Decide what joins a new pin to a net by its name."""
    if net.startswith(("Net-(", "unconnected-(")):
        raise EditError(
            "E_NET_UNNAMED",
            f"{net} has no name of its own (KiCad made that one up from a pin), "
            "so nothing drawn elsewhere can join it by name: put a label on it "
            "in KiCad first",
        )
    if not net.startswith("/"):
        for holder in [sheet, *sorted(project.sheets.values(), key=lambda s: s.path)]:
            defs = _defs(holder.document)
            for symbol in _placed(holder.document):
                definition = _definition_of(symbol, defs)
                if (
                    definition is not None
                    and definition.first("power") is not None
                    and _property(symbol, "Value") == net
                ):
                    name = definition.values()[0]
                    return _Ending(net, "power symbol", net, name, definition)
        return _Ending(net, "global label", net)
    prefix = sheet.names[0]
    local = net[len(prefix) :] if net.startswith(prefix) else ""
    if not local or "/" in local:
        raise EditError(
            "E_NET_NOT_ON_SHEET",
            f"{net} is local to another sheet, and {sheet.path.name}'s local "
            f"nets are named {prefix}<name>",
        )
    return _Ending(net, "label", local)


def _own_box(
    definition: SList,
    ref: str,
    value: str,
    endings: Mapping[str, _Ending],
    unit: int = 1,
    lettered: bool = False,
) -> Box:
    """The room one unit of a new part needs, labels and text included.

    ``lettered`` is for a part of several units, whose reference KiCad
    shows with the unit's letter after it.
    """
    origin = Placement(0.0, 0.0)
    body = placed_box(origin, symbol_box(definition, unit))
    boxes = [
        Box(body.left - GRID, body.top - GRID, body.right + GRID, body.bottom + GRID)
    ]
    drawn = _definition_pins(definition, unit, 1)
    for number, ending in endings.items():
        x, y, angle = drawn[number]
        start = to_sheet(origin, x, y)
        step = pin_outward(origin, angle)
        # The wire, then the label's text running on the same way, or a
        # power symbol with its name beside it.
        reach = _STUB + (len(ending.text) + 2) * GRID
        if ending.kind == "power symbol":
            reach = _STUB + 3 * GRID
        end = (start[0] + step[0] * reach, start[1] + step[1] * reach)
        across = len(ending.text) * GRID / 2 if ending.kind == "power symbol" else 0
        across += 1.5 * GRID
        boxes.append(
            Box(
                min(start[0], end[0]) - (across if step[0] == 0 else 0),
                min(start[1], end[1]) - (across if step[1] == 0 else 0),
                max(start[0], end[0]) + (across if step[0] == 0 else 0),
                max(start[1], end[1]) + (across if step[1] == 0 else 0),
            )
        )
    shown = {"Reference": f"{ref}A" if lettered else ref, "Value": value}
    for prop in definition.lists("property"):
        values = prop.values()
        hide = prop.first("hide")
        at = prop.first("at")
        if (
            at is None
            or len(values) < 2
            or values[0] not in shown
            or (hide is not None and hide.values() == ["yes"])
        ):
            continue
        x, y = to_sheet(
            Placement(0.0, 0.0), float(at.values()[0]), float(at.values()[1])
        )
        boxes.append(_text_box(x, y, shown[values[0]], 0.0))
    return _union(boxes)


def _centre(sheet: Sheet, beside: str | None, project: str) -> Point:
    """Where the search for free space starts."""
    if beside is not None:
        for symbol in _placed(sheet.document):
            if beside in references(symbol, project):
                placement = _placement(symbol)
                return (placement.x, placement.y)
    area, _ = _usable(sheet.document.root)
    return ((area.left + area.right) / 2, (area.top + area.bottom) / 2)


def _usable(root: SList) -> tuple[Box, Box]:
    """The part of the paper a part may go on, and the title block's corner."""
    paper = root.first("paper")
    values = paper.values() if paper is not None else []
    sizes = {name: (w, h) for name, w, h in PAPERS} | _MORE_PAPERS
    if values and values[0] == "User" and len(values) >= 3:
        width, height = float(values[1]), float(values[2])
    elif values and values[0] in sizes:
        width, height = sizes[values[0]]
        if "portrait" in values[1:]:
            width, height = height, width
    else:
        raise EditError(
            "E_PAPER_UNKNOWN",
            f"the sheet's paper is {' '.join(values) or 'not given'}, whose size "
            "is not known here",
        )
    area = Box(_FRAME, _FRAME, width - _FRAME, height - _FRAME)
    corner = Box(
        area.right - _TITLE_BLOCK[0], area.bottom - _TITLE_BLOCK[1], width, height
    )
    return area, corner


def _free_spot(
    own: Box, centre: Point, area: Box, obstacles: list[Box]
) -> Point | None:
    """The clear position nearest a point, on a coarse grid, or ``None``."""
    cx = round(round(centre[0] / (2 * GRID)) * 2 * GRID, 4)
    cy = round(round(centre[1] / (2 * GRID)) * 2 * GRID, 4)
    reach = int(max(area.width, area.height) / _STEP) + 1
    offsets = sorted(
        (
            (dx, dy)
            for dx in range(-reach, reach + 1)
            for dy in range(-reach, reach + 1)
        ),
        key=lambda d: (d[0] * d[0] + d[1] * d[1], d[1], d[0]),
    )
    for dx, dy in offsets:
        x = round(cx + dx * _STEP, 4)
        y = round(cy + dy * _STEP, 4)
        box = Box(x + own.left, y + own.top, x + own.right, y + own.bottom)
        if (
            box.left < area.left
            or box.top < area.top
            or box.right > area.right
            or box.bottom > area.bottom
        ):
            continue
        if not any(box.overlaps(other, GRID) for other in obstacles):
            return (x, y)
    return None


def _obstacles(document: Document, defs: dict[str, SList]) -> list[Box]:
    """A box around everything drawn on a sheet, generously."""
    boxes: list[Box] = []
    for node in document.root.lists():
        if node.head in _NOT_DRAWN:
            continue
        points = _coordinates(node)
        if node.head == "symbol" and node.first("lib_id") is not None:
            definition = _definition_of(node, defs)
            placement = _placement(node)
            if definition is not None:
                with contextlib.suppress(ValueError):
                    boxes.append(
                        placed_box(placement, symbol_box(definition, _unit(node)))
                    )
            for prop in node.lists("property"):
                hide = prop.first("hide")
                values = prop.values()
                if len(values) >= 2 and not (
                    hide is not None and hide.values() == ["yes"]
                ):
                    x, y, angle = _at_angle(prop)
                    boxes.append(_text_box(x, y, values[1], angle))
        elif node.head in (*_LABELS, "text"):
            x, y, angle = _at_angle(node)
            boxes.append(_text_box(x, y, (node.values() or [""])[0], angle))
        elif node.head in ("sheet", "text_box"):
            size = node.first("size")
            if size is not None and points:
                x, y = points[0]
                w, h = (float(v) for v in size.values()[:2])
                boxes.append(
                    Box(min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h))
                )
        if points:
            xs = [x for x, _ in points]
            ys = [y for _, y in points]
            boxes.append(Box(min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _text_box(x: float, y: float, text: str, angle: float) -> Box:
    """A box sure to hold a line of text anchored at a point, either way round."""
    along = max(len(text), 1) * GRID
    across = 1.5 * GRID
    if int(angle) % 180 == 90:
        return Box(x - across, y - along, x + across, y + along)
    return Box(x - along, y - across, x + along, y + across)


def _union(boxes: list[Box]) -> Box:
    return Box(
        min(b.left for b in boxes),
        min(b.top for b in boxes),
        max(b.right for b in boxes),
        max(b.bottom for b in boxes),
    )


def _placed(document: Document) -> list[SList]:
    """The placed symbols of a sheet."""
    return [s for s in document.root.lists("symbol") if s.first("lib_id") is not None]


def _defs(document: Document) -> dict[str, SList]:
    """The symbols a sheet file holds its own copies of, by name."""
    held = document.root.first("lib_symbols")
    found: dict[str, SList] = {}
    for node in held.lists("symbol") if held is not None else []:
        values = node.values()
        if values:
            found[values[0]] = node
    return found


def _definition_of(symbol: SList, defs: dict[str, SList]) -> SList | None:
    """The embedded symbol a placed symbol is drawn from."""
    for head in ("lib_name", "lib_id"):
        node = symbol.first(head)
        if node is not None and node.values() and node.values()[0] in defs:
            return defs[node.values()[0]]
    return None


def _definition_pins(
    definition: SList, unit: int, style: int
) -> dict[str, tuple[float, float, float]]:
    """Each pin of one unit: its number, and where and which way it is drawn."""
    pins: dict[str, tuple[float, float, float]] = {}
    for child in definition.lists("symbol"):
        found = _UNIT_NAME.search((child.values() or [""])[0])
        if found is None:
            continue
        if int(found.group(1)) not in (0, unit) or int(found.group(2)) not in (
            0,
            style,
        ):
            continue
        for pin in child.lists("pin"):
            number = pin.first("number")
            at = pin.first("at")
            if number is None or at is None or len(at.values()) < 3:
                continue
            x, y, angle = (float(v) for v in at.values()[:3])
            pins[number.values()[0]] = (x, y, angle)
    return pins


def _pin_points(symbol: SList, defs: dict[str, SList], where: str) -> dict[str, Point]:
    """Where each pin of a placed symbol is on the sheet."""
    definition = _definition_of(symbol, defs)
    if definition is None:
        name = symbol.first("lib_id")
        raise EditError(
            "E_SYMBOL_MISSING",
            f"{where} draws {', '.join(references(symbol)) or 'a part'} from "
            f"{name.values()[0] if name else 'a symbol'}, which the file does not "
            "hold, so where its pins are cannot be known",
        )
    style = symbol.first("body_style") or symbol.first("convert")
    placement = _placement(symbol)
    return {
        number: to_sheet(placement, x, y)
        for number, (x, y, _) in _definition_pins(
            definition, _unit(symbol), int(style.values()[0]) if style else 1
        ).items()
    }


def _placement(symbol: SList) -> Placement:
    """How a placed symbol lies on its sheet."""
    x, y, angle = _at_angle(symbol)
    mirror = symbol.first("mirror")
    turn: Rotation = 0
    for candidate in (90, 180, 270):
        if int(angle) % 360 == candidate:
            turn = candidate
    flip: Mirror = ""
    if mirror is not None and mirror.values():
        flip = "x" if mirror.values()[0] == "x" else "y"
    return Placement(x, y, turn, flip)


def _unit(symbol: SList) -> int:
    node = symbol.first("unit")
    return int(node.values()[0]) if node is not None and node.values() else 1


def _property(symbol: SList, name: str) -> str | None:
    for prop in symbol.lists("property"):
        values = prop.values()
        if len(values) >= 2 and values[0] == name:
            return values[1]
    return None


def _uuid(node: SList) -> str:
    found = node.first("uuid")
    if found is None or not found.values():
        raise EditError("E_NOT_A_SCHEMATIC", "an item has no identifier")
    return found.values()[0]


def _at(node: SList) -> Point:
    x, y, _ = _at_angle(node)
    return (round(x, 4), round(y, 4))


def _at_angle(node: SList) -> tuple[float, float, float]:
    at = node.first("at")
    values = at.values() if at is not None else []
    if len(values) < 2:
        return (0.0, 0.0, 0.0)
    return (
        float(values[0]),
        float(values[1]),
        float(values[2]) if len(values) > 2 else 0.0,
    )


def _points(node: SList) -> list[Point]:
    """The points of a wire."""
    points = node.first("pts")
    return [
        (round(float(xy.values()[0]), 4), round(float(xy.values()[1]), 4))
        for xy in (points.lists("xy") if points is not None else [])
    ]


def _coordinates(node: SList) -> list[Point]:
    """Every coordinate pair anywhere inside an item."""
    found: list[Point] = []
    for child in node.lists():
        if child.head in _COORDINATES:
            values = child.values()
            with contextlib.suppress(IndexError, ValueError):
                found.append((round(float(values[0]), 4), round(float(values[1]), 4)))
        else:
            found.extend(_coordinates(child))
    return found


def _newline(document: Document) -> str:
    return "\r\n" if "\r\n" in document.to_text() else "\n"


def _power_numbers(project: Project) -> list[int]:
    """The numbers power symbols carry already, with the width they are written at."""
    numbers = [0]
    width = 2
    for ref in project.all_references():
        found = re.fullmatch(r"#PWR(\d+)", ref)
        if found:
            numbers.append(int(found.group(1)))
            width = max(width, len(found.group(1)))
    return [width, *numbers]


def _next_power(numbers: list[int]) -> str:
    """A power reference no symbol has, remembered for the next call."""
    width, taken = numbers[0], numbers[1:]
    number = max(taken) + 1
    numbers.append(number)
    return f"#PWR{number:0{width}d}"


def _embed(root: SList, definition: SList, newline: str) -> None:
    """Put a symbol among the file's own, in name order as KiCad keeps them."""
    held = root.first("lib_symbols")
    if held is None:
        raise EditError("E_NOT_A_SCHEMATIC", "the sheet has no lib_symbols list")
    lay_out(definition, depth=2, newline=newline)
    definition.before = newline + "\t\t"
    name = definition.values()[0]
    position = len(held.items)
    for index, item in enumerate(held.items):
        if (
            isinstance(item, SList)
            and item.head == "symbol"
            and item.values()[0] > name
        ):
            position = index
            break
    if not held.lists():
        held.end = newline + "\t"
    held.items.insert(position, definition)


def _insert(root: SList, node: SList, newline: str) -> None:
    """Put an item on the sheet after the last of its kind."""
    lay_out(node, depth=1, newline=newline)
    node.before = newline + "\t"
    assert node.head in _KINDS
    rank = _KINDS.index(node.head)
    position = None
    for index, item in enumerate(root.items):
        if isinstance(item, Atom):
            continue
        if item.head in _KINDS and _KINDS.index(item.head) > rank:
            position = index
            break
    if position is None:
        position = len(root.items)
    root.items.insert(position, node)
