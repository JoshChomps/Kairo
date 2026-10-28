"""Compare what Kairo would simulate with the netlist KiCad itself writes for SPICE.

Kairo builds its circuit from the design. KiCad builds a SPICE netlist
from the schematic file, with its own netlister. For a project Kairo
wrote, the two should name the same resistors, capacitors and inductors,
with the same values, between the same nets. Where they differ, either
the file does not say what the design says or one of the two netlisters
is wrong, and the simulation's numbers are about a circuit that is not
the one drawn.

Only two-pin passives are compared. KiCad writes every other part
according to its ``Sim.*`` fields, which Kairo does not set, so a diode
or a chip appears there with no model and in pin order, and comparing
those lines would only report that.

What KiCad 10.0.6 writes was read from its output for the reference
designs: ``R1 /IN /OUT 1k`` for a resistor, the reference, the two nets
and the value field as it stands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kairo.library.grounding import Grounded
from kairo.library.kinds import PASSIVES
from kairo.units import QuantityError, parse_quantity


@dataclass(frozen=True, slots=True)
class KicadPassive:
    """A two-pin passive as KiCad's SPICE netlist writes it.

    Attributes:
        ref: Its reference.
        nets: The two nets it joins, as KiCad names them.
        value: Its value, as written.
    """

    ref: str
    nets: frozenset[str]
    value: str


def kicad_passives(text: str) -> dict[str, KicadPassive]:
    """Read the resistors, capacitors and inductors out of KiCad's SPICE netlist.

    Args:
        text: The netlist ``kicad-cli sch export netlist --format spice``
            wrote.

    Returns:
        Each line of four fields whose first letter is R, C or L, by
        reference.
    """
    found: dict[str, KicadPassive] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0][:1].upper() in ("R", "C", "L"):
            found[fields[0]] = KicadPassive(
                fields[0], frozenset(fields[1:3]), fields[3]
            )
    return found


def cross_check(
    grounded: Grounded, net_names: Mapping[str, str], text: str
) -> list[str]:
    """Say how KiCad's SPICE netlist differs from the design's passives.

    Args:
        grounded: The design with its pins resolved.
        net_names: KiCad's name for each of the design's nets, as the
            schematic writer gives them.
        text: KiCad's SPICE netlist of the project.

    Returns:
        One line per difference; empty if every passive agrees.
    """
    design = grounded.design
    theirs = kicad_passives(text)
    differences: list[str] = []
    seen: set[str] = set()
    for ref, part in design.components.items():
        kind = PASSIVES.get(part.lib_id)
        if kind is None:
            continue
        nets = [pin.net for pin in grounded.pins if pin.ref == ref]
        if len(nets) != 2:
            continue
        seen.add(ref)
        line = theirs.get(ref)
        if line is None:
            differences.append(f"KiCad's SPICE netlist has no line for {ref}")
            continue
        ours = frozenset(net_names.get(net, net) for net in nets)
        if line.nets != ours:
            differences.append(
                f"{ref} joins {' and '.join(sorted(ours))} in the design and "
                f"{' and '.join(sorted(line.nets))} in KiCad's SPICE netlist"
            )
        try:
            mine = parse_quantity(part.value or "", kind[1], bare=True).value
            other = parse_quantity(line.value, kind[1], bare=True).value
        except QuantityError:
            if (part.value or "") != line.value:
                differences.append(
                    f"{ref} is {part.value!r} in the design and {line.value!r} in "
                    "KiCad's SPICE netlist"
                )
            continue
        if abs(mine - other) > 1e-9 * max(abs(mine), abs(other)):
            differences.append(
                f"{ref} is {part.value} in the design and {line.value} in KiCad's "
                "SPICE netlist"
            )
    for ref in sorted(set(theirs) - seen):
        if ref not in design.components:
            differences.append(
                f"KiCad's SPICE netlist has {ref}, which the design does not"
            )
    return differences
