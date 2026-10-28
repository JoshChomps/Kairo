"""Where each part's reference prints: clear of copper and of other print.

A footprint's library puts its reference where it suits the part alone;
on a packed board those spots land on pads, on a neighbour's outline and
on each other (about 300 of KiCad's silkscreen findings on the BMS). A
designer moves each one to a clear spot beside its part, or leaves it off
the silkscreen where there is none. This does the same, greedily, the
largest parts first: the part's centre where its body has no pads, then
above, below, left and right of its courtyard, at the full text size and
then a smaller one; a label that fits nowhere moves to the fabrication
layer, where the assembly drawing still shows it.

A label's box is estimated from KiCad's stroke font: each character about
`CHAR_WIDTH` of the text size wide, and the box `LINE_HEIGHT` of it tall.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kairo.pcb.geometry import Rect, rotate
from kairo.pcb.place import Outline, placed_pads, turned

#: A character's advance in KiCad's stroke font, as a share of the size.
CHAR_WIDTH = 0.9
#: A line's box height, as a share of the size (the stroke's width added).
LINE_HEIGHT = 1.3
#: The text sizes tried, largest first, in mm. JLCPCB prints down to 0.8.
SIZES = (1.0, 0.8)
#: Room kept between a label and anything else, in mm.
MARGIN = 0.15
#: How far from its courtyard a label beside a part may stand, in mm.
OFFSET = 0.2


@dataclass(frozen=True, slots=True)
class Label:
    """A reference's place: its centre and angle on the board, and its size.

    Attributes:
        x: Its centre's x on the board, in mm.
        y: Its centre's y, in mm.
        angle: Its angle on the board: 0 reads left to right, 90 upward.
        size: Its text height, in mm.
        printed: False where it goes to the fabrication layer instead.
    """

    x: float
    y: float
    angle: float
    size: float
    printed: bool = True


def labels(
    outlines: Mapping[str, Outline],
    places: Mapping[str, tuple[float, float, float]],
    board: Rect,
    edge: float = 0.5,
) -> dict[str, Label]:
    """A place for each part's reference on the top silkscreen.

    Args:
        outlines: Each part's footprint outline, by reference.
        places: Each part's x, y and turn on the board.
        board: The board's edge, as left, top, right and bottom, in mm.
        edge: The least distance from a label to the board's edge, in mm.

    Returns:
        Each part's label, by reference.
    """
    taken: list[Rect] = []
    bodies: dict[str, Rect] = {}
    for ref, outline in outlines.items():
        if ref not in places:
            continue
        x, y, turn = places[ref]
        # Copper a label may not print on: the top pads, and through pads.
        taken += [
            _grow(box, MARGIN)
            for _, box, layer in placed_pads(outline, x, y, turn)
            if layer in ("F.Cu", "all")
        ]
        taken += [_grow(_moved(box, x, y, turn), MARGIN) for box in outline.silk]
        left, top, right, bottom = turned(outline.courtyard, turn)
        bodies[ref] = (x + left, y + top, x + right, y + bottom)
    inner = (board[0] + edge, board[1] + edge, board[2] - edge, board[3] - edge)
    out: dict[str, Label] = {}
    order = sorted(bodies, key=lambda r: -_area(bodies[r]))
    for ref in order:
        body = bodies[ref]
        x, y, turn = places[ref]
        pads = [box for _, box, _ in placed_pads(outlines[ref], x, y, turn)]
        found = None
        for size in SIZES:
            for spot in _spots(ref, body, size, pads):
                box = _box(spot, ref, size)
                if _within(box, inner) and not any(_hits(box, t) for t in taken):
                    found = Label(spot[0], spot[1], spot[2], size)
                    break
            if found is not None:
                break
        if found is None:
            cx, cy = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
            found = Label(cx, cy, 0.0, SIZES[-1], printed=False)
        else:
            taken.append(
                _grow(_box((found.x, found.y, found.angle), ref, found.size), MARGIN)
            )
        out[ref] = found
    return out


def local(label: Label, x: float, y: float, turn: float) -> tuple[float, float]:
    """A label's centre in its footprint's own frame, as the board file keeps it."""
    return rotate(label.x - x, label.y - y, -turn)


def _spots(
    ref: str, body: Rect, size: float, pads: Sequence[Rect]
) -> list[tuple[float, float, float]]:
    """The places tried for a label, best first: centre, above, below, left, right."""
    left, top, right, bottom = body
    cx, cy = (left + right) / 2, (top + bottom) / 2
    w, h = len(ref) * size * CHAR_WIDTH, size * LINE_HEIGHT
    out: list[tuple[float, float, float]] = []
    # Over the part's body, where it is wide enough and has no pad there.
    if right - left >= w + 2 * MARGIN and bottom - top >= h + 2 * MARGIN:
        centre = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        if not any(_hits(_grow(centre, MARGIN), p) for p in pads):
            out.append((cx, cy, 0.0))
    gap = OFFSET + h / 2
    out += [
        (cx, top - gap, 0.0),
        (cx, bottom + gap, 0.0),
        (left - OFFSET - h / 2, cy, 90.0),
        (right + OFFSET + h / 2, cy, 90.0),
    ]
    return out


def _box(spot: tuple[float, float, float], ref: str, size: float) -> Rect:
    x, y, angle = spot
    w, h = len(ref) * size * CHAR_WIDTH, size * LINE_HEIGHT
    if round(angle) % 180 == 90:
        w, h = h, w
    return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def _moved(box: Rect, x: float, y: float, turn: float) -> Rect:
    left, top, right, bottom = turned(box, turn)
    return (x + left, y + top, x + right, y + bottom)


def _grow(box: Rect, by: float) -> Rect:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _hits(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _within(box: Rect, inner: Rect) -> bool:
    return (
        box[0] >= inner[0]
        and box[1] >= inner[1]
        and box[2] <= inner[2]
        and box[3] <= inner[3]
    )


def _area(box: Rect) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])
