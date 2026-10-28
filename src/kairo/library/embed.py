"""Turn a library symbol into the copy a schematic carries of it.

A KiCad schematic holds its own copy of every symbol it uses, in
``lib_symbols``. The copy is named ``<nickname>:<name>``. A derived symbol
(one that ``extends`` another) is stored flattened: the flags and drawing
of the symbol at the end of its chain, under the derived symbol's own name
and with the derived symbol's own properties.

That rule is measured against what the KiCad 10 program embedded in the
real-board fixtures: of 89 embedded symbols that the installed libraries
still hold, 75 are reproduced token for token, 6 of the 8 derived ones
among them; the rest differ where the library has since changed or the
designer edited the copy.
"""

from __future__ import annotations

from kairo.kicad.emit import copy
from kairo.kicad.sexpr import Atom, SList
from kairo.library.reader import LibraryFormatError


def embedded_symbol(library: SList, name: str, nickname: str) -> SList:
    """Build the ``lib_symbols`` entry for one symbol of a library.

    Args:
        library: The parsed library's root list.
        name: The symbol's name in the library.
        nickname: The library's nickname.

    Returns:
        A new ``(symbol "<nickname>:<name>" ...)`` list with no whitespace
        set; the library's tree is not touched.

    Raises:
        LibraryFormatError: If the symbol or a symbol it extends is not in
            the library, or its units are not named after it.
    """
    nodes = {
        node.items[1].value: node
        for node in library.lists("symbol")
        if len(node.items) > 1 and isinstance(node.items[1], Atom)
    }
    if name not in nodes:
        raise LibraryFormatError(f"symbol {name!r} is not in library {nickname!r}")
    own = nodes[name]
    base_name, base = name, own
    seen = {name}
    while (link := base.first("extends")) is not None:
        parent = link.items[1]
        if not isinstance(parent, Atom) or parent.value not in nodes:
            raise LibraryFormatError(
                f"symbol {base_name!r} extends a symbol that is not in "
                f"library {nickname!r}"
            )
        if parent.value in seen:
            raise LibraryFormatError(f"symbol {name!r} extends itself through a loop")
        seen.add(parent.value)
        base_name, base = parent.value, nodes[parent.value]

    items: list[Atom | SList] = [
        Atom.symbol("symbol"),
        Atom.string(f"{nickname}:{name}"),
    ]
    properties_written = False
    for child in base.items[2:]:
        if isinstance(child, SList) and child.head == "property":
            # The derived symbol's properties stand where the base's stood.
            if not properties_written:
                properties_written = True
                items.extend(_properties(own, base))
            continue
        if isinstance(child, SList) and child.head == "symbol":
            unit = copy(child)
            assert isinstance(unit, SList)
            unit_name = unit.items[1]
            if not isinstance(unit_name, Atom) or not unit_name.value.startswith(
                base_name + "_"
            ):
                raise LibraryFormatError(
                    f"a unit of symbol {base_name!r} is not named after it"
                )
            unit.items[1] = Atom.string(name + unit_name.value[len(base_name) :])
            items.append(unit)
            continue
        items.append(copy(child))
    return SList(items)


def _properties(own: SList, base: SList) -> list[SList]:
    """The derived symbol's properties, with the ones it inherits added.

    KiCad 10 flattens a derived symbol from a copy of its base, so every
    property of the base that the derived symbol does not set is carried
    over: ``ki_locked`` (an op amp that extends another leaves it out and
    is still locked), a diode's ``Sim.Device`` and ``Sim.Pins``, a private
    field, a ``Manufacturer``. KiCad's rule check reports a copy without
    them as differing from the library (measured with kicad-cli 10.0.6 on
    LM358, 1N4148W, BCX56, ADS7867, W6100-L, 10M08SCE144, RP2354A and
    MAX15062B). They go in the base's order just before the derived
    symbol's first ``ki_`` entry, where KiCad 10 put ``Sim.Device`` and
    ``Sim.Pins`` when it embedded Diode:1N4148 in a real board.
    """
    out = [copy(node) for node in own.lists("property")]
    names = [_property_name(node) for node in own.lists("property")]
    inherited = [
        copy(node)
        for node in base.lists("property")
        if own is not base and _property_name(node) not in names
    ]
    at = next((i for i, name in enumerate(names) if name.startswith("ki_")), len(out))
    out[at:at] = inherited
    return [node for node in out if isinstance(node, SList)]


def _property_name(node: SList) -> str:
    """A property's name, past the bare ``private`` flag if it has one."""
    first = node.items[1] if len(node.items) > 1 else None
    if isinstance(first, Atom) and not first.quoted and first.value == "private":
        return node.values()[1]
    return node.values()[0]
