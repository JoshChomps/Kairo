# A design arrives here as plain JSON, before it is validated, which a
# strict type checker cannot follow.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
"""Give a design the figures of the parts it names, from the catalogue.

A design that gives a part its maker's part number (``mpn``) has said
which part it is. What the catalogue lists for that part (its tolerance,
its ratings, a capacitor's dielectric, an inductor's saturation current)
is then filled in where the design does not state it. A figure the
design states is never replaced, and the part's ``source`` says which
figures came from the catalogue and that they were not checked against
the datasheet.

The catalogue also catches a slip that nothing else would: a part number
that is not the part the design describes. A resistor marked 10k whose
part number is a 12k part, or a capacitor whose part number is a
resistor, takes nothing from the catalogue and is reported.

The parts store comes before the catalogue. It holds what was saved
for a part number from its datasheet, each figure with its page, which
is the only place a chip's figures can come from and is a better
source for any part than a distributor's listing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from kairo.ir.models import PARAMETERS
from kairo.library.kinds import DIODES, PASSIVES
from kairo.parts.catalogue import SOURCE, Catalogue, CataloguePart
from kairo.parts.store import PartsError, PartStore
from kairo.units import Quantity, QuantityError, format_quantity, parse_quantity

_KINDS = {"R": ("resistor", "ohm"), "C": ("capacitor", "F"), "L": ("inductor", "H")}
#: How far a design's value may be from the catalogue's and still be the
#: same part: the listing's figures are text, so this allows for rounding.
SAME_VALUE = 1e-3


@dataclass(slots=True)
class Filled:
    """What the catalogue did for a design.

    Attributes:
        snapshot: The day the catalogue's listings were read.
        filled: For each part that took figures, which ones, a line each.
        not_found: Parts whose part number the catalogue does not hold.
        mismatched: Parts whose part number is another part than the
            design describes, a line each saying how.
    """

    snapshot: str
    filled: list[str] = field(default_factory=list[str])
    not_found: list[str] = field(default_factory=list[str])
    mismatched: list[str] = field(default_factory=list[str])

    def __bool__(self) -> bool:
        """Whether there is anything to tell."""
        return bool(self.filled or self.not_found or self.mismatched)

    def as_dict(self) -> dict[str, Any]:
        """The notes as plain data, the mismatches first."""
        return {
            "mismatched": list(self.mismatched),
            "not_found": list(self.not_found),
            "filled": list(self.filled),
            "note": f"Figures are from {SOURCE} as read on {self.snapshot}, a "
            "distributor's listing and not the datasheet. A figure the design "
            "states was never replaced.",
        }


def fill(design: dict[str, Any], catalogue: Catalogue) -> tuple[dict[str, Any], Filled]:
    """Fill in a design's unstated figures for parts that name a part number.

    Args:
        design: The design as data, before it is validated. It is not
            changed.
        catalogue: The catalogue.

    Returns:
        A copy of the design with the figures filled in, and what was
        done. A design that is not shaped as one is returned as it is.
    """
    notes = Filled(catalogue.date)
    components = design.get("components")
    if not isinstance(components, dict):
        return design, notes
    out = copy.deepcopy(design)
    for ref, part in out["components"].items():
        if not isinstance(part, dict):
            continue
        params = part.setdefault("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("mpn"), str):
            continue
        number = params["mpn"]
        kind = _kind(str(part.get("lib_id", "")))
        if kind is None:
            # A chip: a listing's figures for one are for choosing it and
            # do not say which are limits, so none is taken.
            continue
        found = catalogue.lookup(number)
        if found is None:
            notes.not_found.append(f"{ref}: {number}")
            continue
        if found.kind != kind[0]:
            notes.mismatched.append(
                f"{ref} is drawn as a {kind[0]} and its part number {number} is "
                f"a {found.kind} in the catalogue; nothing was taken from it"
            )
            continue
        clash = _value_clash(ref, part, found, kind[1])
        if clash is not None:
            notes.mismatched.append(clash)
            continue
        taken = _take(part, params, found, kind[1])
        if taken:
            stated = part.get("source") or params.get("source") or "user-stated"
            cited = (
                f"{stated}; {SOURCE} entry C{found.lcsc} for {found.mpn} (read "
                f"{catalogue.date}, not checked against the datasheet): "
                f"{', '.join(taken)}"
            )
            if "source" in params:
                params["source"] = cited
            else:
                part["source"] = cited
            notes.filled.append(f"{ref} ({found.mpn}): {', '.join(taken)}")
    return out, notes


def _kind(lib_id: str) -> tuple[str, str] | None:
    letter = PASSIVES.get(lib_id, ("", ""))[0]
    if letter in _KINDS:
        return _KINDS[letter]
    if lib_id in DIODES and "LED" in lib_id:
        return ("led", "V")
    return None


def _value_clash(
    ref: str, part: dict[str, Any], found: CataloguePart, unit: str
) -> str | None:
    """Say how a stated value differs from the catalogue's, if it does."""
    written = part.get("value")
    if found.kind == "led" or not isinstance(written, str) or found.value is None:
        return None
    try:
        stated = parse_quantity(written, unit, bare=True).value
    except QuantityError:
        return None
    if abs(stated - found.value) <= SAME_VALUE * max(abs(stated), abs(found.value)):
        return None
    listed = _text(found.value, unit)
    return (
        f"{ref} is marked {written} and its part number {found.mpn} is a "
        f"{listed} part in the catalogue; nothing was taken from it"
    )


