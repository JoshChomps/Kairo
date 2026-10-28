"""JLCPCB's assembly files, from what KiCad writes for any maker.

JLCPCB takes a bill of materials and a placement ("CPL") file in its own
columns. Its guide for KiCad renames the columns of KiCad's placement file
(``Ref``, ``PosX``, ``PosY``, ``Rot``, ``Side`` to ``Designator``, ``Mid
X``, ``Mid Y``, ``Rotation``, ``Layer``), and its BOM lists each kind of
part once: ``Comment`` (the value), ``Designator`` (every reference),
``Footprint`` and ``LCSC Part #``, the number its assembly line orders by.

A part's LCSC number comes from the parts catalogue, by the maker's part
number the design gives it (``params.mpn``); a part with none is listed
with the column empty and named, never guessed. JLCPCB's assembly can
place a part at another rotation than KiCad's footprint has; its preview
shows each, and no correction is applied here.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Mapping

#: KiCad's placement columns, and JLCPCB's names for them.
_PLACEMENT = {
    "Ref": "Designator",
    "PosX": "Mid X",
    "PosY": "Mid Y",
    "Rot": "Rotation",
    "Side": "Layer",
}


def placement(kicad_pos: str) -> str:
    """JLCPCB's placement file from KiCad's (``pcb export pos``, CSV, mm).

    Args:
        kicad_pos: The text KiCad wrote.

    Returns:
        The same rows and values, under JLCPCB's column names.

    Raises:
        ValueError: If a column JLCPCB needs is missing.
    """
    rows = list(csv.DictReader(io.StringIO(kicad_pos)))
    if rows and not set(_PLACEMENT) <= set(rows[0]):
        missing = sorted(set(_PLACEMENT) - set(rows[0]))
        raise ValueError(f"KiCad's placement file lacks {', '.join(missing)}")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(list(_PLACEMENT.values()))
    for row in rows:
        writer.writerow([row[column] for column in _PLACEMENT])
    return out.getvalue()


def bill(
    kicad_bom: str,
    mpn_of: Mapping[str, str],
    lcsc_for: Callable[[str], str | None],
) -> tuple[str, list[str]]:
    """JLCPCB's BOM from KiCad's (``sch export bom``, one row per part).

    Args:
        kicad_bom: The text KiCad wrote, with its ``Refs``, ``Value`` and
            ``Footprint`` columns.
        mpn_of: Each reference's maker's part number, where the design
            gives one.
        lcsc_for: The LCSC number (``C`` and digits) for a part number, or
            ``None`` where the catalogue has none.

    Returns:
        The BOM, one row per kind of part, and the references with no LCSC
        number, a line each.
    """
    groups: dict[tuple[str, str, str], list[str]] = {}
    for row in csv.DictReader(io.StringIO(kicad_bom)):
        for ref in (r.strip() for r in row["Refs"].split(",")):
            if not ref:
                continue
            footprint = row["Footprint"].split(":", 1)[-1]
            key = (row["Value"], footprint, mpn_of.get(ref, ""))
            groups.setdefault(key, []).append(ref)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    unsourced: list[str] = []
    for (value, footprint, mpn), refs in groups.items():
        number = lcsc_for(mpn) if mpn else None
        if number is None:
            reason = (
                f"part number {mpn} is not in the catalogue"
                if mpn
                else ("no part number in the design")
            )
            unsourced.append(f"{', '.join(refs)} ({value}): {reason}")
        writer.writerow([value, ",".join(refs), footprint, number or ""])
    return out.getvalue(), unsourced
