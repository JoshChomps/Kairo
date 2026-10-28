"""The MCP server and its read-tier tools.

Every tool here only reads: nothing in a project is changed. What a tool
says about a schematic comes from KiCad itself, through ``kicad-cli``
(the netlist for parts and connections, ERC for rule checks, SVG export
for drawings), and what it says about a symbol or footprint comes from the
library index. A tool that cannot get a trustworthy answer fails with a
stable error code at the start of its message; it never returns a partial
answer that could be read as a pass.

Projects are reached only under the allowed roots the server was started
with.
"""

from __future__ import annotations

import dataclasses
import difflib
import functools
import inspect
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts import Prompt
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as ListedTool

from kairo import __version__, progress, protocol, provenance
from kairo.calllog import CallLog
from kairo.design_tools import DesignTools
from kairo.guard import CALLS_PER_WINDOW, CallGuard, LoopError
from kairo.kicad import gui
from kairo.kicad.cli import (
    LIBRARY_FINDINGS,
    ErcResult,
    ErcStatus,
    ErcViolation,
    KicadCli,
    KicadVersion,
    NetlistStatus,
    RenderStatus,
)
from kairo.kicad.netlist import Netlist, NetlistFormatError, parse_netlist
from kairo.kicad.portability import check_portability
from kairo.library import (
    Library,
    LibraryIndex,
    install_variables,
    read_library_table,
)
from kairo.library.tables import Kind
from kairo.refusal import refusal
from kairo.spice.circuit import Engine
from kairo.spice.engines import find_simulator

#: The permission tiers, least first. A server offers its tier and below.
TIERS = ("read", "propose", "write")
#: Most projects, components, nets or pins returned by one call.
PAGE_SIZE = 100
#: Most ERC violations returned by one call.
VIOLATIONS_PER_PAGE = 20
#: Most folder entries one project search looks at. KiCad's own install,
#: with its demos and libraries, is about 30,000.
SEARCH_BUDGET = 200_000
#: Folders a project search does not enter: hidden ones (version control,
#: KiCad's .history), packages, and KiCad's backups and autosaves.
SKIPPED_FOLDERS = re.compile(
    r"^(\..*|node_modules|__pycache__|.*-backups|#auto_saved_files#)$", re.IGNORECASE
)

_POWER_PIN_TYPES = frozenset({"power_in", "power_out"})

# The first KiCad whose ERC JSON report has lengths at their true size. Before
# it the JSON writer used the wrong unit scale (KiCad issue 25582, "Fix ERC
# JSON report using wrong unit scale", in the 10.0.7 release notes). Measured
# on 10.0.6: 61 item positions on four real boards are each exactly one
# hundredth of the schematic's, and a 2.54 mm bus is described as 0.0254 mm,
# while the text report of the same run is right.
_ERC_SCALE_FIXED_IN = (10, 0, 7)


@dataclass(frozen=True, slots=True)
class Settings:
    """How one server is set up.

    Attributes:
        allowed_roots: Folders the tools may read projects under.
        cache_dir: Where the library index and drawings are kept.
        kicad_cli: The kicad-cli to use; found automatically if ``None``.
        tier: The highest tier of tools to offer: ``read``, ``propose``
            (also checks and calculations that write nothing) or
            ``write`` (also building and changing projects).
        parts_dir: A folder of datasheet facts by part number, if any.
        parts_catalogue: A parts catalogue built by
            `kairo.parts.catalogue`; if ``None``, the one in the cache
            folder when there is one.
        ngspice: The ngspice program to use; found automatically if
            ``None``.
        xyce: The Xyce program to use; found automatically if ``None``.
        models_dir: A folder of registered simulation models, if any.
        calls_per_ten_minutes: The most tool calls accepted in any ten
            minutes, after which a caller is taken to be in a loop; 0
            for no limit.
        bundle_dir: The folder of the Claude Desktop extension that
            started this server, if one did; `server_info` compares its
            manifest with the tools this build has.
        tools_dir: The folder holding Freerouting and its Java, for
            make_board; the ``tools`` folder in the cache folder if
            ``None``.
    """

    allowed_roots: tuple[Path, ...]
    cache_dir: Path
    kicad_cli: Path | None = None
    tier: str = "read"
    parts_dir: Path | None = None
    parts_catalogue: Path | None = None
    ngspice: Path | None = None
    xyce: Path | None = None
    models_dir: Path | None = None
    calls_per_ten_minutes: int = CALLS_PER_WINDOW
    bundle_dir: Path | None = None
    tools_dir: Path | None = None


def default_cache_dir(environ: Mapping[str, str]) -> Path:
    """Where Kairo keeps its library index and drawings unless told otherwise.

    On Windows this is the ``kairo`` folder in ``%LOCALAPPDATA%``; elsewhere
    ``$XDG_CACHE_HOME/kairo``, or ``~/.cache/kairo``.

    Args:
        environ: The environment to read.

    Returns:
        The folder, which may not exist yet.
    """
    if sys.platform == "win32" and environ.get("LOCALAPPDATA"):
        return Path(environ["LOCALAPPDATA"]) / "kairo"
    if environ.get("XDG_CACHE_HOME"):
        return Path(environ["XDG_CACHE_HOME"]) / "kairo"
    return Path.home() / ".cache" / "kairo"


@dataclass(frozen=True, slots=True)
class ToolDoc:
    """What a tool's description must say, so a model can choose it well.

    Attributes:
        what: What the tool does and returns.
        needs: What must be true before calling it.
        limits: What it does not cover, and how results are bounded.
        not_for: When another tool is the right one, and which.
        tier: The permission tier: ``read``, ``propose`` or ``write``.
    """

    what: str
    needs: str
    limits: str
    not_for: str
    tier: str = "read"

    def text(self) -> str:
        """The description as the client sees it."""
        return (
            f"{self.what}\nNeeds: {self.needs}\nLimits: {self.limits}\n"
            f"Not for: {self.not_for}"
        )


