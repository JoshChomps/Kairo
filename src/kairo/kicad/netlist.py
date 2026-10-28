"""Read the netlist ``kicad-cli sch export netlist --format kicadsexpr`` writes.

KiCad's own netlist is how Kairo learns what a schematic connects: which
parts it has and which pins sit on which net. Kairo never works
connectivity out from wires itself, so KiCad stays the referee.

The layout was taken from netlists kicad-cli 10.0.6 wrote for the boards in
``tests/fixtures/real`` (2026-10-08):

- The file is ``(export (version "E") (design ...) (components ...) ...
  (nets ...))``.
- ``(design ...)`` holds one ``(sheet (number ..) (name "/SoM/") ...)`` per
  sheet.
- A component is ``(comp (ref ..) (value ..) (footprint ..) (libsource (lib
  ..) (part ..) (description ..)) (sheetpath (names ..) ...) ...)``. A field
  with nothing in it is written with no value, as in ``(footprint)``, or
  left out.
- A component's own fields are ``(fields (field (name "LCSC")
  "C191023") ...)``, and a field with nothing in it is ``(field (name
  "LCSC"))`` (read from four of those boards' netlists, 2026-10-11).
- A net is ``(net (code ..) (name ..) (class ..) (node (ref ..) (pin ..)
  [(pinfunction ..)] (pintype ..)) ...)``.

A netlist laid out any other way is refused with `NetlistFormatError`.
"""

from __future__ import annotations

from dataclasses import dataclass

from kairo.kicad.sexpr import Atom, SexprError, SList, parse

#: The only netlist version Kairo reads.
NETLIST_VERSION = "E"


class NetlistFormatError(ValueError):
    """A netlist is not laid out the way kicad-cli 10 writes it."""


@dataclass(frozen=True, slots=True)
class Component:
    """One part in the schematic.

    Attributes:
        ref: Its reference, for example ``U1``.
        value: Its value field.
        footprint: Its footprint as ``<library>:<name>``, or empty.
        lib_id: The symbol it was drawn from, as ``<library>:<name>``.
        description: The symbol's description.
        sheet: The sheet it is on, for example ``/`` or ``/Supply/``.
        fields: Its fields that hold something, as name and text, in
            KiCad's order.
        path: Its symbol's path as a board's footprint names it: the sheet
            symbols' uuids then the symbol's own, joined by ``/``. Empty
            for a part drawn as several units, whose footprint path no
            KiCad-saved fixture has yet shown (see ``units``).
        properties: Its properties, as name and value; a flag such as
            ``exclude_from_bom`` has an empty value.
        units: The uuid of each unit's symbol, as KiCad 10.0.7 lists them
            in ``tstamps``: one for most parts, one per unit for a part
            drawn as several (a dual transistor, a quad op-amp).
    """

    ref: str
    value: str
    footprint: str
    lib_id: str
    description: str
    sheet: str
    fields: tuple[tuple[str, str], ...] = ()
    path: str = ""
    properties: tuple[tuple[str, str], ...] = ()
    units: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Node:
    """One pin on a net.

    Attributes:
        ref: The part's reference.
        pin: The pin number.
        pin_function: The pin's name as KiCad reports it, or empty.
        pin_type: The pin's electrical type, for example ``power_in``.
    """

    ref: str
    pin: str
    pin_function: str
    pin_type: str


@dataclass(frozen=True, slots=True)
class Net:
    """One net and every pin on it.

    Attributes:
        name: The net's name, for example ``/ANT`` or ``GND``.
        nodes: The pins on it, in KiCad's order.
    """

    name: str
    nodes: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Netlist:
    """A schematic's parts and connections, as KiCad exported them.

    Attributes:
        tool: The program that wrote it, for example ``Eeschema 10.0.6``.
        sheets: The name of every sheet, the root first.
        components: Every part, in KiCad's order.
        nets: Every net, in KiCad's order.
    """

    tool: str
    sheets: tuple[str, ...]
    components: tuple[Component, ...]
    nets: tuple[Net, ...]


