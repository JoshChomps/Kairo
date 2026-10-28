"""Read a schematic a person drew into a design Kairo can check.

The parts and connections come from KiCad's own netlist, so KiCad stays
the referee of what the drawing connects. A netlist says nothing about
what a net is for or what a part's datasheet promises, and Kairo does not
guess either: every net is a ``signal`` until the caller states its role,
and a part has no electrical facts until the caller gives them. Those
statements are the ``facts``, laid out like the same parts of a design::

    {"nets": {"GND": {"role": "ground"},
              "+3V3": {"role": "power", "voltage": "3.3V"}},
     "components": {"U1": {"params": {"regulator": "linear", "i_q": "5mA"},
                           "source": "datasheet rev C, page 5"}},
     "loads": [{"net": "+3V3", "current": "120mA"}],
     "requirements": [...]}

The result is for checking, not for drawing: pins carry the number, name
and type KiCad's netlist gives and no geometry, and the symbols are not
looked up in the installed libraries, because a drawn board may use
symbols this machine does not have.

One thing a drawing often does say about a part is which part it is: a
field holding the maker's part number or a distributor's. Where the
facts give a part no ``mpn``, the drawing's is used, and with a parts
catalogue the part then takes the figures listed for it, as a design's
part does (`kairo.parts.fill`).

How the netlist marks an open pin was read from netlists kicad-cli 10.0.6
wrote for the boards in ``tests/fixtures/real``: a pin with a no-connect
flag has ``+no_connect`` added to its type, as in ``input+no_connect``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import ValidationError

from kairo.ir import Design
from kairo.kicad.netlist import Component, Net, Netlist
from kairo.library.grounding import Grounded, GroundedPin
from kairo.library.reader import Pin
from kairo.parts.catalogue import Catalogue
from kairo.parts.fill import Filled, Remembered, fill, fill_from_store
from kairo.parts.store import PartStore

#: What the caller may state about a drawn schematic.
FACT_KEYS = ("nets", "components", "loads", "requirements", "policy")
#: What may be stated about one part. Its symbol, footprint and value are
#: the drawing's and are not restated.
PART_KEYS = ("params", "source", "limits", "pin_ratings")
#: Pin types that mark a supply connection.
SUPPLY_TYPES = frozenset({"power_in", "power_out"})

_NO_CONNECT = "+no_connect"
_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_LIB_ID = re.compile(r"[^:\s]+:[^:\s]+")


class DrawnError(ValueError):
    """A drawn schematic cannot be read as a design: its code says why.

    Attributes:
        code: ``E_FACTS_INVALID`` if the facts name something the
            schematic does not have or are laid out wrongly;
            ``E_DRAWN_UNSUPPORTED`` if the schematic holds something a
            design cannot.
    """

    def __init__(self, code: str, message: str) -> None:
        """Keep the code beside the message."""
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Drawn:
    """A drawn schematic as a design.

    Attributes:
        grounded: The design with every pin on its net, as KiCad's
            netlist gives them.
        unstated: Nets that have a supply pin and no stated role, each
            with the supply pins on it. Until their roles are stated the
            power tree does not see them.
        numbered: Parts whose part number was read from a field of
            the drawing, each with the number and the field.
        filled: What the catalogue did for the parts with a part
            number, if there is a catalogue.
        remembered: What the parts store did for them, if there is
            a store.
    """

    grounded: Grounded
    unstated: dict[str, tuple[str, ...]]
    numbered: dict[str, str] = field(default_factory=dict[str, str])
    filled: Filled | None = None
    remembered: Remembered | None = None


def design_name(text: str) -> str:
    """A project's name as a design may be named.

    Args:
        text: The project's file name without its ending.

    Returns:
        The name with every run of other characters replaced by ``_``.
    """
    cleaned = _NAME.sub("_", text).strip("_-")
    return cleaned or "drawn"


#: The names under which drawings give a maker's part number, with letter
#: case and everything but letters and digits taken away. ``Part_number``
#: and ``Manufacturer Part`` are what two of the boards in
#: ``tests/fixtures/real`` use; the rest are common spellings.
MPN_FIELDS = frozenset(
    {
        "mpn",
        "partnumber",
        "manufacturerpart",
        "manufacturerpartnumber",
        "manufacturerpn",
        "mfrpart",
        "mfrpartnumber",
        "mfrpn",
        "mfgpart",
        "mfgpartnumber",
        "mfgpn",
    }
)
_LCSC_NUMBER = re.compile(r"[Cc]\d+")
_SQUEEZE = re.compile(r"[^a-z0-9]+")


def part_numbers(part: Component) -> list[tuple[str, str]]:
    """The part numbers a drawn part's fields give.

    A maker's part number is a field named as `MPN_FIELDS` lists. An LCSC
    number is a field with ``lcsc`` in its name holding ``C`` and digits;
    the boards in ``tests/fixtures/real`` call it ``LCSC``, ``LCSC Part``
    and ``LCSC Part #``.

    Args:
        part: The part, as KiCad's netlist gives it.

    Returns:
        Each number with the field it was in, the maker's first.
    """
    makers: list[tuple[str, str]] = []
    lcsc: list[tuple[str, str]] = []
    for name, text in part.fields:
        squeezed = _SQUEEZE.sub("", name.lower())
        number = text.strip()
        if squeezed in MPN_FIELDS and number:
            makers.append((number, name))
        elif "lcsc" in squeezed and _LCSC_NUMBER.fullmatch(number):
            lcsc.append((number.upper(), name))
    return makers + lcsc


def restrict(
    netlist: Netlist, parts: Sequence[str], facts: Mapping[str, Any] | None = None
) -> tuple[Netlist, dict[str, Any], list[str]]:
    """Keep only some parts of a netlist, and the facts about what is left.

    A whole board cannot be simulated: most of its chips have no model.
    The parts that have (a supply's input, a regulator and its
    capacitors, a filter) can, with everything else on their nets stated
    as a load. This cuts the netlist down to the parts named; a net none
    of them touches goes, and so do the facts about what went.

    Args:
        netlist: The whole netlist.
        parts: The references to keep.
        facts: The facts for the whole schematic.

    Returns:
        The smaller netlist, the facts that still apply, and a line for
        each fact left out, saying why.

    Raises:
        DrawnError: ``E_FACTS_INVALID`` if a named part is not in the
            schematic, or no part is named.
    """
    wanted = set(parts)
    known = {part.ref for part in netlist.components}
    missing = sorted(wanted - known)
    if missing or not wanted:
        raise DrawnError(
            "E_FACTS_INVALID",
            f"parts names {', '.join(missing)}, which the schematic lacks"
            if missing
            else "parts is empty: name the parts to simulate",
        )
    nets = tuple(
        Net(net.name, on_it)
        for net in netlist.nets
        if (on_it := tuple(node for node in net.nodes if node.ref in wanted))
    )
    smaller = Netlist(
        netlist.tool,
        netlist.sheets,
        tuple(part for part in netlist.components if part.ref in wanted),
        nets,
    )
    names = {net.name for net in nets}
    given = dict(facts or {})
    left_out: list[str] = []
    kept: dict[str, Any] = {}
    for key, value in given.items():
        if key == "nets" and isinstance(value, dict):
            stated = cast("dict[str, Any]", value)
            kept[key] = {name: fact for name, fact in stated.items() if name in names}
            left_out += [
                f"net {name}: none of the named parts is on it"
                for name in stated
                if name not in names
            ]
        elif key == "components" and isinstance(value, dict):
            stated = cast("dict[str, Any]", value)
            kept[key] = {ref: fact for ref, fact in stated.items() if ref in wanted}
            left_out += [
                f"part {ref}: not among the named parts"
                for ref in stated
                if ref not in wanted
            ]
        elif key == "loads" and isinstance(value, list):
            loads = cast("list[Any]", value)
            kept[key] = [
                load
                for load in loads
                if not isinstance(load, dict)
                or cast("dict[str, Any]", load).get("net") in names
            ]
            left_out += [
                f"a load on {cast('dict[str, Any]', load).get('net')}: none of the "
                "named parts is on that net"
                for load in loads
                if isinstance(load, dict)
                and cast("dict[str, Any]", load).get("net") not in names
            ]
        elif key == "requirements":
            # A requirement may name anything; simulation judges limits, not these.
            left_out.append("requirements: a simulation judges its own limits")
        else:
            kept[key] = value
    return smaller, kept, left_out


def read_drawn(
    netlist: Netlist,
    name: str,
    facts: Mapping[str, Any] | None = None,
    catalogue: Catalogue | None = None,
    store: PartStore | None = None,
) -> Drawn:
    """Turn KiCad's netlist of a drawn schematic into a design.

    Every part and every connected pin is kept. A pin KiCad marks as
    left open on purpose goes to the design's ``no_connect`` list. A net
    the facts do not mention is a ``signal``. A part the facts give no
    ``mpn`` takes the part number in its fields, if it has one.

    Args:
        netlist: The netlist kicad-cli exported.
        name: The design's name.
        facts: What the caller states: net roles and voltages, parts'
            parameters, loads and requirements.
        catalogue: The parts catalogue, to fill in what it lists for
            each part that has a part number.
        store: The parts store, whose saved figures come first.

    Returns:
        The design, grounded in the netlist, and the nets whose role
        still needs stating.

    Raises:
        DrawnError: ``E_FACTS_INVALID`` if the facts name a net or part
            the schematic does not have, give a key that is not a fact,
            or do not validate as part of a design.
    """
    facts = dict(facts or {})
    extra = sorted(set(facts) - set(FACT_KEYS))
    if extra:
        raise DrawnError(
            "E_FACTS_INVALID",
            f"facts may hold {', '.join(FACT_KEYS)}; it also holds {', '.join(extra)}",
        )
    stated_nets = _mapping(facts.get("nets"), "nets")
    stated_parts = _mapping(facts.get("components"), "components")

    components: dict[str, dict[str, Any]] = {}
    for part in netlist.components:
        entry: dict[str, Any] = {"lib_id": part.lib_id}
        if part.value:
            entry["value"] = part.value
        if _LIB_ID.fullmatch(part.footprint):
            entry["footprint"] = part.footprint
        components[part.ref] = entry
    for ref, stated in stated_parts.items():
        if ref not in components:
            raise DrawnError(
                "E_FACTS_INVALID", f"facts name part {ref!r}, which the schematic lacks"
            )
        given = _mapping(stated, f"components.{ref}")
        unknown = sorted(set(given) - set(PART_KEYS))
        if unknown:
            raise DrawnError(
                "E_FACTS_INVALID",
                f"{ref}: facts may give {', '.join(PART_KEYS)}; the drawing gives "
                f"the rest. Not accepted: {', '.join(unknown)}",
            )
        components[ref].update(given)
    numbered: dict[str, str] = {}
    for part in netlist.components:
        entry = components[part.ref]
        stated = entry.get("params", {})
        found = part_numbers(part)
        if not found or not isinstance(stated, dict) or "mpn" in stated:
            continue
        # Where a part gives both, the one the catalogue holds is the
        # one that can do something.
        number, where = next(
            (
                one
                for one in found
                if catalogue is not None and catalogue.lookup(one[0])
            ),
            found[0],
        )
        entry["params"] = {**stated, "mpn": number}
        numbered[part.ref] = f"{number}, from its field {where!r}"

    nets: dict[str, dict[str, Any]] = {}
    connections: dict[str, list[str]] = {}
    no_connect: list[str] = []
    pins: list[GroundedPin] = []
    open_pins: list[GroundedPin] = []
    unstated: dict[str, tuple[str, ...]] = {}
    for net in netlist.nets:
        supply: list[str] = []
        for node in net.nodes:
            written = f"{node.ref}.{node.pin}"
            kind = node.pin_type
            if kind.endswith(_NO_CONNECT) or kind == "no_connect":
                no_connect.append(written)
                open_pins.append(
                    GroundedPin(
                        node.ref,
                        _pin(
                            node.pin, node.pin_function, kind.removesuffix(_NO_CONNECT)
                        ),
                        "",
                    )
                )
                continue
            connections.setdefault(net.name, []).append(written)
            pins.append(
                GroundedPin(node.ref, _pin(node.pin, node.pin_function, kind), net.name)
            )
            if kind in SUPPLY_TYPES:
                supply.append(written)
        if net.name not in connections:
            continue
        nets[net.name] = {"role": "signal"}
        if supply and net.name not in stated_nets:
            unstated[net.name] = tuple(supply)
    for net_name, stated in stated_nets.items():
        if net_name not in nets:
            raise DrawnError(
                "E_FACTS_INVALID",
                f"facts name net {net_name!r}, which the schematic lacks",
            )
        nets[net_name] = dict(_mapping(stated, f"nets.{net_name}"))

    data: dict[str, Any] = {
        "design": name,
        "nets": nets,
        "components": components,
        "connections": connections,
        "no_connect": no_connect,
        "loads": facts.get("loads", []),
        "requirements": facts.get("requirements", []),
        "policy": facts.get("policy", {}),
    }
    remembered: Remembered | None = None
    if store is not None:
        data, remembered = fill_from_store(data, store)
    filled: Filled | None = None
    if catalogue is not None:
        data, filled = fill(data, catalogue)
    try:
        design = Design.model_validate(data)
    except ValidationError as exc:
        # With the facts taken away only the drawing is left, which tells
        # whose fault the failure is.
        bare: dict[str, Any] = {
            **data,
            "nets": {net_name: {"role": "signal"} for net_name in nets},
            "components": {
                ref: {k: v for k, v in entry.items() if k not in PART_KEYS}
                for ref, entry in components.items()
            },
            "loads": [],
            "requirements": [],
            "policy": {},
        }
        try:
            Design.model_validate(bare)
        except ValidationError as drawing:
            raise DrawnError("E_DRAWN_UNSUPPORTED", _problems(drawing)) from drawing
        raise DrawnError("E_FACTS_INVALID", _problems(exc)) from exc
    return Drawn(
        Grounded(design, {}, tuple(pins), tuple(open_pins)),
        unstated,
        numbered,
        filled,
        remembered,
    )


def _problems(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'design'}: {error['msg']}"
        for error in exc.errors()[:10]
    )


def _pin(number: str, name: str, kind: str) -> Pin:
    """A pin as the netlist describes it: no geometry, which only drawing needs."""
    return Pin(
        number=number,
        name=name,
        electrical_type=kind,
        shape="",
        unit=0,
        body_style=0,
        x=0.0,
        y=0.0,
        angle=0.0,
        length=0.0,
        hidden=False,
    )


def _mapping(value: object, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DrawnError("E_FACTS_INVALID", f"{where} must be a mapping")
    return dict(value)  # pyright: ignore[reportUnknownArgumentType]