TOOL_DOCS: dict[str, ToolDoc] = {
    "list_projects": ToolDoc(
        what="List the KiCad projects (.kicad_pro) under the allowed roots.",
        needs="Nothing.",
        limits=(
            f"{PAGE_SIZE} a page; skips hidden, package and backup folders; at "
            f"most {SEARCH_BUDGET:,} entries searched (see searched_all)."
        ),
        not_for="Looking inside a project: project_summary, read_schematic.",
    ),
    "project_summary": ToolDoc(
        what=(
            "A project's sheets, part and net counts, power nets, ERC status, "
            "and what keeps it from opening the same on another computer."
        ),
        needs="A project under an allowed root.",
        limits="Counts and names only; run_erc=false skips the slow ERC.",
        not_for="Parts or nets (read_schematic, trace_net); violations (run_erc).",
    ),
    "read_schematic": ToolDoc(
        what=(
            "A schematic's parts (ref, value, footprint, symbol, sheet) or nets "
            "(name, pin count), from KiCad's netlist, with both totals."
        ),
        needs="A project under an allowed root.",
        limits=f"One list per call (what); {PAGE_SIZE} a page; no coordinates.",
        not_for="One net's pins (trace_net); a symbol's pins (get_symbol_pins).",
    ),
    "trace_net": ToolDoc(
        what="Every pin on one net: ref, number, name, electrical type.",
        needs="The net's exact name from read_schematic (e.g. /ANT, GND).",
        limits=f"{PAGE_SIZE} a page; an unknown name lists the closest.",
        not_for="Finding nets; use read_schematic.",
    ),
    "find_symbol": ToolDoc(
        what=(
            "Search KiCad's symbol libraries by words; gives Library:Name, "
            "description, pin and unit counts."
        ),
        needs="Words. The first call may take ~30 s to index.",
        limits=(
            "Every word must match (else all but one, noted); 50 results; "
            "global libraries only."
        ),
        not_for="Pin names and numbers; use get_symbol_pins.",
    ),
    "get_symbol_pins": ToolDoc(
        what=(
            "A symbol's exact pins: number, name, type, unit, alternates. The "
            "only source of pins; never recall them from memory."
        ),
        needs="The exact Library:Name from find_symbol.",
        limits=f"{PAGE_SIZE} a page; an unknown name lists the closest.",
        not_for="Pins of a part in a schematic; use trace_net.",
    ),
    "find_footprint": ToolDoc(
        what="Search KiCad's footprint libraries by words; Library:Name and pads.",
        needs="Words.",
        limits="As find_symbol: every word, 50 results, global libraries.",
        not_for="Symbols; use find_symbol.",
    ),
    "run_erc": ToolDoc(
        what=(
            "KiCad's electrical rules check, classed clean, findings, "
            "malformed or unavailable; only clean passes. Missing-library "
            "findings are counted apart."
        ),
        needs="A project under an allowed root.",
        limits=(
            f"{VIOLATIONS_PER_PAGE} violations a page, errors first; read "
            "by_type first, filter with violation_type. Page 1 without a "
            "filter reruns KiCad. Nothing is waived."
        ),
        not_for="Connectivity; use trace_net.",
    ),
    "design_schema": ToolDoc(
        what=(
            "The design's JSON schema, parameter names with units, and the "
            "rules. part: 'patch' (edit_design's operations), 'board', 'all'."
        ),
        needs="Nothing. Read once before writing a design.",
        limits="About 19,000 characters; fixed for the session: never call again.",
        not_for="Pin names; use get_symbol_pins.",
    ),
    "accuracy_report": ToolDoc(
        what=(
            "What a design's results rest on: each tolerance and its source, "
            "each requirement's result and what would resolve it, and each "
            "part's simulation model."
        ),
        needs="A design as design_schema describes.",
        limits="Reads only; writes and simulates nothing.",
        not_for="Structure or drawing problems; use plan_design.",
    ),
    "fetch_datasheet": ToolDoc(
        what="Download and keep a part's datasheet PDF for read_datasheet.",
        needs="part (Library:Name or catalogue number) or url; network.",
        limits="Refuses a link that gives a web page; fetches a link once.",
        not_for="Reading it; use read_datasheet on the file it names.",
    ),
    "read_datasheet": ToolDoc(
        what=(
            "A datasheet's MIN/TYP/MAX table rows with unit and page: a chip's "
            "limits no catalogue holds (reference range, current limit, "
            "absolute maximums)."
        ),
        needs="A PDF under an allowed root or from fetch_datasheet; search words.",
        limits=(
            "MIN/MAX tables only; names may be split, check figures against "
            "the page. 40 rows a call; name pages (a whole PDF takes ~15 s)."
        ),
        not_for="Resistors, capacitors, inductors, LEDs; use find_part.",
    ),
    "save_part": ToolDoc(
        what=(
            "Keep a part's datasheet figures by part number; any design or "
            "drawing naming it (params.mpn, an MPN field) then uses them."
        ),
        needs=(
            "mpn, manufacturer, document, revision, facts: {param: {value, "
            'page, limit?: min|max}}, e.g. {"v_ref": {"value": "0.596V", '
            '"page": "5"}}. A typical and its spread are two params '
            "(v_ref, v_ref_tolerance)."
        ),
        limits="Unreviewed until a person marks them; never alters a reviewed one.",
        not_for="A figure for one design only; state it in the design.",
        tier="propose",
    ),
    "find_part": ToolDoc(
        what=(
            "Real resistors, capacitors, inductors and LEDs of one value, and "
            "regulators by output, with part numbers, tolerance, ratings, "
            "stock. Put the number in params.mpn and Kairo takes its figures."
        ),
        needs=(
            "kind: resistor|capacitor|inductor|led|regulator; value: 10k, "
            "100nF, 15uH, 2V (LED), 3.3V (regulator, with topology "
            "ldo|buck|boost, v_in, i_out). Optional: package, tolerance, "
            "dielectric, v_rating, p_rating, i_sat."
        ),
        limits=(
            "A dated catalogue snapshot, not the datasheet; regulators' "
            "figures are for choosing only. 20 a call."
        ),
        not_for="Symbols and footprints; use find_symbol, find_footprint.",
    ),
    "calc": ToolDoc(
        what=(
            "One cited formula: value, worst case over tolerances, accuracy "
            "block. Never do this arithmetic yourself."
        ),
        needs="name and inputs as text with units, tolerance after ('10k 1%').",
        limits="An input with no tolerance makes the worst case unverified.",
        not_for="A whole design; use analyze_power_tree.",
        tier="propose",
    ),
    "analyze_power_tree": ToolDoc(
        what=(
            "Each rail's current and regulator's load; each requirement pass, "
            "fail or unverified; Kairo's review (faults, suggestions)."
        ),
        needs="A design as design_schema describes.",
        limits="Unsolvable branches are reported unknown. Writes nothing.",
        not_for="Pins or the drawing; use plan_design.",
        tier="propose",
    ),
    "simulate": ToolDoc(
        what=(
            "Simulate a design in ngspice and Xyce: op, tran (peak, overshoot, "
            "rise, settling, inrush, ripple), ac (gain, cutoff, loop margins), "
            "dc; judged against limits at the worst case; graphs saved."
        ),
        needs=(
            "design, and request: {analysis: {kind, duration, f_start, f_stop, "
            "temperatures, ripple_from}, stimuli: [{net, kind: "
            "dc|step|ac|sine, value, ...}], limits: [{metric, net|ref, "
            "max|min}], monte_carlo?, loop?: {pin, into}}. Quantities as "
            "text with units."
        ),
        limits=(
            "A limit passes only at the worst case; a missing tolerance or "
            "esr leaves it unverified, as does any regulator or diode model "
            "in tran or ac. Unmodelled parts are left open and named. "
            "monte_carlo decides nothing. 60 metrics."
        ),
        not_for="Calculated pass or fail; use analyze_power_tree.",
        tier="propose",
    ),
    "simulate_schematic": ToolDoc(
        what="Simulate named parts of a drawn schematic, as simulate does.",
        needs=(
            "project, parts (refs), request as simulate, facts as "
            "check_schematic: the ground net, model values, loads."
        ),
        limits=(
            "Only the named parts; chips without models are open. simulate's "
            "limits apply. Writes nothing."
        ),
        not_for="A Kairo design (simulate); rail currents (check_schematic).",
        tier="propose",
    ),
    "check_schematic": ToolDoc(
        what=(
            "Check a drawn schematic: its netlist plus stated facts give each "
            "rail's current and each requirement's result; lists supply nets "
            "with no role; Kairo's review (faults, suggestions)."
        ),
        needs=(
            "project; facts laid out as a design's nets, components (params, "
            "source, limits), loads, requirements."
        ),
        limits=(
            "Nothing is guessed: no facts, no rails. Pins by number. Writes "
            "nothing. Kept facts (save_facts) apply, yours over them."
        ),
        not_for="Changing it (set_value, edit_schematic); Kairo designs.",
        tier="propose",
    ),
    "plan_design": ToolDoc(
        what=(
            "Check a design without writing: pins, structure, drawing, "
            "requirements; says whether apply_design would proceed."
        ),
        needs="A design as design_schema describes.",
        limits="KiCad's own checks run only on apply. 20 findings.",
        not_for="Writing; use apply_design once the plan is clean.",
        tier="propose",
    ),
    "verify_design": ToolDoc(
        what="Recheck a project built this run against its design, with KiCad.",
        needs="A project apply_design wrote in this server run.",
        limits="This run's projects only. 20 findings.",
        not_for="Other projects; use run_erc and read_schematic.",
        tier="propose",
    ),
    "apply_design": ToolDoc(
        what=(
            "Build a design into a KiCad project and check it with KiCad; on "
            "any failure nothing is written. A wired copy goes in wired/."
        ),
        needs=(
            "A design plan_design accepts; a new folder under an allowed root "
            "or one built this run. Ask the user before the first write."
        ),
        limits=(
            "12 attempts per project per run; after 3 failures in a row "
            "without improvement it refuses: stop and tell the user."
        ),
        not_for="Small changes to a built project; use edit_design.",
        tier="write",
    ),
    "edit_design": ToolDoc(
        what=(
            "Patch a project built this run (add, connect, disconnect, set, "
            "remove; see design_schema 'patch'), rebuild and check; a failed "
            "check restores every file."
        ),
        needs="A project apply_design wrote in this server run.",
        limits="apply_design's attempt limits. Reports lines changed.",
        not_for="Projects Kairo did not build, or edited in KiCad since.",
        tier="write",
    ),
    "save_facts": ToolDoc(
        what="Keep a drawn schematic's facts in its project for later checks.",
        needs="project; facts as check_schematic; replace: true drops the old.",
        limits="Merged by net and part; facts that do not fit are refused.",
        not_for="Kairo designs, whose facts are in the design.",
        tier="write",
    ),
    "set_value": ToolDoc(
        what=(
            "Change one part's value in a drawn schematic, every other byte "
            "kept; KiCad confirms nothing else changed, else it is undone."
        ),
        needs="project, ref, value.",
        limits=(
            "Values only. Refuses a reused sheet, and a schematic open with "
            "unsaved changes."
        ),
        not_for="Kairo designs; use edit_design.",
        tier="write",
    ),
    "edit_schematic": ToolDoc(
        what=(
            "Add or remove a part, or move a pin to another net, in a drawn "
            "KiCad 10 schematic, moving nothing else; KiCad confirms exactly "
            "that changed, else it is undone."
        ),
        needs=(
            "project; operations (all or none): {op:'add_part', ref, lib_id, "
            "value, pins:{pin: net}, footprint?, no_connect?:[pin], "
            "beside?:ref, sheet?:file}, {op:'remove_part', ref}, "
            "{op:'reconnect', pin:'R5.2', net}. Nets as read_schematic names."
        ),
        limits=(
            "Multi-unit parts are added whole (pins by number where names "
            "repeat). Not Net-(...) nets, pins wires pass through, reused "
            "sheets or unsaved open schematics. max_changed_ratio caps a change."
        ),
        not_for="A value alone (set_value); Kairo designs (edit_design).",
        tier="write",
    ),
    "make_board": ToolDoc(
        what=(
            "Make a built project's PCB in one call: place, pour, route with "
            "Kairo's router, and have KiCad fill and check it against the net "
            "classes and maker's rules. Returns ready or not, blockers, "
            "unrouted nets, bends; a ready board gets fab files, JLCPCB's "
            "BOM and placement included."
        ),
        needs=(
            "A project from apply_design (or the design), with board: size, "
            "layers, sides, edges, pours, net_classes, rules (design_schema "
            "'board'). engine: 'freerouting' to use Freerouting."
        ),
        limits=(
            "Minutes (~6 for 250 parts). Places are kept and reused; "
            "replace_places: true redoes them. undo restores the files."
        ),
        not_for="Hand-placed boards (it replaces the board file).",
        tier="write",
    ),
    "undo": ToolDoc(
        what="Put a project back as before the last apply or edit.",
        needs="A project written this server run, not edited in KiCad since.",
        limits="One step per call.",
        not_for="Edits made in KiCad; refused.",
        tier="write",
    ),
    "render_schematic": ToolDoc(
        what="Draw each sheet as an SVG file and return the paths.",
        needs="A project under an allowed root.",
        limits="A picture is never evidence a design is correct.",
        not_for="Reading parts or nets; use read_schematic.",
    ),
    "server_info": ToolDoc(
        what=(
            "Which Kairo build is serving: version, commit, folder, whether "
            "it needs a restart or an extension rebuild, tier, roots, and the "
            "programs found."
        ),
        needs="Nothing.",
        limits="What it cannot read is 'unknown', never guessed.",
        not_for="Checking designs. Call first when a tool seems missing or stale.",
    ),
    "recent_calls": ToolDoc(
        what="This server's latest calls and each tool's counts, times, failures.",
        needs="Nothing.",
        limits="The last 200 calls; text cut at 300 characters.",
        not_for="Undoing. Read after a refusal before repeating the call.",
    ),
    "go": ToolDoc(
        what=(
            "The steps to follow when the user asks to design or check a "
            "circuit (a message starting 'Kairo' or 'kairo go')."
        ),
        needs="What the user asked, in their words.",
        limits="The same steps for every request: call once per request.",
        not_for="Doing the work; it only says what to do.",
    ),
    "open_in_kicad": ToolDoc(
        what="Open a project in the KiCad window on this computer.",
        needs="A screen and a project's .kicad_pro under an allowed root.",
        limits=(
            "Changes nothing. KiCad cannot be told to reload: after a change "
            "the person reverts or reopens; the result says so."
        ),
        not_for="Judging a design; use render_schematic.",
    ),
}


