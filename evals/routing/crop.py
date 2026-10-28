"""Cut a window out of a frozen routing case, for a case that routes in seconds.

    uv run python evals/routing/crop.py SOURCE NAME LEFT TOP RIGHT BOTTOM

The window becomes the board's edge. Only copper wholly inside it is kept;
a net then joins only the pads it has inside, so the small case keeps the
local difficulty (fine-pitch escapes, crowding) without the whole board.
"""

# The router's shapes are read directly here, as its tests do.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import gzip
import json
import sys
from dataclasses import replace
from pathlib import Path

from kairo.pcb.autoroute import RouteCase, case_from_json, case_to_json
from kairo.pcb.autoroute.grid import _Shape
from kairo.pcb.geometry import Rect

HERE = Path(__file__).parent
#: How far inside the window copper must lie: more than the edge keep-off.
MARGIN = 0.5


def load(name: str) -> RouteCase:
    """A frozen case by name, plain or gzipped."""
    path = HERE / "cases" / f"{name}.json"
    raw = (
        path.read_bytes()
        if path.exists()
        else gzip.decompress(path.with_suffix(".json.gz").read_bytes())
    )
    return case_from_json(json.loads(raw.decode("utf-8")))


def crop(case: RouteCase, window: Rect) -> RouteCase:
    """The case inside a window: its copper there, the window as the edge."""
    left, top, right, bottom = window

    def inside(points: list[tuple[float, float]]) -> bool:
        return all(
            left + MARGIN <= x <= right - MARGIN
            and top + MARGIN <= y <= bottom - MARGIN
            for x, y in points
        )

    kept: list[_Shape] = []
    for shape in case.shapes:
        if shape.box is not None:
            x0, y0, x1, y1 = shape.box
            points = [(x0, y0), (x1, y1), (x0, y1), (x1, y0)]
        elif shape.polygon is not None:
            points = list(shape.polygon)
        else:
            assert shape.start is not None and shape.end is not None
            points = [shape.start, shape.end]
        if inside(points):
            kept.append(shape)
    edge = ((left, top), (right, top), (right, bottom), (left, bottom))
    return replace(case, shapes=tuple(kept), outline=edge)


def main() -> None:
    """Write the cropped case beside the others."""
    source, name, *box = sys.argv[1:]
    left, top, right, bottom = (float(v) for v in box)
    small = crop(load(source), (left, top, right, bottom))
    out = HERE / "cases" / f"{name}.json"
    out.write_text(
        json.dumps(case_to_json(small), separators=(",", ":")), encoding="utf-8"
    )
    print(out, len(small.shapes), "shapes")


if __name__ == "__main__":
    main()
