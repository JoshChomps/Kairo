"""From a built schematic to a placed, poured board that KiCad has checked.

The steps, each from a source KiCad itself trusts:

1. KiCad's netlist of the project's schematic (``kicad-cli sch export
   netlist``) gives every part, footprint, field and pad net, as KiCad's
   own update from the schematic would (`kairo.pcb.assemble`).
2. The design's board constraints place the parts (`kairo.pcb.place`).
3. Each pour becomes zones: a plane where it has a layer to itself, a
   share of the layer by nearest pad where it does not (`kairo.pcb.zones`).
4. The board is written (`kairo.pcb.board_writer`) into a scratch copy of
   the project, where KiCad fills its zones and saves it, then runs its
   DRC with the schematic parity check; that report is the verdict.

The filled board is what the caller writes into the project, through the
transaction module like every other project file.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from kairo.calc import Given, calculate
from kairo.ir import Board, Design
from kairo.kicad.cli import KicadCli, NetlistStatus
from kairo.kicad.netlist import Netlist, parse_netlist
from kairo.kicad.project import parse_project
from kairo.library.index import LibraryIndex
from kairo.pcb.assemble import parts_from_netlist
from kairo.pcb.board_writer import (
    OUNCE,
    BoardError,
    PlacedPart,
    Stackup,
    Track,
    Via,
    Zone,
    write_board,
)
from kairo.pcb.fanout import Fanout, fan_out
from kairo.pcb.geometry import Point, Rect, point_segment, segment_segment
from kairo.pcb.place import (
    Item,
    PlacementError,
    place,
    placed_pads,
    placement_report,
    read_outline,
    rotate,
    strays,
    turned,
)
from kairo.pcb.rules import (
    apply_rules,
    net_volts,
    pair_rules,
    plain,
    routing_classes,
    unstated,
    via_of,
)
from kairo.pcb.silk import labels, local
from kairo.pcb.specctra import NetClass, keepout_polygons, net_gaps
from kairo.pcb.zones import (
    filled_islands,
    island_stitches,
    plane,
    shared,
    stitching,
)
from kairo.process import run_process
from kairo.units import Quantity

#: A router: from the placed parts, the board's outline, its pours and its
#: layers, the tracks and vias that join the rest.
Router = Callable[
    [
        Sequence[PlacedPart],
        Sequence[tuple[float, float]],
        Sequence[Zone],
        Stackup,
        Sequence[Via],
        Sequence[Track],
        Sequence[NetClass],
    ],
    tuple[list[Track], list[Via]],
]
#: Room kept around a power part's courtyard, in mm: its pours and their
#: stitching vias run between it and its neighbours.
POWER_MARGIN = 1.5
#: Where the board's corner sits on the drawing sheet, in mm.
ORIGIN = (30.0, 30.0)
#: A net's weight in placement: a pour's pads are joined by copper areas,
#: so their spread costs less than a track's.
POUR_WEIGHT = 0.3
#: A plane's net costs least: every pad reaches it straight down a via.
PLANE_WEIGHT = 0.05


class LayoutError(ValueError):
    """The board cannot be laid out as the design asks."""


@dataclass(frozen=True, slots=True)
class Laid:
    """A board laid out and judged.

    Attributes:
        text: The board file, its zones filled by KiCad.
        places: Each part's x, y and turn on the sheet, in mm and degrees.
        violations: KiCad's DRC violations by kind.
        unconnected: How many connections KiCad found not yet made.
        parity: KiCad's schematic parity issues by kind.
        report: KiCad's DRC report, as it wrote it.
        project: The project file with the board's classes and rules, as
            the DRC read it; empty if the board states none.
        fanned_out: Pads of a plane's net the fanout found no room beside.
        placement: How near each local part sits to the IC pin it serves
            (`kairo.pcb.place.placement_report`).
        narrowed: The nets of a class wider than the narrowest drawn
            narrower than their class somewhere (`narrowed`).
        pair_rules: KiCad's custom rules for the pairs of classes that need
            more than either (``.kicad_dru``); empty if none.
        unstated: The nets the board's voltage spacing could not be checked
            for: the design states no voltage for them.
        current: For each narrowed net, whether its narrowest copper carries
            its stated current (`current_checks`).
        detours: The routed nets that wander furthest from their pads'
            span (`detours`): a connection forced through is made, but
            not well.
        blocks: Parts that serve only an IC of another block
            (`block_check`), with what to do.
        mended: The missing connections the last step tried to mend
            (``tried``) and those it joined (``joined``), by net.
    """

    text: str
    places: dict[str, tuple[float, float, float]]
    violations: dict[str, int]
    unconnected: int
    parity: dict[str, int]
    report: dict[str, object] = field(default_factory=dict[str, object])
    project: str = ""
    fanned_out: list[str] = field(default_factory=list[str])
    placement: dict[str, object] = field(default_factory=dict[str, object])
    narrowed: dict[str, dict[str, float]] = field(
        default_factory=dict[str, dict[str, float]]
    )
    pair_rules: str = ""
    unstated: list[str] = field(default_factory=list[str])
    current: dict[str, dict[str, object]] = field(
        default_factory=dict[str, dict[str, object]]
    )
    detours: dict[str, dict[str, float]] = field(
        default_factory=dict[str, dict[str, float]]
    )
    blocks: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    mended: dict[str, object] = field(default_factory=dict[str, object])


def netlist_of(cli: KicadCli, schematic: Path) -> Netlist:
    """KiCad's netlist of a project's root sheet."""
    exported = cli.export_netlist(schematic)
    if exported.status is not NetlistStatus.OK or exported.text is None:
        raise LayoutError(f"KiCad could not export the netlist: {exported.reason}")
    return parse_netlist(exported.text)


def lay_out_board(
    design: Design,
    project: Path,
    index: LibraryIndex,
    cli: KicadCli,
    *,
    tracks: Sequence[Track] = (),
    vias: Sequence[Via] = (),
    effort: float = 1.0,
    places: Mapping[str, tuple[float, float, float]] | None = None,
    router: Router | None = None,
    rejoin: Router | None = None,
    guide: Callable[[str, Sequence[str]], Router] | None = None,
    mend: Callable[[Collection[str]], Router] | None = None,
) -> Laid:
    """Place a built project's parts, pour its power nets and have KiCad judge it.

    Args:
        design: The design, with its ``board`` constraints.
        project: The project's folder, its schematic already built.
        index: The library index.
        cli: kicad-cli, which fills the zones and runs the DRC.
        tracks: Copper runs to add, such as a router's.
        vias: Vias to add.
        effort: A multiplier on the placer's moves.
        places: Parts' places from an earlier layout, kept as they were.
        router: Routes the placed board's other nets (`kairo.pcb.route`);
            its tracks and vias join any given.
        rejoin: Routes what is still apart once the pours are filled and
            their islands stitched, given those islands as copper; a
            shorter run than ``router``'s.
        guide: A router for one net on the layers named (all if none),
            for the board's ``routes``: each is routed in turn before
            ``router``, and its copper is then fixed for the rest.
        mend: A router for some nets, given the filled pours' islands as
            copper: for each connection still missing at the end, the
            router's tracks round it are taken up, its net routed, and
            theirs joined again (`take_up`); kept where fewer connections
            are left.

    Returns:
        The filled board and KiCad's verdict.

    Raises:
        LayoutError: If the design has no board, or a step fails.
    """
    board = design.board
    if board is None:
        raise LayoutError("the design states no board: give it board.width and height")
    root = project / f"{design.design}.kicad_sch"
    netlist = netlist_of(cli, root)
    width, height = board.width.value, board.height.value
    if places is None:
        places = _place(design, netlist, index, effort)
    ox, oy = ORIGIN
    stackup = Stackup(
        board.copper_layers, board.outer_oz, board.inner_oz, board.thickness.value
    )
    zones = _pours(design, netlist, index, places, stackup.copper)
    parts = parts_from_netlist(netlist, places, root.name)
    outlines = {
        c.ref: read_outline(index, c.footprint)
        for c in netlist.components
        if c.footprint and c.ref in places
    }
    marks = labels(outlines, places, (ox, oy, ox + width, oy + height))
    parts = [
        replace(
            p,
            label=(
                *local(marks[p.ref], p.x, p.y, p.rotation),
                marks[p.ref].angle,
                marks[p.ref].size,
                marks[p.ref].printed,
            ),
        )
        if p.ref in marks
        else p
        for p in parts
    ]
    outline = [(ox, oy), (ox + width, oy), (ox + width, oy + height), (ox, oy + height)]
    net_names = [net.name for net in netlist.nets]
    classes = routing_classes(board, net_names, net_volts(design))
    gap = net_gaps(classes)
    stitched = stitching(
        zones,
        _courtyards(netlist, index, places),
        pads=_pad_boxes(netlist, index, places),
        gap=gap,
    )
    keepouts = [
        (
            min(x for x, _ in c),
            min(y for _, y in c),
            max(x for x, _ in c),
            max(y for _, y in c),
        )
        for _, c in keepout_polygons(index, parts)
    ]
    fanned = _fan_out(design, netlist, index, places, zones, stitched, gap, keepouts)
    fixed_vias = [*stitched, *fanned.vias]
    vias = [*vias, *fixed_vias]
    tracks = [*tracks, *fanned.tracks]
    # The router's own copper, which it may neck; the fanout's via stubs
    # are drawn narrow on purpose.
    drawn: list[Track] = []
    bored: list[Via] = []
    if board.routes and guide is None:
        raise LayoutError(
            "the board names routes to take first, and only Kairo's own "
            "router takes them: make the board with engine 'kairo'"
        )
    first = _guided(design, net_names, stackup.copper) if router is not None else []
    held = list(fanned.tracks)
    for net, layers in first:
        assert guide is not None
        routed, drilled = guide(net, layers)(
            parts, outline, zones, stackup, fixed_vias, held, classes
        )
        held += routed
        drawn += routed
        bored += drilled
        tracks = [*tracks, *routed]
        fixed_vias = [*fixed_vias, *drilled]
        vias = [*vias, *drilled]
    if router is not None:
        routed, drilled = router(
            parts, outline, zones, stackup, fixed_vias, held, classes
        )
        tracks = [*tracks, *routed]
        drawn += routed
        bored += drilled
        vias = [*vias, *drilled]

    def written(tracks: Sequence[Track], vias: Sequence[Via]) -> str:
        stated = design.title_block
        vias = unique_vias(vias)
        try:
            return write_board(
                design.design,
                parts,
                outline,
                index,
                stackup=stackup,
                tracks=tracks,
                vias=vias,
                zones=zones,
                title={
                    "title": design.design,
                    "date": stated.date,
                    "rev": stated.revision,
                    "company": stated.company,
                },
            )
        except BoardError as exc:
            raise LayoutError(str(exc)) from exc

    laid = judge(design, project, written(tracks, vias), cli, dict(places), net_names)
    shared_nets = sorted(
        {z.net for z in zones if len({y.net for y in zones if y.layer == z.layer}) > 1}
    )
    if shared_nets and laid.unconnected:
        poured = sorted({z.layer for z in zones if z.net in shared_nets})
        stitches = island_stitches(
            laid.text,
            shared_nets,
            poured,
            through=_through_pads(netlist, index, places),
            **_via_size(board),
        )
        if stitches:
            again = judge(
                design,
                project,
                written(tracks, [*vias, *stitches]),
                cli,
                dict(places),
                net_names,
            )
            if again.unconnected < laid.unconnected:
                laid, vias = again, [*vias, *stitches]
    router = rejoin
    if router is not None and laid.unconnected:
        # The second stage: the router was given the pours as planes and
        # took their nets as joined; KiCad's fill may have left islands.
        # It is given the islands as they filled, with everything routed
        # so far kept, and joins what is still apart.
        joined, drilled = router(
            parts,
            outline,
            filled_islands(laid.text),
            stackup,
            list(vias),
            list(tracks),
            classes,
        )
        if joined or drilled:
            again = judge(
                design,
                project,
                written([*tracks, *joined], [*vias, *drilled]),
                cli,
                dict(places),
                net_names,
            )
            if again.unconnected < laid.unconnected:
                laid, drawn = again, [*drawn, *joined]
                bored += drilled
                tracks, vias = [*tracks, *joined], [*vias, *drilled]
    mended: dict[str, object] = {"tried": [], "joined": []}
    for net, a, b in gaps(laid.report)[:FEW_GAPS] if mend is not None else []:
        assert mend is not None
        cast("list[str]", mended["tried"]).append(net)
        for reach in MEND_REACH:
            kept, ripped, kept_bored, gone = take_up(drawn, bored, net, a, b, reach)
            rest: list[Track] = [t for t in tracks if id(t) not in ripped]
            standing: list[Via] = [v for v in vias if id(v) not in gone]
            islands = filled_islands(laid.text)
            first, drilled = mend({net})(
                parts, outline, islands, stackup, standing, rest, classes
            )
            others = {t.net for t in drawn if id(t) in ripped}
            joined: list[Track] = []
            more: list[Via] = []
            if others:
                joined, more = mend(others)(
                    parts,
                    outline,
                    islands,
                    stackup,
                    [*standing, *drilled],
                    [*rest, *first],
                    classes,
                )
            again = judge(
                design,
                project,
                written([*rest, *first, *joined], [*standing, *drilled, *more]),
                cli,
                dict(places),
                net_names,
            )
            if again.unconnected < laid.unconnected:
                laid = again
                drawn = [*kept, *first, *joined]
                bored = [*kept_bored, *drilled, *more]
                tracks = [*rest, *first, *joined]
                vias = [*standing, *drilled, *more]
                cast("list[str]", mended["joined"]).append(net)
                break
    return replace(
        laid,
        fanned_out=fanned.left,
        placement=_placement(design, netlist, index, places),
        narrowed=narrowed(drawn, classes),
        current=current_checks(narrowed(drawn, classes), design),
        detours=detours(drawn, bored, _pad_boxes(netlist, index, places)),
        blocks=block_check(design, netlist, index),
        mended=mended,
        unstated=unstated(board, net_names, net_volts(design)),
    )


def current_checks(
    narrow: Mapping[str, Mapping[str, float]], design: Design
) -> dict[str, dict[str, object]]:
    """Whether each narrowed net's narrowest copper carries the net's current.

    By IPC-2221's chart (`kairo.calc`, ``trace_current_external`` or
    ``internal``) at the narrowest width, the layer's copper weight and
    the board's allowed rise. A net whose current the design does not
    state is ``unverified``, with what to state; a short neck carries more
    than the chart says, since its copper sheds heat along the wider
    track either side, so a pass here is cautious.

    Returns:
        For each narrowed net: ``current`` (stated, A, or None),
        ``carries`` (A), and ``verdict``: ``pass``, ``fail`` or
        ``unverified``.
    """
    board = design.board
    assert board is not None
    out: dict[str, dict[str, object]] = {}
    for net, entry in narrow.items():
        outer = bool(entry.get("outer", 1.0))
        oz = board.outer_oz if outer else board.inner_oz
        result = calculate(
            "trace_current_external" if outer else "trace_current_internal",
            {
                "width": Given("W", Quantity(entry["least"], "mm"), f"net {net}"),
                "thickness": Given(
                    "T", Quantity(round(oz * OUNCE, 4), "mm"), f"{oz:g} oz copper"
                ),
                "temp_rise": Given(
                    "dT", Quantity(board.trace_temp_rise, "degC"), "board"
                ),
            },
        )
        carries = result.value.value if result.value is not None else 0.0
        stated = design.nets.get(plain(net))
        current = stated.current.value if stated and stated.current else None
        verdict = (
            "unverified"
            if current is None
            else "pass"
            if carries >= current
            else "fail"
        )
        out[net] = {
            "current": current,
            "carries": round(carries, 3),
            "verdict": verdict,
        }
    return out


#: A routed net listed as a detour: its copper this many times its pads'
#: span, and at least `DETOUR_MM` long.
DETOUR = 2.5
DETOUR_MM = 10.0
#: Nets with more pads than this are trees, longer than their span by
#: nature, and are not judged so.
DETOUR_PADS = 3


def detours(
    tracks: Sequence[Track],
    vias: Sequence[Via],
    pads: Sequence[tuple[str, Rect]],
) -> dict[str, dict[str, float]]:
    """The routed nets whose copper runs much longer than their pads lie apart.

    A net of two or three pads whose tracks add up to `DETOUR` times the
    half-perimeter of its pads' centres (and at least `DETOUR_MM`) went
    the long way round: joined, but not as a designer would join it.

    Args:
        tracks: The router's tracks.
        vias: The router's vias.
        pads: Every pad's net and box.

    Returns:
        For each such net, longest first: ``mm`` of track, ``span_mm`` and
        ``vias``.
    """
    centres: dict[str, list[tuple[float, float]]] = {}
    for net, box in pads:
        centres.setdefault(net, []).append(
            ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        )
    length: dict[str, float] = {}
    for tr in tracks:
        length[tr.net] = length.get(tr.net, 0.0) + math.dist(tr.start, tr.end)
    drilled = Counter(v.net for v in vias)
    out: dict[str, dict[str, float]] = {}
    for net, mm in length.items():
        spots = centres.get(net, [])
        if not 2 <= len(spots) <= DETOUR_PADS:
            continue
        span = (max(x for x, _ in spots) - min(x for x, _ in spots)) + (
            max(y for _, y in spots) - min(y for _, y in spots)
        )
        if mm >= DETOUR_MM and mm >= DETOUR * max(span, 1.0):
            out[net] = {
                "mm": round(mm, 1),
                "span_mm": round(span, 1),
                "vias": float(drilled.get(net, 0)),
            }
    return dict(
        sorted(out.items(), key=lambda kv: -kv[1]["mm"] / max(kv[1]["span_mm"], 1.0))
    )


def narrowed(
    tracks: Sequence[Track], classes: Sequence[NetClass]
) -> dict[str, dict[str, float]]:
    """The nets drawn narrower than their class, where the class is wide.

    A class wider than the narrowest is wide for its current; the router
    necks a track to fit past a pin or a corner (`kairo.pcb.autoroute`).
    A short neck at a pin is usual, but how much current the narrowed
    copper carries needs the net's current, which the design does not
    state, so each such net is reported for a person to check.

    Returns:
        For each such net: its class width, the narrowest drawn, the
        length drawn narrower and the longest single track of it, in mm,
        and whether its narrowest is on an outer layer (1) or not (0).
    """
    narrowest = min((c.width for c in classes), default=0.0)
    width_of = {n: c.width for c in classes if c.width > narrowest for n in c.nets}
    out: dict[str, dict[str, float]] = {}
    for tr in tracks:
        wide = width_of.get(tr.net)
        if wide is None or tr.width >= wide - 1e-6:
            continue
        entry = out.setdefault(
            tr.net,
            {"width": wide, "least": tr.width, "mm": 0.0, "longest": 0.0, "outer": 1.0},
        )
        length = math.dist(tr.start, tr.end)
        if tr.width <= entry["least"]:
            entry["outer"] = 1.0 if tr.layer in ("F.Cu", "B.Cu") else 0.0
        entry["least"] = min(entry["least"], tr.width)
        entry["mm"] = round(entry["mm"] + length, 2)
        entry["longest"] = max(entry["longest"], round(length, 2))
    return out


def judge(
    design: Design,
    project: Path,
    text: str,
    cli: KicadCli,
    places: dict[str, tuple[float, float, float]],
    nets: Sequence[str] = (),
) -> Laid:
    """Have KiCad fill a board's zones and check it, in a scratch copy.

    The copy's project file is given the board's classes and its maker's
    rules first, so the DRC checks the board against them.
    """
    name = f"{design.design}.kicad_pcb"
    rules_text = ""
    pair_text = ""
    with tempfile.TemporaryDirectory(prefix="kairo-board-") as scratch:
        copy = Path(scratch) / project.name
        shutil.copytree(project, copy, ignore=shutil.ignore_patterns("kairo", "*.lck"))
        board = copy / name
        board.write_bytes(text.encode("utf-8"))
        pro = copy / f"{design.design}.kicad_pro"
        assert design.board is not None
        if pro.is_file() and (design.board.net_classes or design.board.rules):
            settings = parse_project(pro.read_bytes().decode("utf-8"))
            classes = routing_classes(design.board, nets, net_volts(design))
            apply_rules(settings.data, design.board, nets, classes)
            rules_text = settings.to_text()
            pro.write_bytes(rules_text.encode("utf-8"))
            # Pairs that need more than either class (the voltage between
            # them) are custom rules, which KiCad reads beside the project.
            pair_text = pair_rules(classes) if any(c.apart for c in classes) else ""
            dru = copy / f"{design.design}.kicad_dru"
            if pair_text:
                dru.write_text(pair_text, encoding="utf-8")
            elif dru.is_file():
                dru.unlink()
        report_file = copy / "drc.json"
        run = run_process(
            [
                *cli.command,
                "pcb",
                "drc",
                "--schematic-parity",
                "--refill-zones",
                "--save-board",
                "--severity-all",
                "--format",
                "json",
                "-o",
                str(report_file),
                str(board),
            ],
            timeout_s=600,
        )
        if not report_file.is_file():
            raise LayoutError(f"KiCad's DRC wrote no report: {run.stderr[-500:]}")
        report = json.loads(report_file.read_text(encoding="utf-8"))
        filled = board.read_bytes().decode("utf-8")
    violations = Counter(str(v["type"]) for v in report.get("violations", []))
    parity = Counter(str(v["type"]) for v in report.get("schematic_parity", []))
    return Laid(
        filled,
        places,
        dict(violations),
        len(report.get("unconnected_items", [])),
        dict(parity),
        report,
        rules_text,
        pair_rules=pair_text,
    )


def _items(design: Design, netlist: Netlist, index: LibraryIndex) -> list[Item]:
    """Every part with a footprint, as the placer takes it."""
    board = design.board
    assert board is not None
    block_of = {ref: block for block, refs in design.blocks.items() for ref in refs}
    pads = _pad_nets(netlist)
    powered = {block for pour in board.pours for block in pour.blocks}
    items: list[Item] = []
    for component in netlist.components:
        if not component.footprint:
            continue
        block = block_of.get(component.ref, "")
        items.append(
            Item(
                component.ref,
                read_outline(index, component.footprint),
                block,
                board.sides.get(block, "center"),
                board.edges.get(component.ref),
                pads.get(component.ref, {}),
                POWER_MARGIN if block in powered else None,
            )
        )
    return items


def block_check(
    design: Design, netlist: Netlist, index: LibraryIndex
) -> list[dict[str, str]]:
    """The parts that serve only an IC of another block, each with what to do.

    Kairo seats a block's local parts at its IC's pins (`kairo.pcb.place`);
    a part whose every net is one IC's, placed in another block, is placed
    with that block instead, and its tracks cross the board (`strays`).

    Returns:
        For each: ``part``, ``block``, ``serves`` (the IC and its block)
        and ``fix``.
    """
    items = _items(design, netlist, index)
    roles = {name: net.role for name, net in design.nets.items()}
    rails = {
        net.name
        for net in netlist.nets
        if roles.get(plain(net.name)) in ("power", "ground")
    }
    return [
        {
            "part": s.ref,
            "block": s.block or "(none)",
            "serves": f"{s.ic} ({s.ic_block or 'no block'})",
            "fix": (
                f"move {s.ref} into block {s.ic_block!r} with {s.ic}, so it is "
                f"seated at {s.ic}'s pins; keep it in {s.block or 'its place'!r} "
                "only if it must sit where that block is (a sensor on what it "
                "measures)"
            ),
        }
        for s in strays(items, _weights(design, netlist), rails)
    ]


def _place(
    design: Design, netlist: Netlist, index: LibraryIndex, effort: float
) -> dict[str, tuple[float, float, float]]:
    board = design.board
    assert board is not None
    items = _items(design, netlist, index)
    weights = _weights(design, netlist)
    try:
        placed = place(
            items,
            board.width.value,
            board.height.value,
            clearance=board.clearance.value,
            weights=weights,
            effort=effort,
        )
    except PlacementError as exc:
        raise LayoutError(str(exc)) from exc
    ox, oy = ORIGIN
    return {ref: (x + ox, y + oy, turn) for ref, (x, y, turn) in placed.items()}


#: The most missing connections mended one by one at the end: each try
#: routes the whole board's grid twice and has KiCad judge it, minutes on
#: a large board.
FEW_GAPS = 5
#: How far round a missing connection other nets' tracks are taken up, in
#: mm: the nearer first, as a designer moves the fewest tracks.
MEND_REACH = (2.0, 4.0)


def gaps(report: Mapping[str, object]) -> list[tuple[str, Point, Point]]:
    """Each connection KiCad's DRC found missing: its net and its two ends.

    Args:
        report: KiCad's DRC report, as JSON.

    Returns:
        The net (KiCad's name) and the two items' places, in mm.
    """
    out: list[tuple[str, Point, Point]] = []
    for item in cast("list[dict[str, Any]]", report.get("unconnected_items", [])):
        ends = item.get("items", [])
        if len(ends) != 2:
            continue
        found = re.search(r"\[([^\]]+)\]", str(ends[0].get("description", "")))
        if found is None:
            continue
        a, b = (e.get("pos", {}) for e in ends)
        out.append(
            (
                found.group(1),
                (float(a.get("x", 0.0)), float(a.get("y", 0.0))),
                (float(b.get("x", 0.0)), float(b.get("y", 0.0))),
            )
        )
    return out


def take_up(
    drawn: Sequence[Track],
    bored: Sequence[Via],
    net: str,
    a: Point,
    b: Point,
    reach: float,
) -> tuple[list[Track], set[int], list[Via], set[int]]:
    """The router's copper of other nets within a reach of a missing connection.

    As a designer clears a way for one connection: every routed track of
    another net that comes within ``reach`` of the line between its ends,
    and that net's vias as near, are taken up, to be routed again once
    the connection is made.

    Args:
        drawn: The router's tracks.
        bored: The router's vias.
        net: The missing connection's net.
        a: One end.
        b: The other end.
        reach: How far from the line, in mm.

    Returns:
        The tracks kept, the ids of those taken up, the vias kept and the
        ids of those taken up.
    """
    ripped = {
        id(t)
        for t in drawn
        if t.net != net and segment_segment(t.start, t.end, a, b) <= reach + t.width / 2
    }
    nets = {t.net for t in drawn if id(t) in ripped}
    gone = {
        id(v) for v in bored if v.net in nets and point_segment(v.at, a, b) <= reach
    }
    return (
        [t for t in drawn if id(t) not in ripped],
        ripped,
        [v for v in bored if id(v) not in gone],
        gone,
    )


def unique_vias(vias: Sequence[Via]) -> list[Via]:
    """The vias with each net's hole at one spot drilled once.

    An island's stitch can land on the grid point where the pour's own
    stitching already put a via of its net; KiCad then reports the holes
    as co-located, and the maker would drill twice.
    """
    # Compared as written, to the micron: two sources' sums can differ in
    # the last bits.
    return list(
        {(round(v.at[0], 3), round(v.at[1], 3), v.net): v for v in vias}.values()
    )


def _guided(
    design: Design, net_names: Sequence[str], copper: Sequence[str]
) -> list[tuple[str, list[str]]]:
    """The board's routes, by KiCad's net names, with their layers checked.

    Raises:
        LayoutError: If a route names a net the board does not have, or a
            layer it has not.
    """
    board = design.board
    assert board is not None
    names = {plain(name): name for name in net_names}
    out: list[tuple[str, list[str]]] = []
    for route in board.routes:
        net = names.get(plain(route.net))
        if net is None:
            raise LayoutError(f"routes names net {route.net!r}, which the board lacks")
        wrong = [layer for layer in route.layers if layer not in copper]
        if wrong:
            raise LayoutError(
                f"routes gives {route.net} layers {wrong}; the board's copper "
                f"is {list(copper)}"
            )
        out.append((net, list(route.layers)))
    return out


def _pad_nets(netlist: Netlist) -> dict[str, dict[str, str]]:
    """Each part's pads' nets, by reference then pad number."""
    pads: dict[str, dict[str, str]] = {}
    for net in netlist.nets:
        for node in net.nodes:
            pads.setdefault(node.ref, {})[node.pin] = net.name
    return pads


def _placement(
    design: Design,
    netlist: Netlist,
    index: LibraryIndex,
    places: Mapping[str, tuple[float, float, float]],
) -> dict[str, object]:
    """The placement report for the parts as placed."""
    pads = _pad_nets(netlist)
    items = [
        Item(
            c.ref,
            read_outline(index, c.footprint),
            "",
            "center",
            None,
            pads.get(c.ref, {}),
        )
        for c in netlist.components
        if c.footprint and c.ref in places
    ]
    return placement_report(items, places, _weights(design, netlist))


def _fan_out(
    design: Design,
    netlist: Netlist,
    index: LibraryIndex,
    places: Mapping[str, tuple[float, float, float]],
    zones: Sequence[Zone],
    fixed: Sequence[Via],
    gap: Callable[[str, str], float],
    keepouts: Sequence[tuple[float, float, float, float]],
) -> Fanout:
    """A via beside each surface pad of every net that has a plane."""
    board = design.board
    assert board is not None
    per_layer: dict[str, set[str]] = {}
    for zone in zones:
        per_layer.setdefault(zone.layer, set()).add(zone.net)
    planes = [next(iter(nets)) for nets in per_layer.values() if len(nets) == 1]
    outlines = {
        c.ref: read_outline(index, c.footprint)
        for c in netlist.components
        if c.footprint and c.ref in places
    }
    pad_nets = _pad_nets(netlist)
    ox, oy = ORIGIN
    bounds = (
        ox + board.clearance.value,
        oy + board.clearance.value,
        ox + board.width.value - board.clearance.value,
        oy + board.height.value - board.clearance.value,
    )
    # KiCad's own Default via when the design states none.
    via = via_of(board)
    return fan_out(
        outlines,
        places,
        pad_nets,
        planes,
        bounds,
        gap=gap,
        fixed=fixed,
        keepouts=keepouts,
        via=via or (0.6, 0.3),
    )


def _via_size(board: Board) -> dict[str, float]:
    """The Default class's via, or KiCad's own Default via."""
    size, drill = via_of(board) or (0.6, 0.3)
    return {"size": size, "drill": drill}


def _pad_boxes(
    netlist: Netlist,
    index: LibraryIndex,
    places: Mapping[str, tuple[float, float, float]],
) -> list[tuple[str, Rect]]:
    """Every placed pad's net and copper box on the board, in mm."""
    nets = _pad_nets(netlist)
    boxes: list[tuple[str, Rect]] = []
    for component in netlist.components:
        if component.footprint and component.ref in places:
            outline = read_outline(index, component.footprint)
            boxes.extend(
                (nets.get(component.ref, {}).get(number, ""), box)
                for number, box, _ in placed_pads(outline, *places[component.ref])
            )
    return boxes


def _through_pads(
    netlist: Netlist,
    index: LibraryIndex,
    places: Mapping[str, tuple[float, float, float]],
) -> list[Rect]:
    """Every placed through pad's copper box on the board, in mm."""
    boxes: list[Rect] = []
    for component in netlist.components:
        if component.footprint and component.ref in places:
            outline = read_outline(index, component.footprint)
            boxes.extend(
                box
                for _, box, layer in placed_pads(outline, *places[component.ref])
                if layer == "all"
            )
    return boxes


def _courtyards(
    netlist: Netlist,
    index: LibraryIndex,
    places: Mapping[str, tuple[float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Every placed part's courtyard on the board, in mm."""
    boxes: list[tuple[float, float, float, float]] = []
    for component in netlist.components:
        if not component.footprint or component.ref not in places:
            continue
        x, y, turn = places[component.ref]
        left, top, right, bottom = turned(
            read_outline(index, component.footprint).courtyard, turn
        )
        boxes.append((x + left, y + top, x + right, y + bottom))
    return boxes


def _weights(design: Design, netlist: Netlist) -> dict[str, float]:
    """Placement weights by KiCad's net names: pours and planes count less."""
    board = design.board
    assert board is not None
    names = {plain(net.name): net.name for net in netlist.nets}
    weights: dict[str, float] = {}
    layers: dict[str, list[str]] = {}
    for pour in board.pours:
        for layer in pour.layers or ["*"]:
            layers.setdefault(layer, []).append(pour.net)
    for pour in board.pours:
        alone = all(len(layers[layer]) == 1 for layer in pour.layers or ["*"])
        name = names.get(pour.net, pour.net)
        weights[name] = PLANE_WEIGHT if alone else POUR_WEIGHT
    return weights


def _pours(
    design: Design,
    netlist: Netlist,
    index: LibraryIndex,
    places: Mapping[str, tuple[float, float, float]],
    copper: tuple[str, ...],
) -> list[Zone]:
    board = design.board
    assert board is not None
    names = {plain(net.name): net.name for net in netlist.nets}
    footprints = {c.ref: c.footprint for c in netlist.components if c.footprint}
    outlines = {ref: read_outline(index, fp) for ref, fp in footprints.items()}
    block_of = {ref: block for block, refs in design.blocks.items() for ref in refs}
    served = {names.get(pour.net, pour.net): set(pour.blocks) for pour in board.pours}
    centres: dict[str, list[tuple[float, float]]] = {}
    for net in netlist.nets:
        for node in net.nodes:
            if node.ref not in places:
                continue
            # A pour is anchored only by its net's pads in the blocks it
            # serves; a stray pad elsewhere is joined by a track.
            blocks = served.get(net.name)
            if blocks and block_of.get(node.ref) not in blocks:
                continue
            x, y, turn = places[node.ref]
            for number, px, py in outlines[node.ref].pads:
                if number == node.pin:
                    dx, dy = rotate(px, py, turn)
                    centres.setdefault(net.name, []).append((x + dx, y + dy))
    by_layer: dict[str, list[str]] = {}
    for pour in board.pours:
        for layer in pour.layers or list(copper):
            if layer not in copper:
                raise LayoutError(
                    f"pour {pour.net}: layer {layer} is not on this board"
                )
            by_layer.setdefault(layer, []).append(names.get(pour.net, pour.net))
    ox, oy = ORIGIN
    width, height = board.width.value, board.height.value
    zones: list[Zone] = []
    for layer, nets in by_layer.items():
        if len(nets) == 1:
            zones.append(plane(nets[0], layer, width, height, ORIGIN))
            continue
        zones += shared(
            {net: centres.get(net, []) for net in nets},
            layer,
            (ox, oy, ox + width, oy + height),
            clearance=board.clearance.value,
        )
    return zones
