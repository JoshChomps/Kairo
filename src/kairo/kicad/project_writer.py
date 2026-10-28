"""Write a design drawn on several sheets as one hierarchical KiCad project.

A design with ``sheets`` is drawn as a root sheet and one child sheet per
entry, the way a drawing office lays out a board too large for one page:

- each child is drawn by `write_schematic`, wired for a person by default,
  from the parts of the blocks the design puts on it;
- a signal net with pins on two or more sheets is a port: on each of its
  sheets it ends in a hierarchical label, and on the root it is a pin of
  that sheet's symbol, joined to the same pin of the other sheets by a
  local label of the net's name on a short wire (KiCad names the net after
  the root's label, ``/<net>``);
- supplies and grounds keep their power symbols, or global labels where
  the library has no symbol of that name, so they need no ports;
- what each pin's stub ends in is decided once on the whole design
  (`endings_of`), so a net that needs a power flag gets one in the whole
  project, not one per sheet;
- power and flag references are numbered from ``page x 1000 + 1`` on each
  sheet, as KiCad's own annotation offers, and each symbol's instance path
  is ``/<root>/<sheet symbol>``.

The root sheet is a block diagram: one sheet symbol per child in page
order, three to a row, each with its title written inside and its ports
on the side facing the sheets they lead to. Every form written here is the
one KiCad 10.0.7 saves (its re-save of the cm5_minima and
complex_hierarchy demos); KiCad's netlist and rule check of the whole
project are the judges, as for one sheet.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from kairo import __version__
from kairo.ir import Design, split_pin
from kairo.kicad.emit import lay_out, lst, sym
from kairo.kicad.geometry import GRID
from kairo.kicad.schematic_writer import (
    FORMAT_VERSION,
    NAMESPACE,
    PAPERS,
    SheetPlace,
    Style,
    WriterError,
    WrittenSchematic,
    endings_of,
    label,
    text,
    title_block,
    wire,
    write_schematic,
)
from kairo.kicad.sexpr import SList, to_text
from kairo.library.grounding import Grounded
from kairo.library.index import LibraryIndex
from kairo.spice.registry import ModelRegistry
from kairo.verify.findings import Finding

# Root sheet layout, in mm, each a whole number of 1.27 mm grid steps.
_BOX_WIDTH = 40 * GRID
# Sheet pins 5.08 mm apart, so each pin's name and label read clear of
# the next on a printed A3 sheet.
_PIN_PITCH = 4 * GRID
_BOX_HEADER = 6 * GRID
_STUB = 4 * GRID
_ROW_GAP = 8 * GRID
_MARGIN = 20 * GRID
_PER_ROW = 3
# KiCad's default text is about 0.85 of its height wide, per character.
_CHARACTER = 1.27 * 0.85


@dataclass(frozen=True, slots=True)
class WrittenProject:
    """A hierarchical project ready to save.

    Attributes:
        files: Each file's text by name, the root sheet first.
        root: The root sheet's file name.
        net_names: The name KiCad's netlist gives each of the design's nets.
        sheets: Each child sheet as drawn, by sheet name.
        findings: Warnings from every sheet's drawing, each naming its sheet.
    """

    files: dict[str, str]
    root: str
    net_names: dict[str, str]
    sheets: dict[str, WrittenSchematic]
    findings: tuple[Finding, ...]


def ports_of(grounded: Grounded) -> dict[str, dict[str, str]]:
    """For each sheet, the signal nets that leave it and their labels' shapes.

    The shape says which way the net runs as seen from the sheet: ``output``
    when a pin on the sheet drives it, ``input`` when its pins there only
    listen, ``bidirectional`` for a bus pin, and ``passive`` otherwise.
    """
    design = grounded.design
    sheet_of = design.sheet_of()
    ports: dict[str, dict[str, str]] = {name: {} for name in design.sheets}
    for net in design.connections:
        if design.nets[net].role != "signal":
            continue
        pins = grounded.net_pins(net)
        sheets = {sheet_of[p.ref] for p in pins}
        if len(sheets) < 2:
            continue
        for name in sheets:
            kinds = {p.pin.electrical_type for p in pins if sheet_of[p.ref] == name}
            if kinds & {"output", "power_out", "open_collector", "open_emitter"}:
                shape = "output"
            elif "bidirectional" in kinds or "tri_state" in kinds:
                shape = "bidirectional"
            elif kinds and kinds <= {"input"}:
                shape = "input"
            else:
                shape = "passive"
            ports[name][net] = shape
    return ports


def write_project(
    grounded: Grounded,
    index: LibraryIndex,
    *,
    newline: str | None = None,
    models: ModelRegistry | None = None,
    style: Style = "wired",
) -> WrittenProject:
    """Draw a design with sheets as a root sheet and one child per sheet.

    Args:
        grounded: The whole design, grounded.
        index: The library index it was grounded against.
        newline: The line ending; the platform's own by default.
        models: The models folder, as for `write_schematic`.
        style: The children's style; ``wired`` draws them for a person.

    Returns:
        Every file's text and the net names KiCad will give.

    Raises:
        WriterError: If the design has no sheets, or a sheet cannot be drawn.
    """
    design = grounded.design
    if not design.sheets:
        raise WriterError("write_project draws a design with sheets; this one has none")
    newline = newline or ("\r\n" if os.name == "nt" else "\n")
    sheet_of = design.sheet_of()
    endings = endings_of(grounded, index)
    ports = ports_of(grounded)
    root_uuid = str(uuid.uuid5(NAMESPACE, design.design))
    files: dict[str, str] = {}
    sheets: dict[str, WrittenSchematic] = {}
    findings: list[Finding] = []
    for page, name in enumerate(design.sheets, start=2):
        child = _child(grounded, name, sheet_of)
        place = SheetPlace(
            design.design,
            f"{root_uuid}/{_box_uuid(design, name)}",
            page,
            ports[name],
            design.sheets[name].title,
        )
        try:
            written = write_schematic(
                child,
                index,
                newline=newline,
                models=models,
                style=style,
                sheet=place,
                endings=endings,
            )
        except WriterError as exc:
            raise WriterError(f"sheet {name}: {exc}") from exc
        sheets[name] = written
        files[f"{name}.kicad_sch"] = written.text
        findings += [
            Finding(f.layer, f.code, f.severity, f"sheet {name}: {f.message}", f.where)
            for f in written.findings
        ]
    root_name = f"{design.design}.kicad_sch"
    root = _root(design, root_uuid, ports, newline)
    net_names: dict[str, str] = {}
    for net in design.connections:
        if design.nets[net].role != "signal":
            net_names[net] = net
        elif any(net in p for p in ports.values()):
            net_names[net] = f"/{net}"
        else:
            (only,) = {sheet_of[p.ref] for p in grounded.net_pins(net)}
            net_names[net] = f"/{only}/{net}"
    return WrittenProject(
        {root_name: root, **files}, root_name, net_names, sheets, tuple(findings)
    )


def symbol_paths(design: Design) -> dict[str, tuple[str, str, str]]:
    """Each part's place in the schematic, as a board's footprint names it.

    Returns:
        By reference: the path (the sheet symbol's uuid, then the symbol's,
        joined by ``/``), the sheet's path such as ``/power/``, and the
        sheet's file. For a design without sheets, the symbol's uuid alone
        on the root sheet.
    """
    if not design.sheets:
        root = f"{design.design}.kicad_sch"
        return {
            ref: (_symbol_uuid(design.design, ref), "/", root)
            for ref in design.components
        }
    sheet_of = design.sheet_of()
    return {
        ref: (
            f"{_box_uuid(design, sheet_of[ref])}/"
            f"{_symbol_uuid(f'{design.design}-{sheet_of[ref]}', ref)}",
            f"/{sheet_of[ref]}/",
            f"{sheet_of[ref]}.kicad_sch",
        )
        for ref in design.components
    }


def _symbol_uuid(drawing: str, ref: str) -> str:
    # The writer's own recipe for a placed symbol (`_Ids.of(ref, "symbol")`).
    return str(uuid.uuid5(NAMESPACE, f"{drawing}/{ref}/symbol"))


def _box_uuid(design: Design, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{design.design}/sheet:{name}/box"))


def _child(grounded: Grounded, name: str, sheet_of: Mapping[str, str]) -> Grounded:
    """The part of the design on one sheet, as a design of its own."""
    design = grounded.design
    refs = [ref for ref in design.components if sheet_of[ref] == name]
    on = set(refs)
    connections = {
        net: [pin for pin in pins if split_pin(pin, design.components)[0] in on]
        for net, pins in design.connections.items()
    }
    connections = {net: pins for net, pins in connections.items() if pins}
    blocks = {block: design.blocks[block] for block in design.sheets[name].blocks}
    child = Design(
        design=f"{design.design}-{name}",
        kicad_version=design.kicad_version,
        nets={net: design.nets[net] for net in connections},
        components={ref: design.components[ref] for ref in refs},
        connections=connections,
        no_connect=[
            pin
            for pin in design.no_connect
            if split_pin(pin, design.components)[0] in on
        ],
        blocks=blocks,
        policy=design.policy,
        title_block=design.title_block,
    )
    return Grounded(
        child,
        {ref: grounded.symbols[ref] for ref in refs},
        tuple(pin for pin in grounded.pins if pin.ref in on),
    )


@dataclass(frozen=True, slots=True)
class _Box:
    name: str
    x: float
    y: float
    height: float
    left: tuple[str, ...]
    right: tuple[str, ...]


def _root(
    design: Design,
    root_uuid: str,
    ports: Mapping[str, Mapping[str, str]],
    newline: str,
) -> str:
    """The root sheet: a block diagram of sheet symbols joined at their pins.

    A net that runs only between two neighbouring sheets of a row is drawn
    as one straight wire between their facing pins, which take the same
    slot on both sides; any other port ends in a short wire and a label.
    """
    names = list(design.sheets)
    column = {name: i % _PER_ROW for i, name in enumerate(names)}

    def neighbour(name: str, step: int) -> str | None:
        i = names.index(name) + step
        if 0 <= i < len(names) and i // _PER_ROW == names.index(name) // _PER_ROW:
            return names[i]
        return None

    sides: dict[str, tuple[list[str], list[str]]] = {}
    direct: dict[str, int] = {}
    for name in names:
        before, after = neighbour(name, -1), neighbour(name, 1)
        left_direct: list[str] = []
        right_direct: list[str] = []
        left: list[str] = []
        right: list[str] = []
        for net in sorted(ports[name], key=_natural):
            others = [o for o in names if o != name and net in ports[o]]
            if others == [after]:
                right_direct.append(net)
            elif others == [before]:
                left_direct.append(net)
            # A port leads left when most of its other sheets stand to the
            # left; on the same column, right, so the row reads onward.
            elif sum(column[o] for o in others) / len(others) < column[name]:
                left.append(net)
            else:
                right.append(net)
        sides[name] = (left_direct + left, right_direct + right)
        direct[name] = len(right_direct)
    longest = (
        max((len(net) for p in ports.values() for net in p), default=0) * _CHARACTER
    )
    reach = _snap(_STUB + longest + 2 * GRID)
    pitch_x = _snap(_BOX_WIDTH + 2 * reach + 4 * GRID)
    boxes: list[_Box] = []
    y = _MARGIN + 4 * GRID
    for row in range(0, len(names), _PER_ROW):
        members = names[row : row + _PER_ROW]
        heights = {
            name: _snap(
                _BOX_HEADER
                + max(len(sides[name][0]), len(sides[name][1]), 1) * _PIN_PITCH
                + 2 * GRID
            )
            for name in members
        }
        for i, name in enumerate(members):
            left, right = sides[name]
            boxes.append(
                _Box(
                    name,
                    _snap(_MARGIN + reach + i * pitch_x),
                    y,
                    heights[name],
                    tuple(left),
                    tuple(right),
                )
            )
        y = _snap(y + max(heights.values()) + _ROW_GAP + 4 * GRID)
    width = _MARGIN * 2 + reach * 2 + pitch_x * min(len(names), _PER_ROW)
    paper = next(
        (p for p, w, h in PAPERS if width <= w and y + _MARGIN + 40 <= h), PAPERS[-1][0]
    )

    def uid(*what: str) -> str:
        return str(uuid.uuid5(NAMESPACE, "/".join((design.design, "root", *what))))

    texts: list[SList] = [
        text(
            design.sheets[b.name].title,
            (b.x + GRID, b.y + 2 * GRID + 1.27),
            1.27,
            uid(b.name, "title"),
        )
        for b in boxes
    ]
    wires: list[SList] = []
    labels: list[SList] = []
    sheets: list[SList] = []
    at = {box.name: box for box in boxes}
    for page, box in enumerate(boxes, start=2):
        pins: list[SList] = []
        for side, nets in (("left", box.left), ("right", box.right)):
            for k, net in enumerate(nets):
                py = _snap(box.y + _BOX_HEADER + k * _PIN_PITCH)
                px = box.x if side == "left" else box.x + _BOX_WIDTH
                out = -1 if side == "left" else 1
                pins.append(
                    lst(
                        "pin",
                        net,
                        sym(ports[box.name][net]),
                        lst("at", px, py, 180 if side == "left" else 0),
                        lst("uuid", uid(box.name, "pin", net)),
                        lst(
                            "effects",
                            lst("font", lst("size", 1.27, 1.27)),
                            lst("justify", sym(side)),
                        ),
                    )
                )
                after = neighbour(box.name, 1)
                if side == "right" and k < direct[box.name] and after is not None:
                    # Straight across to the neighbour's pin in the same slot,
                    # named once in the middle of the wire.
                    far = at[after].x
                    wires.append(wire((px, py), (far, py), uid(box.name, "wire", net)))
                    middle = (_snap((px + far) / 2), py)
                    labels.append(
                        label(net, middle, (1, 0), uid(box.name, "label", net))
                    )
                    continue
                if side == "left" and k < direct.get(neighbour(box.name, -1) or "", 0):
                    continue
                end = (round(px + out * _STUB, 4), py)
                wires.append(wire((px, py), end, uid(box.name, "wire", net)))
                labels.append(label(net, end, (out, 0), uid(box.name, "label", net)))
        sheets.append(_sheet_symbol(design, box, page, root_uuid, pins))
    root = lst(
        "kicad_sch",
        lst("version", FORMAT_VERSION[design.kicad_version]),
        lst("generator", "kairo"),
        lst("generator_version", __version__),
        lst("uuid", root_uuid),
        lst("paper", paper),
        title_block(design, design.design),
        lst("lib_symbols"),
        *sorted(texts, key=_uuid),
        *sorted(wires, key=_uuid),
        *sorted(labels, key=_uuid),
        *sorted(sheets, key=_uuid),
        lst("sheet_instances", lst("path", "/", lst("page", "1"))),
        lst("embedded_fonts", sym("no")),
    )
    lay_out(root, newline=newline)
    return to_text(root) + newline


def _sheet_symbol(
    design: Design, box: _Box, page: int, root_uuid: str, pins: list[SList]
) -> SList:
    """A sheet symbol in the form KiCad 10.0.7 saves one (cm5_minima's root)."""
    return lst(
        "sheet",
        lst("at", box.x, box.y),
        lst("size", _BOX_WIDTH, box.height),
        lst("exclude_from_sim", sym("no")),
        lst("in_bom", sym("yes")),
        lst("on_board", sym("yes")),
        lst("dnp", sym("no")),
        lst("fields_autoplaced", sym("yes")),
        lst("stroke", lst("width", 0.1524), lst("type", sym("solid"))),
        lst("fill", lst("color", 0, 0, 0, 0)),
        lst("uuid", _box_uuid(design, box.name)),
        lst(
            "property",
            "Sheetname",
            box.name,
            lst("at", box.x, round(box.y - 0.7116, 4), 0),
            lst("show_name", sym("no")),
            lst("do_not_autoplace", sym("no")),
            lst(
                "effects",
                lst(
                    "font",
                    lst("size", 1.27, 1.27),
                    lst("thickness", 0.254),
                    lst("bold", sym("yes")),
                ),
                lst("justify", sym("left"), sym("bottom")),
            ),
        ),
        lst(
            "property",
            "Sheetfile",
            f"{box.name}.kicad_sch",
            lst("at", box.x, round(box.y + box.height + 0.5846, 4), 0),
            lst("hide", sym("yes")),
            lst("show_name", sym("no")),
            lst("do_not_autoplace", sym("no")),
            lst(
                "effects",
                lst("font", lst("size", 1.27, 1.27)),
                lst("justify", sym("left"), sym("top")),
            ),
        ),
        *pins,
        lst(
            "instances",
            lst(
                "project",
                design.design,
                lst("path", f"/{root_uuid}", lst("page", str(page))),
            ),
        ),
    )


def _natural(name: str) -> tuple[tuple[int, int | str], ...]:
    """A sort key that puts VC2 before VC10, as a person reads them."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", name)
        if part
    )


def _uuid(item: SList) -> str:
    node = item.first("uuid")
    assert node is not None
    return str(node.values()[0])


def _snap(value: float) -> float:
    # Rounded before the ceiling, so a length already on the grid (k x
    # 5.08 mm, say) is not pushed a step on by a float's last digit.
    steps = math.ceil(round(value / GRID, 6))
    return round(steps * GRID, 4)