class Workspace:
    """The tools, as plain methods that return JSON-ready dictionaries.

    Tools run on worker threads, so the shared state is used under a lock.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        cli: KicadCli | None = None,
        index: LibraryIndex | None = None,
        opener: Callable[[Path], None] | None = None,
    ) -> None:
        """Set up with nothing loaded; KiCad is looked for on first use.

        Args:
            settings: The allowed roots, cache folder and kicad-cli to use.
            cli: A kicad-cli wrapper to use instead of finding one.
            index: A library index to use instead of building one.
            opener: What opens a project file in the KiCad window, instead
                of starting the KiCad installed beside kicad-cli.
        """
        self.settings = settings
        self._opener = opener
        self._roots = tuple(root.resolve() for root in settings.allowed_roots)
        # Tools run on worker threads, and the netlist cache and the index's
        # database connection are shared between them.
        self._lock = threading.RLock()
        self._cli: KicadCli | None = cli
        self._netlists: dict[Path, tuple[object, Netlist]] = {}
        self._erc: dict[Path, tuple[object, ErcResult]] = {}
        self._index: LibraryIndex | None = index
        self._index_notes: list[str] = []

    def list_projects(self, page: int = 1) -> dict[str, Any]:
        """List KiCad projects under the allowed roots.

        Args:
            page: Which page of results, from 1.

        Returns:
            The roots searched, the projects found, and whether the search
            covered everything (`SEARCH_BUDGET`).
        """
        projects, complete = find_projects(self._roots, SEARCH_BUDGET)
        found = [
            {
                "project": str(project),
                "has_root_sheet": project.with_suffix(".kicad_sch").is_file(),
            }
            for project in projects
        ]
        listed: dict[str, Any] = {
            "allowed_roots": [str(root) for root in self._roots],
            "searched_all": complete,
            **_page("projects", found, page, PAGE_SIZE),
        }
        if not complete:
            listed["note"] = (
                f"The search stopped after {SEARCH_BUDGET:,} entries, so projects "
                "further in are not listed. Pass a project's path to "
                "project_summary directly, or ask the user to start Kairo with a "
                "narrower --allowed-root."
            )
        return listed

    def project_summary(self, project: str, run_erc: bool = True) -> dict[str, Any]:
        """Summarise a project from KiCad's netlist and, unless skipped, its ERC.

        Args:
            project: The ``.kicad_pro`` or root ``.kicad_sch`` file, or the
                project's folder.
            run_erc: Whether to run ERC as well.

        Returns:
            Sheets, counts, power nets, whether the project opens the
            same on another computer (its footprint libraries and 3D
            models), and the ERC status.
        """
        schematic = self._schematic(project)
        netlist = self._netlist(schematic)
        power = sorted(
            net.name
            for net in netlist.nets
            if any(node.pin_type in _POWER_PIN_TYPES for node in net.nodes)
        )
        summary: dict[str, Any] = {
            "root_sheet": str(schematic),
            "written_by": netlist.tool,
            "sheets": list(netlist.sheets),
            "component_count": len(netlist.components),
            "net_count": len(netlist.nets),
            "power_net_count": len(power),
            "power_nets": power[:PAGE_SIZE],
            "power_nets_definition": "nets with a power_in or power_out pin",
        }
        share, variables = self._install()
        summary["portability"] = check_portability(
            schematic.parent,
            (c.footprint for c in netlist.components),
            share / "footprints",
            variables,
        ).as_dict()
        if run_erc:
            result = self._erc_result(schematic, fresh=True)
            summary["erc"] = {
                "status": result.status.value,
                "passed": result.status is ErcStatus.CLEAN,
                "reason": result.reason,
            }
        else:
            summary["erc"] = {"status": "not run", "passed": False}
        return summary

    def read_schematic(
        self,
        project: str,
        what: Literal["components", "nets"] = "components",
        page: int = 1,
    ) -> dict[str, Any]:
        """List a schematic's parts or its nets.

        Args:
            project: The project file, root sheet or folder.
            what: Which list to return, ``components`` or ``nets``.
            page: Which page of that list, from 1.

        Returns:
            Both totals and one page of the chosen list.
        """
        schematic = self._schematic(project)
        netlist = self._netlist(schematic)
        # One list per call: a board has more nets than parts or the reverse,
        # so a page number shared by both would leave the longer list's tail
        # out of reach, and a caller after net names would pay for parts.
        if what == "nets":
            items = [{"name": n.name, "pin_count": len(n.nodes)} for n in netlist.nets]
        else:
            items = [
                {
                    "ref": c.ref,
                    "value": c.value,
                    "footprint": c.footprint,
                    "lib_id": c.lib_id,
                    "sheet": c.sheet,
                }
                for c in netlist.components
            ]
        return {
            "root_sheet": str(schematic),
            "written_by": netlist.tool,
            "components_total": len(netlist.components),
            "nets_total": len(netlist.nets),
            **_page(what, items, page, PAGE_SIZE),
        }

    def trace_net(self, project: str, net: str, page: int = 1) -> dict[str, Any]:
        """List the pins on one net.

        Args:
            project: The project file, root sheet or folder.
            net: The net's exact name.
            page: Which page of pins, from 1.

        Returns:
            The pins on the net.
        """
        schematic = self._schematic(project)
        netlist = self._netlist(schematic)
        match = next((n for n in netlist.nets if n.name == net), None)
        if match is None:
            close = difflib.get_close_matches(net, [n.name for n in netlist.nets], n=5)
            raise ToolError(
                f"E_NET_UNKNOWN: no net is called {net!r}. Closest names: "
                f"{close or 'none'}. read_schematic lists every net."
            )
        pins = [
            {
                "ref": node.ref,
                "pin": node.pin,
                "pin_name": node.pin_function,
                "pin_type": node.pin_type,
            }
            for node in match.nodes
        ]
        return {"net": match.name, **_page("pins", pins, page, PAGE_SIZE)}

    def find_symbol(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search the symbol libraries.

        Args:
            query: Words to look for.
            limit: The most results to return, up to 50.

        Returns:
            The matching symbols, best first.
        """
        with self._lock:
            index = self._library()
            found = index.find_symbols(query, limit=_limit(limit))
            near: list[str] = []
            if not found:
                found, near = _nearest(
                    lambda words: index.find_symbols(words, limit=limit), query
                )
        return {
            "query": query,
            "symbols": [
                {
                    "lib_id": s.lib_id,
                    "description": s.description,
                    "keywords": s.keywords,
                    "pin_count": s.pin_count,
                    "unit_count": s.unit_count,
                }
                for s in found
            ],
            "notes": [*near, *self._index_notes],
        }

    def get_symbol_pins(self, lib_id: str, page: int = 1) -> dict[str, Any]:
        """Give a symbol's pins as the installed library has them.

        Args:
            lib_id: The symbol, as ``Library:Name``.
            page: Which page of pins, from 1.

        Returns:
            The symbol's identifying fields and its pins.
        """
        with self._lock:
            index = self._library()
            symbol = index.symbol(lib_id)
            if symbol is None:
                library, _, name = lib_id.partition(":")
                names = index.symbol_names(library)
                if names:
                    close = [
                        f"{library}:{n}"
                        for n in difflib.get_close_matches(name, names, n=5)
                    ]
                    hint = f"Closest names in {library}: {close or 'none'}."
                else:
                    close = difflib.get_close_matches(
                        library, index.symbol_libraries(), n=5
                    )
                    hint = (
                        f"No library is called {library!r}. Closest library "
                        f"names: {close or 'none'}."
                    )
                raise ToolError(
                    f"E_SYMBOL_UNKNOWN: the indexed libraries have no symbol "
                    f"{lib_id!r}. {hint} Use find_symbol to search."
                )
        pins = [
            {
                "number": p.number,
                "name": p.name,
                "type": p.electrical_type,
                "unit": p.unit,
                "hidden": p.hidden,
                "alternates": [a.name for a in p.alternates],
            }
            for p in symbol.pins
        ]
        return {
            "lib_id": lib_id,
            "description": symbol.description,
            "reference": symbol.reference,
            "default_footprint": symbol.footprint,
            "footprint_filters": symbol.footprint_filters,
            "datasheet": symbol.datasheet,
            "extends": symbol.extends,
            "is_power": symbol.is_power,
            "unit_count": symbol.unit_count,
            "unit_note": "unit 0 means the pin is in every unit",
            **_page("pins", pins, page, PAGE_SIZE),
            "notes": list(self._index_notes),
        }

    def find_footprint(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search the footprint libraries.

        Args:
            query: Words to look for.
            limit: The most results to return, up to 50.

        Returns:
            The matching footprints, best first.
        """
        with self._lock:
            index = self._library()
            found = index.find_footprints(query, limit=_limit(limit))
            near: list[str] = []
            if not found:
                found, near = _nearest(
                    lambda words: index.find_footprints(words, limit=limit), query
                )
        return {
            "query": query,
            "footprints": [
                {
                    "lib_id": f.lib_id,
                    "description": f.description,
                    "tags": f.tags,
                    "pad_count": f.pad_count,
                }
                for f in found
            ],
            "notes": [*near, *self._index_notes],
        }

    def run_erc(
        self, project: str, page: int = 1, violation_type: str | None = None
    ) -> dict[str, Any]:
        """Run ERC and report it classified.

        A call with page 1 and no type runs KiCad. Any other call reads the
        run already made, unless a project file has changed since.

        Args:
            project: The project file, root sheet or folder.
            page: Which page of violations, from 1.
            violation_type: Show only this type, as ``by_type`` names it.

        Returns:
            The status, counts by type and one page of violations.
        """
        schematic = self._schematic(project)
        fresh = page == 1 and violation_type is None
        result = self._erc_result(schematic, fresh=fresh)
        return erc_report(result, page, violation_type)

    def render_schematic(self, project: str) -> dict[str, Any]:
        """Draw a schematic's sheets as SVG files.

        Args:
            project: The project file, root sheet or folder.

        Returns:
            The files written, one per sheet.
        """
        schematic = self._schematic(project)
        folder = self.settings.cache_dir / "renders" / schematic.stem
        result = self._kicad().export_svg(schematic, folder)
        if result.status is not RenderStatus.OK:
            raise ToolError(f"E_RENDER_UNAVAILABLE: {result.reason}")
        return {
            "files": [str(path) for path in result.files],
            "kicad_version": result.kicad_version,
            "notes": [
                "A drawing is for judging readability only; it is not a check.",
                *result.notes,
            ],
        }

    def open_in_kicad(self, project: str) -> dict[str, Any]:
        """Open a project in the KiCad window, unless it is open already.

        Args:
            project: The project file, root sheet or folder.

        Returns:
            Whether a window was started, whether the project already
            appears to be open and for whom, and what a person must do to
            see a change, since a running KiCad 10 cannot be told to
            reload.

        Raises:
            ToolError: ``E_NO_PROJECT_FILE`` if the schematic has no
                ``.kicad_pro`` beside it; ``E_KICAD_GUI_NOT_FOUND`` if no
                KiCad program is installed beside kicad-cli;
                ``E_KICAD_GUI_FAILED`` if it would not start.
        """
        schematic = self._schematic(project)
        target = schematic.with_suffix(".kicad_pro")
        if not target.is_file():
            raise ToolError(
                f"E_NO_PROJECT_FILE: {schematic.name} has no {target.name} beside "
                "it, and KiCad opens a project by that file"
            )
        who = gui.holder(target)
        if who is not None:
            return {
                "opened": False,
                "already_open": True,
                "notes": [
                    f"{target.name} appears to be open in KiCad already ({who}, "
                    "by KiCad's lock file), so no second window was started.",
                    "KiCad 10 does not reload a schematic that changed on disk "
                    "and cannot be told to: in the Schematic Editor, revert the "
                    "sheet or close and reopen it to see a change.",
                ],
            }
        opener = self._opener
        if opener is None:
            program = gui.find_kicad(Path(self._kicad().command[0]))
            if program is None:
                raise ToolError(
                    "E_KICAD_GUI_NOT_FOUND: no KiCad program is installed beside "
                    f"{self._kicad().command[0]}"
                )

            def start(path: Path, program: Path = program) -> None:
                gui.open_project(program, path)

            opener = start
        try:
            opener(target)
        except OSError as exc:
            raise ToolError(f"E_KICAD_GUI_FAILED: {exc}") from exc
        return {
            "opened": True,
            "already_open": False,
            "notes": [
                f"KiCad was started on {target.name}. Open the Schematic Editor "
                "from its project window.",
                "After a later change by Kairo, revert the sheet in the Schematic "
                "Editor or reopen it: KiCad 10 does not reload a changed file.",
            ],
        }

    def allowed(self, folder: str) -> Path:
        """Resolve a path and check it is under an allowed root.

        Args:
            folder: A path the caller named.

        Returns:
            The resolved path.

        Raises:
            ToolError: ``E_PATH_NOT_ALLOWED`` if it is outside every root.
        """
        path = Path(folder).resolve()
        if not any(path.is_relative_to(root) for root in self._roots):
            raise ToolError(
                f"E_PATH_NOT_ALLOWED: {path} is not under an allowed root "
                f"({[str(r) for r in self._roots]}). Restart the server with "
                "--allowed-root to add one."
            )
        return path

    def kicad(self) -> KicadCli:
        """The kicad-cli in use, found on first need.

        Raises:
            ToolError: ``E_KICAD_UNAVAILABLE`` if none is found.
        """
        return self._kicad()

    def library(self) -> LibraryIndex:
        """The library index, brought up to date once per server run."""
        with self._lock:
            return self._library()

    def schematic(self, project: str) -> Path:
        """The root sheet of a project a caller named.

        Args:
            project: A ``.kicad_pro`` or root ``.kicad_sch`` file, or a
                folder holding one project, under an allowed root.

        Returns:
            The root ``.kicad_sch`` file.

        Raises:
            ToolError: ``E_PROJECT_NOT_FOUND`` or ``E_NOT_ALLOWED``.
        """
        return self._schematic(project)

    def netlist(self, schematic: Path) -> Netlist:
        """KiCad's netlist of a root sheet, exported again only after a change.

        Args:
            schematic: The root ``.kicad_sch`` file, from `schematic`.

        Returns:
            The parts and nets.

        Raises:
            ToolError: ``E_KICAD_UNAVAILABLE``, ``E_NETLIST_UNAVAILABLE`` or
                ``E_NETLIST_MALFORMED``.
        """
        return self._netlist(schematic)

    def _schematic(self, project: str) -> Path:
        """Turn what the caller named into a root sheet under an allowed root."""
        path = self.allowed(project)
        if path.is_dir():
            projects = sorted(path.glob("*.kicad_pro"))
            if len(projects) != 1:
                raise ToolError(
                    f"E_PROJECT_NOT_FOUND: {path} holds {len(projects)} "
                    ".kicad_pro files; name the one to use."
                )
            path = projects[0]
        schematic = path.with_suffix(".kicad_sch")
        if path.suffix not in (".kicad_pro", ".kicad_sch") or not schematic.is_file():
            raise ToolError(
                f"E_PROJECT_NOT_FOUND: expected a .kicad_pro or root .kicad_sch "
                f"file, or a project folder; {schematic} does not exist. "
                "list_projects shows what is there."
            )
        return schematic

    def _kicad(self) -> KicadCli:
        with self._lock:
            if self._cli is None:
                cli, discovery = KicadCli.find(self.settings.kicad_cli)
                if cli is None:
                    searched = "; ".join(discovery.searched) or "nowhere"
                    raise ToolError(
                        f"E_KICAD_UNAVAILABLE: kicad-cli not found (searched: "
                        f"{searched}). Install KiCad 9 or newer, or set "
                        "KAIRO_KICAD_CLI."
                    )
                self._cli = cli
            return self._cli

    def _kicad_version(self) -> KicadVersion:
        version = self._kicad().version()
        if version is None:
            raise ToolError(
                "E_KICAD_UNAVAILABLE: could not read the version from "
                f"{self._kicad().command[0]}."
            )
        return version

    def _erc_result(self, schematic: Path, *, fresh: bool) -> ErcResult:
        """An ERC run for a project: a new one, or the last if nothing changed.

        Paging through one run must not start KiCad again for every page:
        that costs seconds each time and the pages could come from different
        runs. A run that gave no report is never kept. A new run whose
        counts differ from the last, with no project file changed between
        them, says so in its notes.
        """
        signature = _signature(schematic, (".kicad_sch", ".kicad_pro", ".kicad_sym"))
        if not fresh:
            with self._lock:
                cached = self._erc.get(schematic)
            if cached is not None and cached[0] == signature:
                return cached[1]
        result = self._kicad().run_erc(schematic)
        with self._lock:
            last = self._erc.pop(schematic, None)
            if result.status is not ErcStatus.UNAVAILABLE:
                if last is not None and last[0] == signature:
                    notes = result.notes + _unsteady_note(last[1], result)
                    result = dataclasses.replace(result, notes=notes)
                self._erc[schematic] = (signature, result)
        return result

    def _netlist(self, schematic: Path) -> Netlist:
        """KiCad's netlist for a project, exported again only when a sheet changed."""
        signature = _signature(schematic, (".kicad_sch",))
        with self._lock:
            cached = self._netlists.get(schematic)
            if cached is not None and cached[0] == signature:
                return cached[1]
        result = self._kicad().export_netlist(schematic)
        if result.status is not NetlistStatus.OK or result.text is None:
            raise ToolError(f"E_NETLIST_UNAVAILABLE: {result.reason}")
        try:
            netlist = parse_netlist(result.text)
        except NetlistFormatError as exc:
            raise ToolError(f"E_NETLIST_MALFORMED: {exc}") from exc
        with self._lock:
            self._netlists[schematic] = (signature, netlist)
        return netlist

    def _install(self) -> tuple[Path, dict[str, str]]:
        """KiCad's data folder and the path variables it defines."""
        version = self._kicad_version()
        share = Path(self._kicad().command[0]).parent.parent / "share" / "kicad"
        return share, install_variables(share, version.major, os.environ)

    def _library(self) -> LibraryIndex:
        """The library index, brought up to date once per server run."""
        if self._index is not None:
            return self._index
        version = self._kicad_version()
        share, variables = self._install()
        libraries: list[Library] = []
        notes: list[str] = []
        kinds: tuple[tuple[Kind, str], ...] = (
            ("symbol", "sym-lib-table"),
            ("footprint", "fp-lib-table"),
        )
        for kind, name in kinds:
            table = _user_table(name, version, os.environ)
            if table is None:
                table = share / "template" / name
                notes.append(f"no user {name} found; using KiCad's default {table}")
            found, problems = read_library_table(table, kind, variables)
            libraries += found
            notes += [
                f"library table entry {p.nickname or p.table} not used: {p.reason}"
                for p in problems
            ]
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        index = LibraryIndex(
            self.settings.cache_dir / f"library-kicad{version.major}.db"
        )
        started = time.monotonic()
        progress.stage(
            "indexing the KiCad libraries (about half a minute the first time)"
        )
        report = index.update(libraries)
        if report.read:
            notes.append(
                f"indexed {report.read} library files in "
                f"{time.monotonic() - started:.0f} s"
            )
        notes += [
            f"library file not indexed: {p.path} ({p.reason})"
            for p in report.problems[:10]
        ]
        if len(report.problems) > 10:
            notes.append(f"and {len(report.problems) - 10} more files not indexed")
        self._index, self._index_notes = index, notes
        return index


def erc_report(
    result: ErcResult, page: int = 1, violation_type: str | None = None
) -> dict[str, Any]:
    """Lay an ERC result out for a tool response.

    Args:
        result: The classified ERC result.
        page: Which page of violations, from 1.
        violation_type: Show only violations of this type.

    Returns:
        Status, counts and one page of the violations that count, errors
        first.
    """
    active = _active(result)
    by_type = Counter(v.type for v in active)
    library = sum(n for kind, n in by_type.items() if kind in LIBRARY_FINDINGS)
    # Errors first, so the first page holds what matters most however many
    # warnings a board has. The sort is stable: KiCad's order is kept within
    # each group.
    active.sort(key=lambda v: v.severity != "error")
    if violation_type is not None:
        if violation_type not in by_type:
            raise ToolError(
                f"E_VIOLATION_TYPE_UNKNOWN: this run has no violation of type "
                f"{violation_type!r}. Types present: {sorted(by_type) or 'none'}."
            )
        active = [v for v in active if v.type == violation_type]
    if not active and page > 1:
        raise ToolError(
            f"E_PAGE_OUT_OF_RANGE: this run is {result.status.value} "
            f"({result.reason}) and has no violations to page; call run_erc "
            "with page 1."
        )
    violations = [
        {
            "type": v.type,
            "severity": v.severity,
            "sheet": v.sheet,
            "description": v.description,
            "items": [item.description for item in v.items],
        }
        for v in active
    ]
    return {
        "status": result.status.value,
        "passed": result.status is ErcStatus.CLEAN,
        "reason": result.reason,
        "kicad_version": result.kicad_version,
        "errors": result.error_count,
        "warnings": result.warning_count,
        "excluded": result.excluded_count,
        "design_findings": len(active) - library,
        "library_findings": library,
        "library_findings_note": (
            "footprint_link_issues, lib_symbol_issues and lib_symbol_mismatch "
            "say a library was not found on this machine; they are still "
            "findings and are never waived"
        ),
        "by_type": dict(sorted(by_type.items())),
        "ignored_checks": list(result.ignored_checks),
        "violation_type": violation_type,
        **_page("violations", violations, page, VIOLATIONS_PER_PAGE),
        "notes": [*result.notes, *_scale_note(result.kicad_version)],
    }


def _active(result: ErcResult) -> list[ErcViolation]:
    """The violations that count.

    Left out: violations a person waived, and violations of checks the
    project has switched off, neither of which KiCad counts.
    """
    return [v for v in result.violations if not v.excluded and v.severity != "ignored"]


def _unsteady_note(before: ErcResult, after: ErcResult) -> tuple[str, ...]:
    """A warning that two runs on the same files disagree, when they do.

    KiCad's ERC is not always repeatable. Measured on KiCad 10.0.6 with
    Antmicro's PolarFire SoM: six runs on untouched copies gave 15, 16, 17,
    15, 16 and 16 ``multiple_net_names`` warnings. A caller comparing two
    runs must not read such a difference as the result of a change.
    """
    was = Counter(v.type for v in _active(before))
    now = Counter(v.type for v in _active(after))
    moved = [
        f"{kind} ({was[kind]} then {now[kind]})"
        for kind in sorted(was.keys() | now.keys())
        if was[kind] != now[kind]
    ]
    if not moved:
        return ()
    return (
        "No project file has changed since the previous run, yet KiCad's "
        f"counts differ: {', '.join(moved)}. KiCad's ERC is not always "
        "repeatable, so this difference does not come from the design.",
    )


def _scale_note(kicad_version: str | None) -> list[str]:
    """A warning about lengths in ERC descriptions, where this KiCad needs one."""
    version = KicadVersion.parse(kicad_version or "")
    if version is None:
        return [
            "The KiCad version is not known, so lengths quoted in the "
            "descriptions cannot be trusted: before 10.0.7 KiCad wrote them "
            "at one hundredth of their size."
        ]
    if (version.major, version.minor, version.patch) < _ERC_SCALE_FIXED_IN:
        return [
            f"KiCad {version.raw} writes lengths in these descriptions at one "
            "hundredth of their size (KiCad issue 25582, fixed in 10.0.7): "
            "'length 0.0254 mm' means 2.54 mm."
        ]
    return []


def build_server(settings: Settings) -> MCPServer:
    """Build the MCP server with every read-tier tool registered.

    Args:
        settings: The allowed roots, cache folder and kicad-cli to use.

    Returns:
        The server, ready to run.
    """
    if settings.tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}; got {settings.tier!r}")
    workspace = Workspace(settings)
    design = DesignTools(workspace)
    started = provenance.capture()
    calls = CallLog(settings.cache_dir)
    server = _LeanServer(
        "kairo",
        version=__version__,
        # Every session that loads Kairo carries these: each word counts.
        instructions=(
            "Kairo reads, checks and builds KiCad designs. Pins come only from "
            "get_symbol_pins or trace_net, numbers only from calc and the "
            "analysis tools; never from memory or your own arithmetic. Not a "
            "pass: an unverified result (give its to_resolve lines unchanged) "
            "or an ERC status other than clean. A pass on default margins "
            "rests on Kairo's wide tolerances: say so. A refusal gives ok "
            "false, a code, the error, a fix and retryable; when a tool says "
            "stop, stop and tell the user. " + protocol.INSTRUCTION
        ),
    )
    # One design tool runs at a time: they share sessions and the index.
    design_lock = threading.Lock()

    def one_at_a_time(
        function: Callable[..., dict[str, Any]],
    ) -> Callable[..., dict[str, Any]]:
        @functools.wraps(function)
        def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            with design_lock:
                return function(*args, **kwargs)

        return call

    def steps(request: str = "") -> dict[str, Any]:
        return protocol.go(settings.tier, request)

    def server_info() -> dict[str, Any]:
        return {
            "version": __version__,
            **provenance.describe(started),
            "extension": provenance.bundle_check(
                settings.bundle_dir, TOOL_DOCS, __version__
            ),
            "tier": settings.tier,
            "allowed_roots": [str(r) for r in settings.allowed_roots],
            "cache_dir": str(settings.cache_dir),
            "programs": _programs(settings),
        }

    def recent_calls(
        limit: int = 20, tool: str | None = None, failed_only: bool = False
    ) -> dict[str, Any]:
        return calls.recent(limit, tool, failed_only)

    tools: dict[str, Callable[..., dict[str, Any]]] = {
        "list_projects": workspace.list_projects,
        "project_summary": workspace.project_summary,
        "read_schematic": workspace.read_schematic,
        "trace_net": workspace.trace_net,
        "find_symbol": workspace.find_symbol,
        "get_symbol_pins": workspace.get_symbol_pins,
        "find_footprint": workspace.find_footprint,
        "run_erc": workspace.run_erc,
        "render_schematic": workspace.render_schematic,
        "open_in_kicad": workspace.open_in_kicad,
        "go": steps,
        "server_info": server_info,
        "recent_calls": recent_calls,
        "design_schema": one_at_a_time(design.design_schema),
        "find_part": one_at_a_time(design.find_part),
        "fetch_datasheet": one_at_a_time(design.fetch_datasheet),
        "read_datasheet": one_at_a_time(design.read_datasheet),
        "save_part": one_at_a_time(design.save_part),
        "accuracy_report": one_at_a_time(design.accuracy_report),
        "calc": one_at_a_time(design.calc),
        "analyze_power_tree": one_at_a_time(design.analyze_power_tree),
        "plan_design": one_at_a_time(design.plan_design),
        "simulate": one_at_a_time(design.simulate),
        "verify_design": one_at_a_time(design.verify_design),
        "apply_design": one_at_a_time(design.apply_design),
        "edit_design": one_at_a_time(design.edit_design),
        "check_schematic": one_at_a_time(design.check_schematic),
        "simulate_schematic": one_at_a_time(design.simulate_schematic),
        "save_facts": one_at_a_time(design.save_facts),
        "set_value": one_at_a_time(design.set_value),
        "edit_schematic": one_at_a_time(design.edit_schematic),
        "make_board": one_at_a_time(design.make_board),
        "undo": one_at_a_time(design.undo),
    }
    offered = TIERS[: TIERS.index(settings.tier) + 1]
    guard = CallGuard(calls_per_window=settings.calls_per_ten_minutes)
    for name, function in tools.items():
        if TOOL_DOCS[name].tier in offered:
            server.add_tool(
                _compact(function, name, guard, calls),
                name=name,
                description=TOOL_DOCS[name].text(),
            )

    def go(request: str = "") -> str:
        return protocol.prompt(settings.tier, request)

    server.add_prompt(
        Prompt.from_function(
            go,
            name="go",
            title="Kairo, go",
            description=(
                "Design or check a circuit with Kairo, by Kairo's own "
                "order of work. Say what you want in request."
            ),
        )
    )
    return server