def _text(value: float, unit: str) -> str:
    """A value as a design writes one, to six figures and with no padding."""
    text = format_quantity(Quantity(float(f"{value:.6g}"), unit))
    number = text[: -len(unit)]
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number + unit


def _take(
    part: dict[str, Any], params: dict[str, Any], found: CataloguePart, unit: str
) -> list[str]:
    """Fill what the design does not state, and name what was filled."""
    figures: dict[str, str | None] = {}
    if found.tolerance is not None:
        figures["tolerance"] = f"{found.tolerance:g}%"
    if found.kind == "resistor":
        if found.p_rating is not None:
            figures["p_rating"] = f"{found.p_rating:g}W"
        if found.tempco is not None:
            figures["tempco"] = f"{found.tempco:g}ppm/degC"
    elif found.kind == "capacitor":
        if found.v_rating is not None:
            figures["v_rating"] = f"{found.v_rating:g}V"
        figures["dielectric"] = found.dielectric
    elif found.kind == "inductor":
        if found.i_sat is not None:
            figures["i_sat"] = f"{found.i_sat:g}A"
        if found.i_rating is not None:
            figures["i_rating"] = f"{found.i_rating:g}A"
    else:
        if found.v_f is not None:
            figures["v_f"] = f"{found.v_f:g}V"
        if found.v_f_tolerance is not None:
            figures["v_f_tolerance"] = f"{found.v_f_tolerance:.3g}%"
    taken: list[str] = []
    if found.kind != "led" and found.value is not None and not part.get("value"):
        part["value"] = _text(found.value, unit)
        taken.append("value")
    for name, text in figures.items():
        if text is not None and name not in params:
            params[name] = text
            taken.append(name)
    return taken


@dataclass(slots=True)
class Remembered:
    """What the parts store did for a design.

    Attributes:
        filled: For each part that took figures, which ones and whether a
            person has checked them, a line each.
        unusable: Parts whose file in the store cannot be read, with why.
    """

    filled: list[str] = field(default_factory=list[str])
    unusable: list[str] = field(default_factory=list[str])

    def __bool__(self) -> bool:
        """Whether there is anything to tell."""
        return bool(self.filled or self.unusable)

    def as_dict(self) -> dict[str, Any]:
        """The notes as plain data."""
        return {
            "unusable": list(self.unusable),
            "filled": list(self.filled),
            "note": "Figures saved for these part numbers were used where the "
            "design states none. One marked unreviewed was written down by a "
            "model or a program and no person has checked it against the "
            "datasheet: check it at the source the part now cites.",
        }


def fill_from_store(
    design: dict[str, Any], store: PartStore
) -> tuple[dict[str, Any], Remembered]:
    """Fill in a design's unstated figures from what was saved for its parts.

    A part that gives its ``mpn`` takes each figure the store holds for
    that number, where the design states none, and where the figure is
    marked as the datasheet's minimum or maximum the part's ``limits``
    says so. A figure the design states is never replaced.

    Args:
        design: The design as data, before it is validated. It is not
            changed.
        store: The parts store.

    Returns:
        A copy of the design with the figures filled in, and what was
        done. A design that is not shaped as one is returned as it is.
    """
    notes = Remembered()
    components = design.get("components")
    if not isinstance(components, dict):
        return design, notes
    out = copy.deepcopy(design)
    for ref, part in out["components"].items():
        if not isinstance(part, dict):
            continue
        params = part.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("mpn"), str):
            continue
        try:
            known = store.get(params["mpn"])
        except PartsError as exc:
            notes.unusable.append(f"{ref}: {exc}")
            continue
        if known is None:
            continue
        limits = part.get("limits", {})
        taken: dict[str, list[str]] = {}
        for name, fact in known.facts.items():
            if name not in PARAMETERS or name == "mpn" or name in params:
                continue
            params[name] = fact.value
            if fact.limit is not None and isinstance(limits, dict):
                limits[name] = fact.limit
                part["limits"] = limits
            side = f" ({fact.limit})" if fact.limit else ""
            taken.setdefault(f"{fact.source.cite()}, {fact.review}", []).append(
                name + side
            )
        if not taken:
            continue
        stated = part.get("source") or params.get("source") or "user-stated"
        cited = "; ".join(
            f"{where}: {', '.join(names)}" for where, names in taken.items()
        )
        text = f"{stated}; saved for {known.mpn}, {cited}"
        if "source" in params:
            params["source"] = text
        else:
            part["source"] = text
        notes.filled.append(f"{ref} ({known.mpn}): {cited}")
    return out, notes
