"""Read symbols and footprints out of parsed KiCad library files.

Only what the pin resolver, the writer and the pin-to-pad check need is
read: each symbol's pins (with unit, position and alternates) and its
identifying fields, and each footprint's pads. Anything in a file that does
not have the shape described below is refused with `LibraryFormatError`
naming the symbol, never skipped or filled in.

The shapes were taken from a survey of every official library in a KiCad
10.0.6 install (223 symbol libraries holding 22,860 symbols and 541,758
pins; 15,450 footprints holding 386,597 pads; 2026-10-08):

- A symbol's body sits in child symbols named ``<name>_<unit>_<style>``.
  Unit 0 means every unit and style 0 every body style. No symbol has pins
  directly under it.
- A derived symbol holds ``(extends "<parent>")``, its own properties and
  nothing else; its body is the body of the symbol at the end of its
  ``extends`` chain (chains up to four long occur).
- A pin is ``(pin <type> <shape> (at x y angle) (length l) (name "..")
  (number "..") [(hide yes)] [(alternate "<name>" <type> <shape>) ...])``.
- A property is ``(property "<key>" "<value>" ...)``, or ``(property private
  "<key>" "<value>" ...)`` for a note KiCad does not show; private ones may
  repeat a key and are not read.
- A pad is ``(pad "<number>" <type> <shape> ...)``; the number may be empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kairo.kicad.formats import FOOTPRINT, SYMBOL_LIBRARY, check_format
from kairo.kicad.sexpr import Atom, Document, SexprError, SList

#: Every pin electrical type KiCad 10.0.6 writes; all twelve occur in the
#: official libraries.
ELECTRICAL_TYPES = frozenset(
    {
        "input",
        "output",
        "bidirectional",
        "tri_state",
        "passive",
        "free",
        "unspecified",
        "power_in",
        "power_out",
        "open_collector",
        "open_emitter",
        "no_connect",
    }
)

_UNIT_SUFFIX = re.compile(r"_(\d+)_(\d+)")
# Plain decimals only: Python's float() would also take "nan", "inf" and
# "1_0", none of which KiCad writes.
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class LibraryFormatError(ValueError):
    """A library file is not laid out the way the reader has seen KiCad write it."""


@dataclass(frozen=True, slots=True)
class PinAlternate:
    """Another function a pin can take, chosen per instance in the schematic.

    Attributes:
        name: The alternate's name.
        electrical_type: Its electrical type, one of `ELECTRICAL_TYPES`.
        shape: Its graphic shape, for example ``line``.
    """

    name: str
    electrical_type: str
    shape: str


@dataclass(frozen=True, slots=True)
class Pin:
    """One pin of a symbol, as drawn in the library.

    Attributes:
        number: The pin number, which is text (``1``, ``A12``); may be empty.
        name: The pin name; empty when the library gives none.
        electrical_type: One of `ELECTRICAL_TYPES`.
        shape: The graphic shape, for example ``line`` or ``inverted``.
        unit: The unit it belongs to, or 0 for every unit.
        body_style: The body style it belongs to, or 0 for every style.
        x: Where the pin's connection point is in the symbol, in mm.
        y: The same, in the library's y-up coordinates, in mm.
        angle: The direction the pin runs from that point, in degrees.
        length: The pin's length, in mm.
        hidden: True if the library hides the pin.
        alternates: The other functions it offers.
    """

    number: str
    name: str
    electrical_type: str
    shape: str
    unit: int
    body_style: int
    x: float
    y: float
    angle: float
    length: float
    hidden: bool
    alternates: tuple[PinAlternate, ...] = ()


@dataclass(frozen=True, slots=True)
class Symbol:
    """One symbol in a library.

    Attributes:
        name: Its name within the library.
        extends: The symbol it is derived from, or ``None``.
        body: The symbol whose body it uses: the end of its ``extends``
            chain, or itself.
        reference: The reference prefix, for example ``U``.
        value: The Value property.
        footprint: The default footprint, often empty.
        datasheet: The Datasheet property.
        description: The Description property.
        keywords: The ``ki_keywords`` property.
        footprint_filters: The ``ki_fp_filters`` property.
        is_power: True for a power symbol such as ``GND``.
        unit_count: How many units the symbol has, at least 1.
        pins: Every pin of every unit and body style.
    """

    name: str
    extends: str | None
    body: str
    reference: str
    value: str
    footprint: str
    datasheet: str
    description: str
    keywords: str
    footprint_filters: str
    is_power: bool
    unit_count: int
    pins: tuple[Pin, ...]


@dataclass(frozen=True, slots=True)
class Pad:
    """One pad of a footprint.

    Attributes:
        number: The pad number, which is text; empty for an unnumbered pad
            such as a mounting hole.
        type: ``smd``, ``thru_hole``, ``np_thru_hole`` or ``connect``.
        shape: The pad shape, for example ``roundrect``.
    """

    number: str
    type: str
    shape: str


@dataclass(frozen=True, slots=True)
class Footprint:
    """One footprint.

    Attributes:
        name: Its name within the library.
        description: The ``descr`` text, or empty.
        tags: The ``tags`` text, or empty.
        pads: Every pad, in file order.
    """

    name: str
    description: str
    tags: str
    pads: tuple[Pad, ...]


def read_symbol_library(document: Document) -> list[Symbol]:
    """Read every symbol in a parsed ``.kicad_sym`` file.

    Args:
        document: The parsed library.

    Returns:
        The symbols, in file order. A derived symbol carries the pins of the
        symbol whose body it uses.

    Raises:
        UnsupportedFormatError: If the file is not a symbol library in a
            format version Kairo reads.
        LibraryFormatError: If a symbol is not laid out as described in this
            module.
    """
    check_format(document, SYMBOL_LIBRARY)
    nodes: dict[str, SList] = {}
    for node in document.root.lists("symbol"):
        name = _string(node, 1, "a symbol")
        if name in nodes:
            raise LibraryFormatError(f"symbol {name!r} appears twice")
        nodes[name] = node

    bodies: dict[str, tuple[tuple[Pin, ...], int, bool]] = {}
    symbols: list[Symbol] = []
    for name, node in nodes.items():
        body_name, extends = _body_of(name, nodes)
        if body_name not in bodies:
            bodies[body_name] = _read_body(body_name, nodes[body_name])
        pins, unit_count, is_power = bodies[body_name]
        properties = _properties(name, node)
        symbols.append(
            Symbol(
                name=name,
                extends=extends,
                body=body_name,
                reference=properties.get("Reference", ""),
                value=properties.get("Value", ""),
                footprint=properties.get("Footprint", ""),
                datasheet=properties.get("Datasheet", ""),
                description=properties.get("Description", ""),
                keywords=properties.get("ki_keywords", ""),
                footprint_filters=properties.get("ki_fp_filters", ""),
                is_power=is_power,
                unit_count=unit_count,
                pins=pins,
            )
        )
    return symbols


def read_footprint(document: Document) -> Footprint:
    """Read a parsed ``.kicad_mod`` file.

    Args:
        document: The parsed footprint.

    Returns:
        The footprint and its pads.

    Raises:
        UnsupportedFormatError: If the file is not a footprint in a format
            version Kairo reads.
        LibraryFormatError: If a pad is not laid out as described in this
            module.
    """
    check_format(document, FOOTPRINT)
    root = document.root
    name = _string(root, 1, "the footprint")
    where = f"footprint {name!r}"
    pads = tuple(
        Pad(
            number=_string(pad, 1, f"a pad of {where}"),
            type=_bare(pad, 2, f"a pad of {where}"),
            shape=_bare(pad, 3, f"a pad of {where}"),
        )
        for pad in root.lists("pad")
    )
    return Footprint(
        name=name,
        description=_optional_string(root, "descr", where),
        tags=_optional_string(root, "tags", where),
        pads=pads,
    )


def _body_of(name: str, nodes: dict[str, SList]) -> tuple[str, str | None]:
    """Follow a symbol's ``extends`` chain to the symbol that holds its body."""
    extends: str | None = None
    current = name
    seen = {name}
    while (link := nodes[current].first("extends")) is not None:
        parent = _string(link, 1, f"the extends of symbol {current!r}")
        if extends is None:
            extends = parent
        if parent not in nodes:
            raise LibraryFormatError(
                f"symbol {current!r} extends {parent!r}, which is not in the library"
            )
        if parent in seen:
            raise LibraryFormatError(f"symbol {name!r} extends itself through a loop")
        if nodes[current].first("symbol") is not None:
            raise LibraryFormatError(
                f"derived symbol {current!r} has a body of its own"
            )
        seen.add(parent)
        current = parent
    return current, extends