class _LeanServer(MCPServer):
    """An MCP server whose tool listing carries only what a model reads.

    The listing is paid for in every session that loads Kairo, so it is
    trimmed: the library's generated titles (each an argument's name again),
    the ``null`` alternative and ``default: null`` of optional arguments
    (an argument left out of ``required`` already says it), and the output
    schema, which for every tool said only "an object". Calls are validated
    against the full schemas as before, and results still carry their
    structured content.
    """

    async def list_tools(self) -> list[ListedTool]:
        """The tools, with lean input schemas and no output schemas."""
        listed = await super().list_tools()
        return [
            tool.model_copy(
                update={
                    "input_schema": _lean_schema(tool.input_schema),
                    "output_schema": None,
                }
            )
            for tool in listed
        ]


def _lean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A JSON schema without titles, and optional arguments without null."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key == "properties" and isinstance(value, dict):
            properties = cast("dict[str, dict[str, Any]]", value)
            out[key] = {name: _lean_property(prop) for name, prop in properties.items()}
        else:
            out[key] = value
    return out


def _lean_property(prop: dict[str, Any]) -> dict[str, Any]:
    """One argument's schema, its title dropped and an optional ``X | None`` as X."""
    lean = {k: v for k, v in prop.items() if k != "title"}
    options = lean.get("anyOf")
    if isinstance(options, list) and lean.get("default", "") is None:
        kept = [o for o in cast("list[Any]", options) if o != {"type": "null"}]
        del lean["default"]
        del lean["anyOf"]
        if len(kept) == 1:
            lean.update(cast("dict[str, Any]", kept[0]))
        else:
            lean["anyOf"] = kept
    return lean


