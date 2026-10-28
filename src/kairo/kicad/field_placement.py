"""Put a schematic's reference and value texts where they can be read.

The writer knows where every symbol, wire, label and power symbol is.
This module turns that into a text-placement problem for
`kairo.text_placement` (each visible field is a label owned by its
symbol; everything drawn is an obstacle), solves it, and gives back a
position for every field. Text sizes come from
`kairo.text_placement.kicad_text`, which is measured from KiCad's own
drawings, so the boxes the placer reasons about are the boxes KiCad
draws.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kairo.kicad.geometry import Box, Placement, to_sheet
from kairo.kicad.sexpr import Atom, SList
from kairo.text_placement import Label, Part, Problem, Rules, Score, Side, solve
from kairo.text_placement.geometry import Box as PlacerBox
from kairo.text_placement.kicad_text import (
    DEFAULT_SIZE,
    REACH,
    text_height,
    text_length,
)

#: The spots offered for a schematic field: clear of its symbol by 0.3 mm,
#: stepped every half grid along each side, at up to two grid steps away.
RULES = Rules(
    gap=0.3,
    clearance=0.25,
    step=0.635,
    pushes=(0.0, 0.635, 1.27, 2.54),
    allow_turn=True,
    max_distance=3.81,
)
#: Cost evaluations allowed for one sheet, so placement always ends.
BUDGET = 400_000

#: A unit's name ends in its unit and body style: ``<name>_<unit>_<style>``.
_UNIT_STYLE = re.compile(r"_(\d+)_(\d+)$")
#: A library pin's angle as a step toward the body, with y growing upward.
_AXES = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}

#: The side of its symbol a field is favoured on.
Prefer = Literal["above", "below", "left", "right"]
_SIDES = {"above": Side.N, "below": Side.S, "left": Side.W, "right": Side.E}


@dataclass(frozen=True, slots=True)
class FieldText:
    """A visible field to place.

    Attributes:
        owner: The key of the symbol it belongs to.
        name: The field's name, such as ``Reference``.
        text: What it says.
        prefer: The side to favour when nothing else decides.
    """

    owner: str
    name: str
    text: str
    prefer: Prefer = "above"


@dataclass(frozen=True, slots=True)
class FieldSpot:
    """Where a field goes.

    Attributes:
        x: The text's centre on the sheet, in mm.
        y: The same, growing downward.
        angle: 0 for upright text, 90 for text turned to read upward.
        box: The room the text takes on the sheet.
    """

    x: float
    y: float
    angle: Literal[0, 90]
    box: Box


@dataclass(frozen=True, slots=True)
class FieldLayout:
    """The fields' positions and how good they are.

    Attributes:
        spots: Each field's spot by owner and name, or ``None`` for a
            field that has no acceptable spot.
        score: The layout's score under the text placer's judge.
        stopped_by_budget: True if the search used its whole budget.
    """

    spots: dict[tuple[str, str], FieldSpot | None]
    score: Score
    stopped_by_budget: bool

    @property
    def unplaced(self) -> list[tuple[str, str]]:
        """The fields with no acceptable spot, as owner and name."""
        return [key for key, spot in self.spots.items() if spot is None]


def place_fields(
    owners: dict[str, Box],
    obstacles: Sequence[Box],
    fields: Sequence[FieldText],
    sheet: Box,
    *,
    budget: int = BUDGET,
) -> FieldLayout:
    """Find a readable spot for every field.

    Args:
        owners: Each symbol's box on the sheet, by key; fields are placed
            around these and none may cover another.
        obstacles: Everything else drawn: wires, labels, power symbols.
        fields: The fields to place.
        sheet: The area text must stay inside.
        budget: The most cost evaluations to spend.

    Returns:
        A spot for each field, or ``None`` where there is no acceptable
        one, with the layout's score.

    Raises:
        ValueError: If a field names an owner that is not given.
    """
    parts = [Part(key, _to_placer(box)) for key, box in owners.items()]
    parts += [
        Part(f"obstacle/{number}", _to_placer(_grown(box, 0.1)))
        for number, box in enumerate(obstacles)
    ]
    labels = tuple(
        Label(
            owner=field.owner,
            text=field.text,
            width=text_length(field.text),
            height=text_height(field.text),
            preferred=_SIDES[field.prefer],
        )
        for field in fields
    )
    problem = Problem(tuple(parts), labels, _to_placer(sheet), RULES)
    # Descent alone: it is the cheapest strategy that the benchmark shows
    # close to the best, and it uses no randomness.
    result = solve(problem, "descent", budget=budget)
    spots: dict[tuple[str, str], FieldSpot | None] = {}
    for field, spot in zip(fields, result.layout, strict=True):
        if spot is None:
            spots[(field.owner, field.name)] = None
            continue
        box = Box(spot.box.x0, -spot.box.y1, spot.box.x1, -spot.box.y0)
        spots[(field.owner, field.name)] = FieldSpot(
            round(spot.box.cx, 4),
            round(-spot.box.cy, 4),
            90 if spot.turned else 0,
            box,
        )
    return FieldLayout(spots, result.score, result.stopped_by_budget)


def label_box(
    text: str,
    at: tuple[float, float],
    step: tuple[int, int],
    *,
    framed: bool = False,
    size: float = DEFAULT_SIZE,
) -> Box:
    """The room a net label takes, running from its anchor along a direction.

    A local label's text sits on its anchor's line, bottom-justified, so
    it rises above a horizontal wire and lies to the left of a vertical
    one (KiCad keeps vertical text reading upward). A global label is
    centred on the line inside a frame.

    Args:
        text: The label's text.
        at: Its anchor on the sheet.
        step: The unit step from the anchor along which the text runs.
        framed: True for a global label.
        size: The text size, in mm.

    Returns:
        The box on the sheet.
    """
    length = text_length(text, size) + (2.5 * size if framed else 0.0)
    x, y = at
    dx, dy = step
    if framed:
        before, after = REACH * size + 0.3, REACH * size + 0.3
    else:
        # Measured for bottom-justified text: the ink reaches 1.47 sizes to
        # the side the text stands on and 0.06 to the other.
        before, after = 1.5 * size, 0.1 * size
    if dx:
        return Box(
            min(x, x + dx * length), y - before, max(x, x + dx * length), y + after
        )
    return Box(x - before, min(y, y + dy * length), x + after, max(y, y + dy * length))


#: How far a pin's text lies from the pin's line, in text sizes: the near
#: and far edges of its ink, with room for KiCad's stroke. Measured on
#: KiCad 10.0.6 from kicad-cli's SVG of twelve reference sheets and two
#: real boards (1,293 numbers, sizes 1.016 and 1.27 mm): text on the
#: upper side spans 0.38 to 1.46 sizes from the line, on the lower side
#: 0.27 to 1.32.
_UPPER = (0.3, 1.55)
_LOWER = (0.2, 1.45)


def pin_text_boxes(
    symbol: SList, placement: Placement, unit: int = 1, body_style: int = 1
) -> list[tuple[str, Box]]:
    """The room taken by the pin numbers and outside pin names KiCad draws.

    KiCad draws them along each pin, centred on it, never inside the box
    `symbol_box` gives the symbol's own shapes. With pin names inside the
    body (a name offset above zero, or names hidden) a number sits on the
    upper side of a horizontal pin, the left of a vertical one. With names
    outside (offset zero) the name takes that side and the number moves to
    the other. A mirrored or turned symbol keeps its text upright, so the
    sides are the sheet's, not the symbol's. All of this was measured, not
    taken from KiCad's code (see `_UPPER`); the one case not seen, names
    hidden at offset zero, is given both sides.

    Args:
        symbol: The symbol as the schematic embeds it.
        placement: Where it is placed.
        unit: The unit drawn.
        body_style: The body style drawn.

    Returns:
        Each drawn text and its box on the sheet.
    """
    names = symbol.first("pin_names")
    offset = 0.508
    names_shown = True
    if names is not None:
        if (node := names.first("offset")) is not None:
            offset = float(node.values()[0])
        names_shown = not _hidden(names)
    numbers = symbol.first("pin_numbers")
    numbers_shown = numbers is None or not _hidden(numbers)
    found: list[tuple[str, Box]] = []
    for child in symbol.lists("symbol"):
        name = child.items[1]
        style = _UNIT_STYLE.search(name.value) if isinstance(name, Atom) else None
        if style is None or int(style.group(1)) not in (0, unit):
            continue
        if int(style.group(2)) not in (0, body_style):
            continue
        for pin in child.lists("pin"):
            if _hidden(pin):
                continue
            at = pin.first("at")
            length = pin.first("length")
            if at is None or length is None:
                continue
            x, y, angle = (float(v) for v in at.values()[:3])
            reach = float(length.values()[0])
            cos, sin = _AXES[int(angle) % 360]
            tip = to_sheet(placement, x, y)
            body = to_sheet(placement, x + cos * reach, y + sin * reach)
            texts: list[tuple[str, float, tuple[float, float]]] = []
            if numbers_shown:
                number, size = _pin_text(pin, "number")
                if offset > 0:
                    sides = [_UPPER]
                elif names_shown:
                    sides = [_LOWER]
                else:
                    sides = [_UPPER, _LOWER]
                texts += [(number, size, side) for side in sides]
            if names_shown and offset == 0:
                text, size = _pin_text(pin, "name")
                texts.append((text, size, _UPPER))
            for text, size, side in texts:
                if text in ("", "~"):
                    continue
                found.append((text, _along_pin(tip, body, text, size, side)))
    return found


def _along_pin(
    tip: tuple[float, float],
    body: tuple[float, float],
    text: str,
    size: float,
    side: tuple[float, float],
) -> Box:
    """A pin text's box, centred along the pin on one side of it."""
    half = text_length(text, size) / 2
    sign = -1 if side is _UPPER else 1
    near, far = (sign * side[0] * size, sign * side[1] * size)
    if tip[1] == body[1]:
        middle, line = (tip[0] + body[0]) / 2, tip[1]
        return Box(
            middle - half, line + min(near, far), middle + half, line + max(near, far)
        )
    # Vertical text reads upward, so its upper side is the sheet's left.
    middle, line = (tip[1] + body[1]) / 2, tip[0]
    return Box(
        line + min(near, far), middle - half, line + max(near, far), middle + half
    )


def _pin_text(pin: SList, head: str) -> tuple[str, float]:
    """A pin's number or name and its text size."""
    node = pin.first(head)
    if node is None:
        return "", DEFAULT_SIZE
    effects = node.first("effects")
    font = effects.first("font") if effects is not None else None
    size = font.first("size") if font is not None else None
    height = float(size.values()[0]) if size is not None else DEFAULT_SIZE
    return node.values()[0], height


def _hidden(node: SList) -> bool:
    hide = node.first("hide")
    return hide is not None and hide.values() == ["yes"]


def _to_placer(box: Box) -> PlacerBox:
    # The placer's y grows upward, the sheet's downward.
    return PlacerBox(box.left, -box.bottom, box.right, -box.top)


def _grown(box: Box, by: float) -> Box:
    return Box(box.left - by, box.top - by, box.right + by, box.bottom + by)