def _read_body(name: str, node: SList) -> tuple[tuple[Pin, ...], int, bool]:
    """A symbol's pins, its unit count, and whether it is a power symbol."""
    if node.first("pin") is not None:
        raise LibraryFormatError(f"symbol {name!r} has a pin outside any unit")
    pins: list[Pin] = []
    unit_count = 1
    for child in node.lists("symbol"):
        child_name = _string(child, 1, f"a unit of symbol {name!r}")
        suffix = (
            _UNIT_SUFFIX.fullmatch(child_name, len(name))
            if child_name.startswith(name)
            else None
        )
        if suffix is None:
            raise LibraryFormatError(
                f"unit {child_name!r} of symbol {name!r} is not named "
                f"<name>_<unit>_<style>"
            )
        unit, style = int(suffix.group(1)), int(suffix.group(2))
        unit_count = max(unit_count, unit)
        pins.extend(_pin(pin, unit, style, name) for pin in child.lists("pin"))
    return tuple(pins), unit_count, node.first("power") is not None


def _pin(node: SList, unit: int, style: int, symbol: str) -> Pin:
    where = f"a pin of symbol {symbol!r}"
    number = _string(_child(node, "number", where), 1, where)
    where = f"pin {number!r} of symbol {symbol!r}"
    at = _child(node, "at", where)
    if len(at.items) != 4:
        raise LibraryFormatError(f"{where} has no (at x y angle)")
    hide = node.first("hide")
    hidden = False
    if hide is not None:
        flag = _bare(hide, 1, where)
        if flag not in ("yes", "no"):
            raise LibraryFormatError(f"{where} has (hide {flag})")
        hidden = flag == "yes"
    return Pin(
        number=number,
        name=_string(_child(node, "name", where), 1, where),
        electrical_type=_electrical_type(node, 1, where),
        shape=_bare(node, 2, where),
        unit=unit,
        body_style=style,
        x=_number(at, 1, where),
        y=_number(at, 2, where),
        angle=_number(at, 3, where),
        length=_number(_child(node, "length", where), 1, where),
        hidden=hidden,
        alternates=tuple(
            PinAlternate(
                name=_string(alt, 1, f"an alternate of {where}"),
                electrical_type=_electrical_type(alt, 2, f"an alternate of {where}"),
                shape=_bare(alt, 3, f"an alternate of {where}"),
            )
            for alt in node.lists("alternate")
        ),
    )