def response_text(data: dict[str, Any]) -> str:
    """The text a client is sent for a tool's result.

    JSON with no spaces or line breaks. Left alone, the MCP library sends
    it indented, which on real boards is about two fifths more characters
    for the same content. A list of records that share their fields is
    sent as a table (`_tabled`); the structured result keeps the records.

    Args:
        data: The tool's result.

    Returns:
        The result as compact JSON.
    """
    return json.dumps(_tabled(data), separators=(",", ":"), ensure_ascii=False)


#: A list of at least this many records sharing their fields is a table.
_TABLE_ROWS = 3


def _tabled(value: Any) -> Any:
    """A result with each list of like records as ``{columns, rows}``.

    Each record of a long list repeats its field names: on a 75-pin net
    they were two fifths of ``trace_net``'s text. A list whose records
    all have the same fields, in the same order, is sent as the fields
    once and each record's values as a row.
    """
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        return {key: _tabled(item) for key, item in mapping.items()}
    if not isinstance(value, list):
        return value
    items = [_tabled(item) for item in cast("list[Any]", value)]
    if len(items) < _TABLE_ROWS or not all(isinstance(i, dict) for i in items):
        return items
    records = cast("list[dict[str, Any]]", items)
    columns = list(records[0])
    if not columns or any(list(r) != columns for r in records[1:]):
        return items
    return {"columns": columns, "rows": [list(r.values()) for r in records]}


