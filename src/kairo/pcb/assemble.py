"""A board's parts from KiCad's own netlist of the schematic.

KiCad's "update board from schematic" takes each footprint's name, value,
fields, flags and pad nets from the schematic's netlist; Kairo does the
same from ``kicad-cli sch export netlist``, so every pad is on exactly the
net KiCad names, unconnected pins included (``unconnected-(U5-IO3-Pad15)``),
and the schematic parity check of KiCad's DRC has nothing to object to.
Only the place of each part comes from Kairo's placer.
"""

from __future__ import annotations

from collections.abc import Mapping

from kairo.kicad.netlist import Netlist
from kairo.pcb.board_writer import BoardError, PlacedPart

#: Fields every footprint carries, empty when the symbol leaves them so.
ALWAYS_FIELDS = ("Datasheet", "Description")
#: Fields the board holds in its own way, never copied as properties.
OWN_FIELDS = ("Reference", "Value", "Footprint")


def parts_from_netlist(
    netlist: Netlist,
    places: Mapping[str, tuple[float, float, float]],
    root_file: str,
) -> list[PlacedPart]:
    """Every part of a netlist with a footprint, at its place.

    Args:
        netlist: KiCad's netlist of the whole schematic.
        places: Each part's x and y in mm and its turn in degrees.
        root_file: The root sheet's file name, which a part on the root
            sheet names as its sheet file.

    Returns:
        The parts, in the netlist's order.

    Raises:
        BoardError: If a part with a footprint has no place, or is drawn
            as several units.
    """
    pads: dict[str, dict[str, str]] = {}
    for net in netlist.nets:
        for node in net.nodes:
            pads.setdefault(node.ref, {})[node.pin] = net.name
    parts: list[PlacedPart] = []
    for component in netlist.components:
        if not component.footprint:
            continue
        if component.ref not in places:
            raise BoardError(
                f"{component.ref} has a footprint but no place on the board"
            )
        if len(component.units) > 1:
            # Which unit's uuid KiCad puts in the footprint's path is not
            # yet shown by a KiCad-saved board, so it is not guessed.
            raise BoardError(
                f"{component.ref} is drawn as {len(component.units)} units; "
                "Kairo cannot yet name its footprint's path on a board"
            )
        x, y, turn = places[component.ref]
        fields = dict(component.fields)
        flags = {name for name, _ in component.properties}
        sheetfile = dict(component.properties).get("Sheetfile", "")
        parts.append(
            PlacedPart(
                component.ref,
                component.footprint,
                component.value,
                x,
                y,
                turn,
                component.path,
                component.sheet,
                sheetfile or root_file,
                pads.get(component.ref, {}),
                {
                    **{name: "" for name in ALWAYS_FIELDS},
                    **{k: v for k, v in fields.items() if k not in OWN_FIELDS},
                },
                "exclude_from_bom" not in flags,
            )
        )
    return parts