def _properties(symbol: str, node: SList) -> dict[str, str]:
    found: dict[str, str] = {}
    for prop in node.lists("property"):
        where = f"a property of symbol {symbol!r}"
        first = prop.items[1] if len(prop.items) > 1 else None
        if isinstance(first, Atom) and first.raw == "private":
            continue
        key = _string(prop, 1, where)
        if key in found:
            raise LibraryFormatError(f"symbol {symbol!r} has two {key!r} properties")
        found[key] = _string(prop, 2, where)
    return found


def _child(node: SList, head: str, where: str) -> SList:
    child = node.first(head)
    if child is None:
        raise LibraryFormatError(f"{where} has no ({head} ...)")
    return child


def _atom(node: SList, index: int, where: str) -> Atom:
    item = node.items[index] if index < len(node.items) else None
    if not isinstance(item, Atom):
        raise LibraryFormatError(f"{where}: ({node.head} ...) is missing a value")
    return item


def _string(node: SList, index: int, where: str) -> str:
    atom = _atom(node, index, where)
    if not atom.quoted:
        raise LibraryFormatError(f"{where}: expected a quoted string, got {atom.raw}")
    try:
        return atom.value
    except SexprError as exc:
        raise LibraryFormatError(f"{where}: {exc}") from exc


def _bare(node: SList, index: int, where: str) -> str:
    atom = _atom(node, index, where)
    if atom.quoted:
        raise LibraryFormatError(f"{where}: expected a keyword, got {atom.raw}")
    return atom.raw


def _number(node: SList, index: int, where: str) -> float:
    raw = _bare(node, index, where)
    if not _NUMBER.fullmatch(raw):
        raise LibraryFormatError(f"{where}: {raw!r} is not a number")
    return float(raw)


def _electrical_type(node: SList, index: int, where: str) -> str:
    kind = _bare(node, index, where)
    if kind not in ELECTRICAL_TYPES:
        raise LibraryFormatError(f"{where} has unknown electrical type {kind!r}")
    return kind


def _optional_string(node: SList, head: str, where: str) -> str:
    child = node.first(head)
    return "" if child is None else _string(child, 1, f"the {head} of {where}")