def _compact(
    function: Callable[..., dict[str, Any]],
    name: str,
    guard: CallGuard,
    calls: CallLog,
) -> Callable[..., Any]:
    """Wrap a tool for the client: compact text, refusals, progress, the log.

    The tool runs on a worker thread while this sends progress (see
    `kairo.progress`). Its answer goes back as compact JSON. An error it
    raises on purpose, with a stable code, goes back as a refusal (see
    `kairo.refusal`); any other is left to the MCP library, which keeps
    a crash's text on the server. A looping caller is stopped. Every
    call, refused or not, goes into the call log (`kairo.calllog`).
    """

    def refused(exc: BaseException, began: float, arguments: str) -> CallToolResult:
        result = _refused(exc)
        data: dict[str, Any] = result.structured_content or {}
        calls.record(
            name,
            time.monotonic() - began,
            "refused",
            code=str(data.get("code", "")),
            arguments=arguments,
            message=str(data.get("error", "")),
        )
        return result

    def work(kwargs: dict[str, Any]) -> CallToolResult:
        began = time.monotonic()
        arguments = json.dumps([[], kwargs], sort_keys=True, default=str)
        shown = json.dumps(kwargs, sort_keys=True, default=str, ensure_ascii=False)
        try:
            guard.before(name, arguments)
        except LoopError as exc:
            return refused(exc, began, shown)
        try:
            data = function(**kwargs)
        except Exception as exc:
            # A call that fails the same way each time is a loop too.
            guard.after(
                name,
                arguments,
                f"{type(exc).__name__}: {exc}",
                error=str(exc).partition(":")[0][:60] or type(exc).__name__,
            )
            if isinstance(exc, ToolError):
                return refused(exc, began, shown)
            calls.record(
                name,
                time.monotonic() - began,
                "error",
                code=type(exc).__name__,
                arguments=shown,
                message=str(exc),
            )
            raise
        text = response_text(data)
        guard.after(name, arguments, text)
        calls.record(name, time.monotonic() - began, "ok", arguments=shown)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=data,
        )

    @functools.wraps(function)
    async def call(ctx: Context, **kwargs: Any) -> CallToolResult:
        latest = [""]

        def named(stage: str) -> None:
            latest[0] = stage

        async def send(seconds: float, message: str) -> None:
            await ctx.report_progress(seconds, None, message)

        # The tool's own error is kept and raised after the task group,
        # which would otherwise wrap it in an exception group.
        outcome: list[CallToolResult | Exception] = []

        def run() -> None:
            try:
                outcome.append(work(kwargs))
            except Exception as exc:
                outcome.append(exc)

        async with anyio.create_task_group() as group:
            group.start_soon(progress.heartbeat, send, name, lambda: latest[0])
            with progress.listening(named):
                # Not abandoned on cancel: a write half done is worse than
                # one finished that nobody reads.
                await anyio.to_thread.run_sync(run)
            group.cancel_scope.cancel()
        (result,) = outcome
        if isinstance(result, Exception):
            raise result
        return result

    # The client sees the tool's own arguments; the library fills in ctx,
    # found by its type, and leaves it out of the arguments it describes.
    own = inspect.signature(function, eval_str=True)
    context = inspect.Parameter(
        "ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context
    )
    call.__signature__ = own.replace(  # pyright: ignore[reportAttributeAccessIssue]
        parameters=[*own.parameters.values(), context]
    )
    call.__annotations__ = {**function.__annotations__, "ctx": Context}
    return call


