"""Freeze a KiCad board someone else drew as a routing case, its tracks taken up.

    uv run python evals/routing/import_board.py BOARD.kicad_pcb NAME

Each footprint's pads, as the board file places them, become the copper
the router starts from, each on its pad's net; the board's own tracks,
vias and pours are left out, so every net is routed from its pads alone.
That is harder than the designer had it where a pour joined a net. The
case is saved as cases/NAME.json, to replay without KiCad, and the
board's own routing is printed beside it for comparison.

Read from KiCad's board format as KiCad 8 and 10 save it: a pad's ``at``
is about its footprint's origin, turned with the footprint, and its angle
is the pad's own on the board; a footprint on the back stores its pads
already mirrored; a pad's net is the last text in its ``net`` list. The
routing classes are the board's own, from the project file beside it
(each class's clearance and track width, its nets by the project's name
patterns, the Default class's via); with no project, KiCad's defaults. A
pad turned other than by a quarter turn is taken as the box around it,
which is larger than it is. The board's edge is taken as the box around
its Edge.Cuts drawing, so a notch or a round corner is not kept out of.
"""

# The router's copper shapes are built directly here, as crop.py does.
# pyright: reportPrivateUsage=false
# A project file is plain JSON, which a strict type checker cannot follow.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import fnmatch
import json
import math
import sys
from pathlib import Path

from kairo.kicad.sexpr import Atom, SList, parse
from kairo.pcb.autoroute import RouteCase, case_to_json
from kairo.pcb.autoroute.grid import _Shape
from kairo.pcb.geometry import rotate
from kairo.pcb.specctra import NetClass

HERE = Path(__file__).parent


def _text(node: SList, index: int = 0) -> str:
    values = node.values()
    return str(values[index]) if len(values) > index else ""


def _box(
    cx: float, cy: float, w: float, h: float, angle: float
) -> tuple[float, float, float, float]:
    """The box around a w by h rectangle at a centre, turned by an angle."""
    r = math.radians(angle)
    hw = (abs(w * math.cos(r)) + abs(h * math.sin(r))) / 2
    hh = (abs(w * math.sin(r)) + abs(h * math.cos(r))) / 2
    return (cx - hw, cy - hh, cx + hw, cy + hh)


def classes_of(
    project: dict[str, object] | None, nets: list[str]
) -> tuple[tuple[NetClass, ...], tuple[float, float]]:
    """The project's routing classes for these nets, and the Default via."""
    settings = project.get("net_settings", {}) if project else {}
    assert isinstance(settings, dict)
    listed = settings.get("classes", []) or [{"name": "Default"}]
    patterns = settings.get("netclass_patterns", []) or []
    sizes = {
        str(c["name"]): (
            float(c.get("track_width") or 0.2),
            float(c.get("clearance") or 0.2),
            float(c.get("via_diameter") or 0.6),
            float(c.get("via_drill") or 0.3),
        )
        for c in listed
    }
    sizes.setdefault("Default", (0.2, 0.2, 0.6, 0.3))
    members: dict[str, list[str]] = {name: [] for name in sizes}
    for net in nets:
        chosen = next(
            (
                str(p["netclass"])
                for p in patterns
                if str(p.get("netclass")) in sizes
                and fnmatch.fnmatchcase(net, str(p.get("pattern", "")))
            ),
            "Default",
        )
        members[chosen].append(net)
    classes = tuple(
        NetClass(name, width, clearance, tuple(members[name]))
        for name, (width, clearance, _, _) in sizes.items()
        if members[name] or name == "Default"
    )
    default = sizes["Default"]
    return classes, (default[2], default[3])


def read_board(
    text: str, project: dict[str, object] | None = None
) -> tuple[RouteCase, dict[str, float]]:
    """The routing case of a board, and the board's own routing in numbers."""
    root = parse(text).root
    # Each layer is (number "name" type ...), the number its head: the
    # copper ones end in .Cu.
    table = root.first("layers")
    copper = [
        _text(entry, 0)
        for entry in (table.lists() if table is not None else [])
        if _text(entry, 0).endswith(".Cu")
    ]
    shapes: list[_Shape] = []
    for footprint in root.lists("footprint"):
        at = footprint.first("at")
        if at is None:
            continue
        values = [float(v) for v in at.values()]
        x, y = values[0], values[1]
        turn = values[2] if len(values) > 2 else 0.0
        for pad in footprint.lists("pad"):
            kind = str(pad.items[2].value) if isinstance(pad.items[2], Atom) else ""
            place, size = pad.first("at"), pad.first("size")
            if place is None or size is None or kind == "np_thru_hole":
                continue
            p = [float(v) for v in place.values()]
            angle = p[2] if len(p) > 2 else turn
            w, h = (float(v) for v in size.values()[:2])
            dx, dy = rotate(p[0], p[1], turn)
            listed = pad.first("layers")
            names = [str(v) for v in listed.values()] if listed is not None else []
            if kind == "thru_hole" or "*.Cu" in names:
                layer = "*"
            else:
                layer = next((n for n in names if n.endswith(".Cu")), "")
            if not layer:
                continue
            net = pad.first("net")
            name = str(net.values()[-1]) if net is not None and net.values() else ""
            shapes.append(_Shape(name, layer, box=_box(x + dx, y + dy, w, h, angle)))
    edge: list[tuple[float, float]] = []
    for item in root.lists():
        layer = item.first("layer")
        if layer is None or _text(layer) != "Edge.Cuts":
            continue
        for key in ("start", "end", "mid", "center"):
            point = item.first(key)
            if point is not None:
                edge.append((float(point.values()[0]), float(point.values()[1])))
        pts = item.first("pts")
        if pts is not None:
            edge += [
                (float(xy.values()[0]), float(xy.values()[1])) for xy in pts.lists("xy")
            ]
    if not edge:
        raise ValueError("the board has no Edge.Cuts drawing")
    xs, ys = [p[0] for p in edge], [p[1] for p in edge]
    outline = (
        (min(xs), min(ys)),
        (max(xs), min(ys)),
        (max(xs), max(ys)),
        (min(xs), max(ys)),
    )
    nets = sorted({s.net for s in shapes if s.net})
    tracks = [s for s in root.lists("segment")]
    length = sum(
        math.dist(
            (float(t.first("start").values()[0]), float(t.first("start").values()[1])),  # type: ignore[union-attr]
            (float(t.first("end").values()[0]), float(t.first("end").values()[1])),  # type: ignore[union-attr]
        )
        for t in tracks
    )
    own = {
        "track_mm": round(length, 1),
        "vias": len(root.lists("via")),
        "zones": len(root.lists("zone")),
    }
    classes, via = classes_of(project, nets)
    case = RouteCase(tuple(shapes), outline, tuple(copper), classes, frozenset(), via)
    return case, own


def project_of(board: Path) -> dict[str, object] | None:
    """The project file beside a board, if there is one."""
    path = board.with_suffix(".kicad_pro")
    if not path.is_file():
        return None
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def main() -> None:
    """Freeze the board named on the command line."""
    board, name = Path(sys.argv[1]), sys.argv[2]
    case, own = read_board(board.read_text(encoding="utf-8"), project_of(board))
    out = HERE / "cases" / f"{name}.json"
    out.write_text(json.dumps(case_to_json(case)), encoding="utf-8")
    pads = len(case.shapes)
    nets = len({s.net for s in case.shapes if s.net})
    print(
        f"{out}: {pads} pads, {nets} nets, {len(case.layers)} layers; "
        f"the board's own: {own}"
    )


if __name__ == "__main__":
    main()