def parse_netlist(text: str) -> Netlist:
    """Read a netlist in KiCad's S-expression format.

    Args:
        text: The netlist, as kicad-cli wrote it.

    Returns:
        The parts and nets.

    Raises:
        NetlistFormatError: If the text is not a version ``E`` netlist laid
            out as this module describes.
    """
    try:
        root = parse(text).root
    except SexprError as exc:
        raise NetlistFormatError(f"not a netlist: {exc}") from exc
    if root.head != "export":
        raise NetlistFormatError(f"not a netlist: top-level list is {root.head!r}")
    version = _text(root, "version", "the netlist")
    if version != NETLIST_VERSION:
        raise NetlistFormatError(
            f"netlist version {version!r} is not one Kairo reads "
            f"(it reads {NETLIST_VERSION!r})"
        )
    design = _child(root, "design", "the netlist")
    return Netlist(
        tool=_text(design, "tool", "the design"),
        sheets=tuple(_text(s, "name", "a sheet") for s in design.lists("sheet")),
        components=tuple(
            _component(node)
            for node in _child(root, "components", "the netlist").lists("comp")
        ),
        nets=tuple(
            _net(node) for node in _child(root, "nets", "the netlist").lists("net")
        ),
    )


def _component(node: SList) -> Component:
    ref = _text(node, "ref", "a component")
    where = f"component {ref}"
    source = _child(node, "libsource", where)
    units = _stamps(node, where)
    return Component(
        ref=ref,
        value=_optional(node, "value", where),
        footprint=_optional(node, "footprint", where),
        lib_id=f"{_text(source, 'lib', where)}:{_text(source, 'part', where)}",
        description=_optional(source, "description", where),
        sheet=_text(_child(node, "sheetpath", where), "names", where),
        fields=_fields(node, where),
        path="/".join(
            part
            for part in (
                # Older netlists, and hand-written ones, carry no tstamps.
                *_optional(_child(node, "sheetpath", where), "tstamps", where).split(
                    "/"
                ),
                *units,
            )
            if part
        )
        if len(units) <= 1
        else "",
        properties=tuple(
            (_text(prop, "name", where), _optional(prop, "value", where))
            for prop in node.lists("property")
        ),
        units=units,
    )


def _stamps(node: SList, where: str) -> tuple[str, ...]:
    """A component's own ``tstamps``: each of its units' symbol uuids."""
    child = node.first("tstamps")
    if child is None:
        return ()
    stamps: list[str] = []
    for item in child.items[1:]:
        if not isinstance(item, Atom):
            raise NetlistFormatError(f"{where}: tstamps holds a list")
        try:
            stamps.append(item.value)
        except SexprError as exc:
            raise NetlistFormatError(f"{where}: {exc}") from exc
    return tuple(s for s in stamps if s)


def _fields(node: SList, where: str) -> tuple[tuple[str, str], ...]:
    """A component's fields that hold something."""
    holder = node.first("fields")
    if holder is None:
        return ()
    found: list[tuple[str, str]] = []
    for field in holder.lists("field"):
        held = [item for item in field.items[1:] if isinstance(item, Atom)]
        if len(held) > 1:
            raise NetlistFormatError(f"{where}: a field holds more than one value")
        try:
            text = held[0].value if held else ""
        except SexprError as exc:
            raise NetlistFormatError(f"{where}: {exc}") from exc
        if text:
            found.append((_text(field, "name", where), text))
    return tuple(found)


def _net(node: SList) -> Net:
    name = _text(node, "name", "a net")
    where = f"net {name}"
    return Net(
        name=name,
        nodes=tuple(
            Node(
                ref=_text(n, "ref", where),
                pin=_text(n, "pin", where),
                pin_function=_optional(n, "pinfunction", where),
                pin_type=_text(n, "pintype", where),
            )
            for n in node.lists("node")
        ),
    )


def _child(node: SList, head: str, where: str) -> SList:
    child = node.first(head)
    if child is None:
        raise NetlistFormatError(f"{where} has no ({head} ...)")
    return child


def _text(node: SList, head: str, where: str) -> str:
    """The one value of a required child, such as ``(ref "U1")``."""
    child = _child(node, head, where)
    value = child.items[1] if len(child.items) == 2 else None
    if not isinstance(value, Atom):
        raise NetlistFormatError(f"{where}: ({head} ...) does not hold one value")
    try:
        return value.value
    except SexprError as exc:
        raise NetlistFormatError(f"{where}: {exc}") from exc


def _optional(node: SList, head: str, where: str) -> str:
    """A child's value, or empty when it is absent or written with no value."""
    child = node.first(head)
    if child is None or len(child.items) == 1:
        return ""
    return _text(node, head, where)