def _programs(settings: Settings) -> dict[str, Any]:
    """Where each outside program was found, and its version, for server_info."""
    cli, discovery = KicadCli.find(settings.kicad_cli)
    kicad_version = cli.version() if cli is not None else None
    found: dict[str, Any] = {
        "kicad_cli": {
            "path": str(discovery.path) if discovery.path else None,
            "version": kicad_version.raw if kicad_version else None,
            "searched": list(discovery.searched),
        }
    }
    engines: tuple[tuple[Engine, Path | None], ...] = (
        ("ngspice", settings.ngspice),
        ("xyce", settings.xyce),
    )
    for engine, explicit in engines:
        simulator, searched = find_simulator(engine, explicit)
        found[engine] = {
            "path": str(simulator.path) if simulator else None,
            "version": simulator.version if simulator else None,
            "searched": list(searched),
        }
    return found


def _refused(exc: BaseException) -> CallToolResult:
    """A refusal as the client is sent it: the same data as text and object."""
    data = refusal(exc)
    return CallToolResult(
        content=[TextContent(type="text", text=response_text(data))],
        structured_content=data,
        is_error=True,
    )


def find_projects(roots: Sequence[Path], budget: int) -> tuple[list[Path], bool]:
    """Find the .kicad_pro files under some folders, looking at a bounded number.

    Folders matching `SKIPPED_FOLDERS` are not entered, nor are links to
    folders, which could lead outside a root or round in a loop.

    Args:
        roots: The folders to search.
        budget: Most folder entries to look at, across all roots.

    Returns:
        The projects found, sorted, and whether every folder was searched.
    """
    found: list[Path] = []
    seen = 0
    waiting = list(reversed(roots))
    while waiting:
        folder = waiting.pop()
        try:
            with os.scandir(folder) as entries:
                listed = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in listed:
            seen += 1
            if seen > budget:
                return sorted(found), False
            if entry.is_dir(follow_symlinks=False):
                if not SKIPPED_FOLDERS.match(entry.name):
                    waiting.append(Path(entry.path))
            elif entry.name.endswith(".kicad_pro") and entry.is_file():
                found.append(Path(entry.path))
    return sorted(found), True


