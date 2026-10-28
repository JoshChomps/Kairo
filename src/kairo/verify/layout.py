"""Layer L2, the drawing: can the sheet be read?

The writer reports where it put every symbol, wire and text. These checks
read that report, never the writer's intentions: two symbols may not
overlap, everything must be on the sheet, no text may lie over another
text, a symbol, a wire or a pin's number or name, and a part's reference
and value must be nearer that part than any other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kairo.kicad.geometry import Box
from kairo.verify.findings import Finding, Where

#: Boxes closer than this are taken to touch, in mm; it absorbs rounding.
TOUCH = 0.01


@dataclass(frozen=True, slots=True)
class DrawnText:
    """A text on the sheet.

    Attributes:
        owner: The part it belongs to, or the net for a net label.
        name: ``Reference``, ``Value`` or ``label``.
        text: What it says.
        box: The room it takes on the sheet.
        is_part: True if ``owner`` is one of the design's parts, whose
            body the text should stay nearest.
    """

    owner: str
    name: str
    text: str
    box: Box
    is_part: bool


@dataclass(frozen=True, slots=True)
class Drawing:
    """Where the writer put things.

    Attributes:
        sheet: The area inside the sheet's frame.
        bodies: Each symbol's box by its key; the design's parts by
            reference, power symbols by a key of their own.
        parts: The keys in ``bodies`` that are the design's parts.
        wires: Each wire segment's box.
        texts: Every shown text.
        pin_texts: The pin numbers and names drawn along symbols' pins,
            which no other text may cover.
    """

    sheet: Box
    bodies: dict[str, Box]
    parts: frozenset[str]
    wires: tuple[Box, ...]
    texts: tuple[DrawnText, ...]
    pin_texts: tuple[DrawnText, ...] = ()


def check_layout(drawing: Drawing) -> list[Finding]:
    """Check that a drawing can be read.

    Args:
        drawing: Where the writer put things.

    Returns:
        One finding per problem.
    """
    findings: list[Finding] = []
    parts = sorted(drawing.parts)
    for ref in parts:
        if not _inside(drawing.bodies[ref], drawing.sheet):
            findings.append(
                Finding(
                    "L2",
                    "E_OFF_SHEET",
                    "error",
                    f"{ref} is drawn partly outside the sheet",
                    Where(ref=ref),
                )
            )
    for index, first in enumerate(parts):
        for second in parts[index + 1 :]:
            if _overlap(drawing.bodies[first], drawing.bodies[second]):
                findings.append(
                    Finding(
                        "L2",
                        "E_SYMBOLS_OVERLAP",
                        "error",
                        f"{first} and {second} are drawn over each other",
                        Where(ref=first),
                    )
                )

    for number, text in enumerate(drawing.texts):
        what = _describe(text)
        where = Where(
            ref=text.owner if text.is_part else None,
            net=text.owner if text.name == "label" else None,
        )
        if not _inside(text.box, drawing.sheet):
            findings.append(
                Finding("L2", "E_OFF_SHEET", "error", f"{what} is off the sheet", where)
            )
        for other in drawing.texts[number + 1 :]:
            if _overlap(text.box, other.box):
                findings.append(
                    Finding(
                        "L2",
                        "E_TEXT_OVERLAP",
                        "error",
                        f"{what} is drawn over {_describe(other)}",
                        where,
                    )
                )
        for key, body in drawing.bodies.items():
            # A net label ends on its own symbol's stub and may touch it.
            if _overlap(text.box, body) and not (
                text.name == "label" and key not in drawing.parts
            ):
                findings.append(
                    Finding(
                        "L2",
                        "E_TEXT_OVERLAP",
                        "error",
                        f"{what} is drawn over {_name(key, drawing)}",
                        where,
                    )
                )
        for pin in drawing.pin_texts:
            if _overlap(text.box, pin.box):
                findings.append(
                    Finding(
                        "L2",
                        "E_TEXT_OVERLAP",
                        "error",
                        f"{what} is drawn over {_describe(pin)}",
                        where,
                    )
                )
        if text.name != "label":
            if any(_overlap(text.box, wire) for wire in drawing.wires):
                findings.append(
                    Finding(
                        "L2",
                        "E_TEXT_OVERLAP",
                        "error",
                        f"{what} is drawn over a wire",
                        where,
                    )
                )
            if text.is_part:
                own = _gap(text.box, drawing.bodies[text.owner])
                nearer = [
                    ref
                    for ref in parts
                    if ref != text.owner
                    and _gap(text.box, drawing.bodies[ref]) < own - TOUCH
                ]
                if nearer:
                    findings.append(
                        Finding(
                            "L2",
                            "E_TEXT_AMBIGUOUS",
                            "error",
                            f"{what} is nearer {nearer[0]} than {text.owner}, so "
                            "it reads as the wrong part's",
                            where,
                        )
                    )
    return findings


def _describe(text: DrawnText) -> str:
    if text.name == "label":
        return f"the label {text.text!r}"
    if text.name == "pin":
        return f"the pin text {text.text!r} of {text.owner}"
    return f"the {text.name.lower()} {text.text!r} of {text.owner}"


def _name(key: str, drawing: Drawing) -> str:
    return key if key in drawing.parts else "a power symbol"


def _overlap(a: Box, b: Box) -> bool:
    return (
        a.left < b.right - TOUCH
        and b.left < a.right - TOUCH
        and a.top < b.bottom - TOUCH
        and b.top < a.bottom - TOUCH
    )


def _inside(inner: Box, outer: Box) -> bool:
    return (
        inner.left >= outer.left - TOUCH
        and inner.top >= outer.top - TOUCH
        and inner.right <= outer.right + TOUCH
        and inner.bottom <= outer.bottom + TOUCH
    )


def _gap(a: Box, b: Box) -> float:
    """The distance between two boxes, zero if they touch or overlap."""
    dx = max(a.left - b.right, b.left - a.right, 0.0)
    dy = max(a.top - b.bottom, b.top - a.bottom, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def drawing_of(
    sheet: Box,
    bodies: dict[str, Box],
    parts: Sequence[str],
    wires: Sequence[Box],
    texts: Sequence[DrawnText],
    pin_texts: Sequence[DrawnText] = (),
) -> Drawing:
    """Bundle a drawing's facts.

    Args:
        sheet: The area inside the sheet's frame.
        bodies: Each symbol's box by key.
        parts: The keys that are the design's parts.
        wires: Each wire segment's box.
        texts: Every shown text.
        pin_texts: The pin numbers and names drawn along pins, each named
            ``pin``.

    Returns:
        The drawing.
    """
    return Drawing(
        sheet,
        dict(bodies),
        frozenset(parts),
        tuple(wires),
        tuple(texts),
        tuple(pin_texts),
    )
