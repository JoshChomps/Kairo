"""A board from a built project in one call: laid out, routed, judged, saved, exported.

Every step a person had to drive by hand on the first boards is here, in
the order that worked, so a caller makes one call and reads one short
summary instead of a DRC report thousands of lines long:

1. Place, pour, stitch and fan out (`kairo.pcb.layout`), with the
   design's net classes reaching every step.
2. Route with the newest Freerouting found, under a Java new enough for
   it (`kairo.pcb.route.find_freerouting`). No router found is said, not
   worked around: the board is still placed and judged.
3. KiCad fills the zones and runs its DRC with the schematic parity check
   and the board's classes and maker's rules.
4. `summarize` folds the verdict into what blocks fabrication (copper,
   holes, unrouted, parity) and what does not (silkscreen), with the
   unrouted connections grouped by net and the bends counted.
5. `save_board` writes the board, its project rules and its places through
   the transaction module; a later call keeps the places, so a re-route
   does not re-place.
6. `export_fab` has KiCad write the Gerbers, drill files, placement and
   BOM, and saves them in the project's ``fab`` folder the same way.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kairo.ir import Design
from kairo.kicad.cli import KicadCli
from kairo.kicad.transaction import Session, WriteReport
from kairo.library.index import LibraryIndex
from kairo.pcb.autoroute import Settings, kairo_router
from kairo.pcb.evaluate import Evaluation, evaluate
from kairo.pcb.jlcpcb import bill, placement
from kairo.pcb.layout import Laid, lay_out_board
from kairo.pcb.route import find_freerouting, router_for
from kairo.process import run_process

#: DRC findings that stop a board being made, or made to work. Everything
#: else KiCad reports (silkscreen overlaps, text sizes) is cosmetic.
BLOCKING = frozenset(
    {
        "clearance",
        "shorting_items",
        "tracks_crossing",
        "hole_clearance",
        "hole_to_hole",
        "copper_edge_clearance",
        "drill_out_of_range",
        "annular_width",
        "via_diameter",
        "track_width",
        "connection_width",
        "starved_thermal",
        "zones_intersect",
        "malformed_courtyard",
        "invalid_outline",
        "padstack",
        "items_not_allowed",
    }
)
#: Where the places of the last layout are kept, inside the project.
PLACES = "kairo/board_places.json"
#: Where the fabrication files go, inside the project.
FAB = "fab"
#: A DRC item that belongs to a footprint: ``Pad A1 [GND] of J5``.
_OWNER = re.compile(r"\bpad\b.* of ([A-Z]+[0-9]+)\b", re.IGNORECASE)
#: The most passes the second routing stage runs.
REJOIN_PASSES = 2
#: Unrouted nets listed in a summary; the rest are counted.
LISTED = 12


@dataclass(frozen=True, slots=True)
class Made:
    """A board made and judged.

    Attributes:
        laid: The board and KiCad's verdict.
        evaluation: Its copper's length, vias and bends.
        router: The router used, or why none was.
        log: The router's output, if it ran.
    """

    laid: Laid
    evaluation: Evaluation
    router: str
    log: str = ""


def make_board(
    design: Design,
    project: Path,
    index: LibraryIndex,
    cli: KicadCli,
    tools: Path | None,
    *,
    passes: int = 30,
    effort: float = 1.0,
    places: Mapping[str, tuple[float, float, float]] | None = None,
    engine: str = "kairo",
) -> Made:
    """Lay out, route and judge a built project's board.

    Args:
        design: The design, with its ``board``.
        project: The built project's folder.
        index: The library index.
        cli: kicad-cli.
        tools: The folder holding Freerouting and its Java, if any.
        passes: The most router passes.
        effort: A multiplier on the placer's moves.
        places: Places to keep from an earlier layout.
        engine: ``kairo`` (Kairo's own router, `kairo.pcb.autoroute`,
            which needs no tools folder) or ``freerouting``. Kairo's
            measured better on the BMS (docs/router-log.md): judged
            with its classes, no blocking DRC finding and 3 connections
            unmade; at the Default clearance, against Freerouting's 14
            clearance faults and 24 unmade.

    Returns:
        The board, its verdict and its measures.
    """
    if engine == "kairo":
        laid = lay_out_board(
            design,
            project,
            index,
            cli,
            effort=effort,
            places=places,
            router=kairo_router(index, settings=Settings(iterations=passes)),
            rejoin=kairo_router(
                index, settings=Settings(iterations=passes), stage="join"
            ),
            guide=lambda net, layers: kairo_router(
                index, settings=Settings(iterations=passes), only={net}, layers=layers
            ),
            # A mend routes a few nets: they settle in a few rounds.
            mend=lambda nets: kairo_router(
                index,
                settings=Settings(iterations=min(passes, MEND_PASSES)),
                stage="join",
                only=nets,
            ),
        )
        return Made(laid, evaluate(laid.text), "Kairo's own router")
    tool = find_freerouting(tools, passes=passes) if tools is not None else None
    # The second stage only joins what the fill and stitching left apart;
    # on the BMS 20 passes of it took an hour for 9 of 45 connections, and
    # given the islands as copper its first pass did all five passes did.
    again = (
        find_freerouting(tools, passes=min(passes, REJOIN_PASSES))
        if tools is not None
        else None
    )
    router = rejoin = None
    used = "none: no Freerouting jar with Java 25 or newer in the tools folder"
    with tempfile.TemporaryDirectory(prefix="kairo-make-") as scratch:
        log = Path(scratch) / "freerouting.log"
        if tool is not None:
            router = router_for(tool, index, design.design, log=log)
            used = f"{tool.jar.name} on {Path(tool.java).parent.parent.name}"
        if again is not None:
            rejoin = router_for(
                again,
                index,
                design.design,
                log=log.with_name("rejoin.log"),
                areas=True,
            )
        laid = lay_out_board(
            design,
            project,
            index,
            cli,
            effort=effort,
            places=places,
            router=router,
            rejoin=rejoin,
        )
        text = "".join(
            path.read_text(encoding="utf-8")
            for path in (log, log.with_name("rejoin.log"))
            if path.is_file()
        )
    return Made(laid, evaluate(laid.text), used, text)


def summarize(made: Made) -> dict[str, Any]:
    """What a person needs from a made board, in a few lines.

    Args:
        made: The board made.

    Returns:
        ``ready`` (nothing blocks fabrication), the blocking findings by
        kind with up to three examples each, the cosmetic ones counted,
        findings inside one footprint's own land pattern (kept as the
        maker drew it), the unrouted connections by net, the wide nets
        drawn narrower than their class (for a current check), the
        router's own last word, and the copper's measures.
    """
    laid = made.laid
    report: dict[str, Any] = dict(laid.report)
    blocking: dict[str, dict[str, Any]] = {}
    own: dict[str, dict[str, Any]] = {}
    cosmetic: Counter[str] = Counter()
    for v in report.get("violations", []):
        kind = str(v["type"])
        if kind not in BLOCKING:
            cosmetic[kind] += 1
            continue
        # Between two pads of one footprint: the maker's land pattern as
        # drawn, which the board keeps; listed, not blocking.
        refs = {
            m.group(1)
            for i in v.get("items", [])
            if (m := _OWNER.search(str(i["description"])))
        }
        whole = all(_OWNER.search(str(i["description"])) for i in v.get("items", []))
        sort = own if whole and len(refs) == 1 else blocking
        entry = sort.setdefault(kind, {"count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append(
                " | ".join(str(i["description"]) for i in v.get("items", []))
            )
    by_net: Counter[str] = Counter()
    for v in report.get("unconnected_items", []):
        found = re.search(r"\[([^\]]+)\]", str(v["items"][0]["description"]))
        by_net[found.group(1) if found else "?"] += 1
    ev = made.evaluation
    router_said = [
        line.split("INFO", 1)[-1].strip()
        for line in made.log.splitlines()
        if "stage completed" in line or "Skipping optimization" in line
    ]
    return {
        "ready": not blocking and laid.unconnected == 0 and not laid.parity,
        "blocking": blocking,
        "land_pattern": own,
        "unconnected": {
            "total": laid.unconnected,
            "by_net": dict(by_net.most_common(LISTED)),
        },
        "parity": laid.parity,
        "cosmetic": dict(cosmetic.most_common()),
        "fanout_left": laid.fanned_out[:LISTED],
        "placement": laid.placement,
        "spacing_unchecked": {
            "count": len(laid.unstated),
            "nets": laid.unstated[:LISTED],
        },
        "narrowed": {
            net: {**entry, **laid.current.get(net, {})}
            for net, entry in sorted(
                laid.narrowed.items(), key=lambda kv: -kv[1]["longest"]
            )[:LISTED]
        },
        "detours": dict(list(laid.detours.items())[:LISTED]),
        "blocks": laid.blocks[:LISTED],
        "mended": laid.mended,
        "current_fails": sorted(
            net for net, c in laid.current.items() if c.get("verdict") == "fail"
        ),
        "router": made.router,
        "router_said": router_said[-2:],
        "copper": {
            "track_mm": ev.length,
            "vias": ev.vias,
            "bends": ev.bends,
            "bends_per_m": ev.bends_per_m,
        },
    }


#: The summary's findings a model is to act on, in the order the go steps
#: give (`kairo.protocol`); a test holds the steps to naming each.
ACTED_ON = (
    "blocking",
    "blocks",
    "unconnected",
    "next",
    "current_fails",
    "detours",
    "spacing_unchecked",
)

#: The most router passes for mending one missing connection.
MEND_PASSES = 10

#: Few enough unrouted connections to name each net to route first.
FEW = 10


def next_step(summary: Mapping[str, Any], design: Design) -> str:
    """What to do about a made board, from its summary and its design.

    Args:
        summary: What `summarize` gave.
        design: The design the board was made from.

    Returns:
        One instruction, naming the design field to change where one is to
        blame.
    """
    if summary["ready"]:
        return "Nothing blocks fabrication; the fab folder holds the files."
    if summary["blocking"]:
        return (
            "Fix the blocking kinds first: a clearance or hole finding near "
            "one part usually means a side or edge to change in the design; "
            "then call again (the places are kept)."
        )
    by_net: Mapping[str, int] = summary["unconnected"]["by_net"]
    strays: Sequence[Mapping[str, str]] = summary.get("blocks", [])
    if by_net and strays:
        return (
            f"{len(strays)} part(s) serve only an IC of another block, so they "
            "are placed away from its pins and their tracks cross the board "
            f"({', '.join(s['part'] for s in strays)}): follow each one's fix "
            "in blocks (move it into its IC's block in the design's blocks, "
            "unless it must sit where it is), then call again with "
            "replace_places."
        )
    if by_net:
        board = design.board
        poured: set[str] = {p.net for p in board.pours} if board is not None else set()
        on_pours = sum(
            n for net, n in by_net.items() if net.rsplit("/", 1)[-1] in poured
        )
        if on_pours * 2 >= sum(by_net.values()):
            return (
                "Most unrouted connections are on poured nets: the pours are "
                "split into pieces no track can reach. Pour each net only "
                "over the blocks its current flows through (board.pours "
                "blocks), let its small pads take tracks, and call again "
                "with replace_places."
            )
        if sum(by_net.values()) <= FEW:
            return (
                f"A few connections are left ({', '.join(sorted(by_net))}): "
                "name their nets in board.routes, hardest first, with a layer "
                "where one is clear, so they are routed before the rest as "
                "by hand, and call again keeping the places; if they still "
                "fail, give the crowded blocks more room (board.sides)."
            )
        return (
            "Connections are left unrouted: give the crowded blocks more "
            "room (board.sides, or a larger board) and call again with "
            "replace_places; more passes rarely help once a run levels off."
        )
    return "Schematic parity differs: rebuild the project, then call again."


def save_board(session: Session, design: Design, made: Made) -> WriteReport:
    """Write the board, its project rules and its places into the project.

    Args:
        session: The project's transaction session.
        design: The design.
        made: The board made.

    Returns:
        The transaction's report.
    """
    files = {
        f"{design.design}.kicad_pcb": made.laid.text,
        PLACES: json.dumps(made.laid.places, indent=1) + "\n",
    }
    if made.laid.project:
        files[f"{design.design}.kicad_pro"] = made.laid.project
    if made.laid.pair_rules:
        files[f"{design.design}.kicad_dru"] = made.laid.pair_rules
    for name in files:
        if (session.folder / name).is_file():
            session.read(name)
    return session.write(files)


def kept_places(project: Path) -> dict[str, tuple[float, float, float]] | None:
    """The places a previous `save_board` kept, if any."""
    path = project / PLACES
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {ref: (float(x), float(y), float(t)) for ref, (x, y, t) in data.items()}


def export_fab(
    session: Session,
    design: Design,
    cli: KicadCli,
    lcsc_for: Callable[[str], str | None] = lambda _: None,
) -> tuple[WriteReport, list[str], list[str]]:
    """Have KiCad write the fabrication files, and save them in ``fab``.

    Gerbers with the X2 attributes and a netlist, Excellon drill files in
    mm with plated and unplated holes apart, a placement file in mm (CSV)
    and the schematic's BOM (CSV), and the same placement and BOM in
    JLCPCB's columns (`kairo.pcb.jlcpcb`). KiCad writes them in a scratch
    copy of the project; they reach the project through the transaction
    like every other file.

    Args:
        session: The project's transaction session.
        design: The design.
        cli: kicad-cli.
        lcsc_for: The LCSC number for a maker's part number, or ``None``.

    Returns:
        The transaction's report, the names of the files written, and the
        parts JLCPCB's BOM has no LCSC number for, a line each.
    """
    project = session.folder
    with tempfile.TemporaryDirectory(prefix="kairo-fab-") as scratch:
        copy = Path(scratch) / "p"
        shutil.copytree(project, copy, ignore=shutil.ignore_patterns("kairo", "*.lck"))
        out = Path(scratch) / "out"
        out.mkdir()
        board = str(copy / f"{design.design}.kicad_pcb")
        sheet = str(copy / f"{design.design}.kicad_sch")
        steps = [
            ["pcb", "export", "gerbers", "-o", f"{out}/", board],
            ["pcb", "export", "drill", "-o", f"{out}/", "--excellon-separate-th",
             "-u", "mm", board],
            ["pcb", "export", "pos", "--format", "csv", "--units", "mm",
             "-o", str(out / f"{design.design}-pos.csv"), board],
            ["sch", "export", "bom", "-o", str(out / f"{design.design}-bom.csv"),
             sheet],
        ]  # fmt: skip
        for step in steps:
            run_process([*cli.command, *step], timeout_s=300)
        files = {
            f"{FAB}/{path.name}": path.read_bytes().decode("utf-8")
            for path in sorted(out.iterdir())
            if path.is_file()
        }
    mpn_of = {
        ref: str(part.params["mpn"])
        for ref, part in design.components.items()
        if isinstance(part.params.get("mpn"), str)
    }
    jlc_bom, unsourced = bill(files[f"{FAB}/{design.design}-bom.csv"], mpn_of, lcsc_for)
    files[f"{FAB}/{design.design}-jlcpcb-bom.csv"] = jlc_bom
    files[f"{FAB}/{design.design}-jlcpcb-cpl.csv"] = placement(
        files[f"{FAB}/{design.design}-pos.csv"]
    )
    for name in files:
        if (project / name).is_file():
            session.read(name)
    return session.write(files), sorted(files), unsourced