def _signature(schematic: Path, suffixes: tuple[str, ...]) -> object:
    """What identifies the state of a project's files with these suffixes."""
    return tuple(
        (str(p), p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(schematic.parent.rglob("*"))
        if p.suffix in suffixes and p.is_file()
    )


def _page(
    key: str, items: list[dict[str, Any]], page: int, size: int
) -> dict[str, Any]:
    """One page of a list, with enough to ask for the rest."""
    pages = max((len(items) + size - 1) // size, 1)
    if page < 1 or page > pages:
        raise ToolError(
            f"E_PAGE_OUT_OF_RANGE: {key} has pages 1 to {pages}; got page {page}."
        )
    return {
        f"{key}_total": len(items),
        f"{key}_page": page,
        f"{key}_pages": pages,
        key: items[(page - 1) * size : page * size],
    }


def _nearest[T](
    search: Callable[[str], list[T]], query: str
) -> tuple[list[T], list[str]]:
    """What a search finds with one word left out, when it found nothing whole.

    A caller that asks for ``C_Ceramic 0603`` when the library says
    ``C_0603_1608Metric`` is one word from an answer, and an empty list
    does not tell it which word. A word joined by ``_``, ``-`` or ``:``
    (``LED_SMD_0603``, where the library has ``LED_SMD:LED_0603_1608Metric``)
    is first searched as its parts. Then each word is left out in turn, and
    the first search that finds anything is returned with a note saying so.

    Returns:
        The matches and the note, or nothing and a note that no word
        left out helps.
    """
    words = query.split()
    parts = [part for part in re.split(r"[\s_:-]+", query) if part]
    if len(parts) > len(words):
        found = search(" ".join(parts))
        if found:
            return found, [
                f"Nothing has {query!r} as written. These have each of its "
                f"parts ({', '.join(repr(p) for p in parts)}): use a lib_id "
                "given here as it is."
            ]
        words = parts
    if len(words) < 2:
        return [], []
    for index, word in enumerate(words):
        found = search(" ".join(words[:index] + words[index + 1 :]))
        if found:
            return found, [
                f"Nothing has every word of {query!r}. These have every word "
                f"but {word!r}: search with other words, not the same again."
            ]
    return [], [
        f"Nothing has every word of {query!r}, nor all but one of them: "
        "search with one plain word, such as a size or a package name."
    ]


def _limit(limit: int) -> int:
    if not 1 <= limit <= 50:
        raise ToolError(f"E_LIMIT_OUT_OF_RANGE: limit must be 1 to 50; got {limit}.")
    return limit


def _user_table(
    name: str, version: KicadVersion, environ: Mapping[str, str]
) -> Path | None:
    """The user's own library table, where Kairo knows KiCad keeps it."""
    candidates: list[Path] = []
    if environ.get("KICAD_CONFIG_HOME"):
        candidates.append(Path(environ["KICAD_CONFIG_HOME"]) / name)
    if sys.platform == "win32" and environ.get("APPDATA"):
        folder = f"{version.major}.{version.minor}"
        candidates.append(Path(environ["APPDATA"]) / "kicad" / folder / name)
    return next((path for path in candidates if path.is_file()), None)
