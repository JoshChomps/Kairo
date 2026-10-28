"""The tools that propose and write designs.

Read-tier tools look at projects. These go further, in two tiers:

- **propose**: check a design, run a calculation, analyse its power tree
  or accuracy. Nothing is written.
- **write**: build a design into a project folder, change it, undo a
  change. Every write goes through a session, which backs up what it
  replaces and puts everything back if a check fails.

A write is refused, with the arithmetic, when a requirement cannot be
met. And a model cannot loop: each project has a limit on write attempts
per server run, and after three failed attempts in a row that do not
improve the verifier's score, further attempts are refused until the
user is consulted.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from kairo.accuracy.report import REPORT_FILE, accuracy_report
from kairo.build import build_project, verify_project
from kairo.calc import CATALOGUE, Given, calculate
from kairo.ir import Design, PatchError, apply_patch, design_schema, parse_patch
from kairo.ir.models import PARAMETERS
from kairo.ir.schema import EXAMPLE
from kairo.kicad.cli import ErcResult
from kairo.kicad.drawn import DrawnError, design_name, read_drawn, restrict
from kairo.kicad.drawn_edit import Project, add_part, reconnect, remove_part
from kairo.kicad.edit import EditError, set_value
from kairo.kicad.netlist import parse_netlist
from kairo.kicad.project_facts import (
    FACTS_FILE,
    FactsFileError,
    facts_text,
    laid_over,
    load_facts,
)
from kairo.kicad.schematic_writer import WriterError, write_schematic
from kairo.kicad.transaction import Session, TransactionError, file_change
from kairo.library import LibraryIndex
from kairo.library.grounding import Grounded, GroundingError, ground
from kairo.parts import Fact, Part, PartsError, PartStore, Source
from kairo.parts.catalogue import (
    FILE_NAME,
    Catalogue,
    CatalogueError,
    CataloguePart,
)
from kairo.parts.datasheet import DatasheetError, matching, read_rows
from kairo.parts.fetch import DATASHEETS, FetchError, fetch_datasheet
from kairo.parts.fill import Filled, Remembered, fill, fill_from_store
from kairo.pcb.layout import LayoutError
from kairo.pcb.make import (
    export_fab,
    kept_places,
    make_board,
    next_step,
    save_board,
    summarize,
)
from kairo.pcb.route import RoutingError
from kairo.spice.circuit import CircuitError, Engine, netlist
from kairo.spice.crosscheck import cross_check
from kairo.spice.engines import ACCURATE, Simulator, commands, find_simulator
from kairo.spice.loop import measure_loop
from kairo.spice.montecarlo import monte_carlo
from kairo.spice.practice import review
from kairo.spice.registry import ModelRegistry, RegistryError
from kairo.spice.report import charts, simulation_accuracy, table
from kairo.spice.request import SimRequest
from kairo.spice.simulate import simulate, sweep_source
from kairo.spice.sweep import worst_case
from kairo.units import QuantityError, parse_quantity
from kairo.verify.findings import Finding
from kairo.verify.layout import check_layout
from kairo.verify.report import make_report
from kairo.verify.requirements import RequirementResult, evaluate_requirements
from kairo.verify.review import review_design
from kairo.verify.structure import check_structure

if TYPE_CHECKING:
    from kairo.server import Workspace

#: Units a value may leave out, as component values usually do (1k, 100n).
_BARE_UNITS = frozenset({"", "ohm", "F", "H"})
#: The most metrics one simulate call returns.
MAX_METRICS = 60
#: Where saved parts are kept in Kairo's cache folder when no parts
#: folder is set.
PARTS_FOLDER = "parts"
#: The kinds of regulator the catalogue keeps.
TOPOLOGIES = ("ldo", "buck", "boost")
#: Rows of a datasheet shown in one answer.
DATASHEET_ROWS = 40
#: The most faults, and the most suggestions, one review lists.
REMARKS_LISTED = 15
#: The most nets with an unstated role that check_schematic lists.
UNSTATED_LISTED = 30
#: Write attempts allowed on one project in one server run.
MAX_ROUNDS = 12
#: Failed attempts in a row, none scoring better than the best so far,
#: after which further attempts are refused.
STALL_ROUNDS = 3


@dataclass(slots=True)
class DesignSession:
    """One project being designed in this server run.

    Attributes:
        files: The session its files are written through.
        designs: The designs applied, oldest first; the last is current.
        attempts: Write attempts made.
        best_score: The best verifier score any attempt has reached.
        stalled: Failed attempts in a row that did not beat that score.
    """

    files: Session
    designs: list[Design] = field(default_factory=list[Design])
    attempts: int = 0
    best_score: float = -1.0
    stalled: int = 0


class DesignTools:
    """The propose-tier and write-tier tools.

    Args:
        workspace: The server's workspace, for KiCad, the library index
            and the allowed roots.
        parts: Datasheet facts by part number, if a parts folder is set.
    """

    def __init__(self, workspace: Workspace, parts: PartStore | None = None) -> None:
        """Start with no design sessions."""
        self._workspace = workspace
        settings = workspace.settings
        self._parts = parts or PartStore(
            settings.parts_dir or settings.cache_dir / PARTS_FOLDER
        )
        self._sessions: dict[Path, DesignSession] = {}
        self._edits: dict[Path, Session] = {}
        self._simulations = 0
        self._catalogue_opened = False
        self._catalogue_found: Catalogue | None = None
        self._filled: Filled | None = None
        self._remembered: Remembered | None = None

    # Read tier

    def find_part(
        self,
        kind: str,
        value: str,
        package: str | int | None = None,
        tolerance: str | None = None,
        dielectric: str | None = None,
        v_rating: str | None = None,
        p_rating: str | None = None,
        i_sat: str | None = None,
        topology: str | None = None,
        v_in: str | None = None,
        i_out: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Find real parts of one value in the parts catalogue.

        Args:
            kind: ``resistor``, ``capacitor``, ``inductor``, ``led`` or
                ``regulator``.
            value: The value as text: ``10k``, ``100nF``, ``15uH``; for
                an LED its forward voltage, ``2V``; for a regulator
                its output voltage, ``3.3V``.
            package: The package, such as ``0603``; a number is read as
                its text, since a model may send ``2010`` unquoted.
            tolerance: The widest tolerance accepted, such as ``1%``.
            dielectric: For a capacitor, the kind wanted, such as ``X7R``.
            v_rating: The least rated voltage accepted.
            p_rating: The least rated power accepted.
            i_sat: The least saturation current accepted.
            topology: For a regulator: ``ldo``, ``buck`` or ``boost``.
            v_in: For a regulator, the input voltage it must take.
            i_out: For a regulator, the least output current accepted.
            limit: The most parts to return, 1 to 20.

        Returns:
            The parts, basic parts first and then by stock, with how many
            matched in all and the day the listings were read.

        Raises:
            ToolError: ``E_CATALOGUE_UNAVAILABLE`` if no catalogue is on
                this computer; ``E_PART_QUERY`` if the kind, the value or
                a figure is not understood.
        """
        catalogue = self._catalogue()
        if catalogue is None:
            raise ToolError(
                "E_CATALOGUE_UNAVAILABLE: no parts catalogue is on this computer. "
                "A person can get one with: python -m kairo.parts.catalogue fetch "
                "FOLDER, then python -m kairo.parts.catalogue build "
                f"FOLDER/cache.sqlite3 {self._workspace.settings.cache_dir / FILE_NAME}"
            )
        units = {
            "resistor": "ohm",
            "capacitor": "F",
            "inductor": "H",
            "led": "V",
            "regulator": "V",
        }
        if kind not in units:
            raise ToolError(
                f"E_PART_QUERY: kind is one of {', '.join(units)}; got {kind!r}"
            )
        if topology is not None and topology.lower() not in TOPOLOGIES:
            raise ToolError(
                f"E_PART_QUERY: topology is one of {', '.join(TOPOLOGIES)}; "
                f"got {topology!r}"
            )
        if kind != "regulator" and (topology or v_in or i_out):
            raise ToolError(
                "E_PART_QUERY: topology, v_in and i_out are for a regulator"
            )
        if not 1 <= limit <= 20:
            raise ToolError(f"E_PART_QUERY: limit is 1 to 20; got {limit}")

        def read(text: str, unit: str, what: str) -> float:
            try:
                return parse_quantity(text, unit, bare=unit in ("ohm", "F", "H")).value
            except QuantityError as exc:
                raise ToolError(f"E_PART_QUERY: {what}: {exc}") from exc

        minimums = {
            name: read(text, unit, name)
            for name, text, unit in (
                ("v_rating", v_rating, "V"),
                ("p_rating", p_rating, "W"),
                ("i_sat", i_sat, "A"),
                ("i_out", i_out, "A"),
            )
            if text is not None
        }
        try:
            found, total = catalogue.find(
                cast("Any", kind),
                read(value, units[kind], "value"),
                package=None if package is None else str(package),
                tolerance=read(tolerance, "%", "tolerance") if tolerance else None,
                minimums=minimums,
                dielectric=dielectric,
                topology=topology,
                v_in=read(v_in, "V", "v_in") if v_in else None,
                limit=limit,
            )
        except CatalogueError as exc:
            raise ToolError(f"E_PART_QUERY: {exc}") from exc
        return {
            "matched": total,
            "listed": len(found),
            "read_on": catalogue.date,
            "parts": [_listed(part) for part in found],
            "notes": [
                "A distributor's listing, not the datasheet. Stock is as of the "
                "day the listings were read.",
                *(
                    [
                        "A regulator's listed figures are for choosing one and "
                        "are never filled into a design: they do not say which "
                        "are typical and which are limits. Read the chosen "
                        "part's limits from its datasheet (read_datasheet) and "
                        "keep them with save_part."
                    ]
                    if kind == "regulator"
                    else []
                ),
                *(
                    [
                        "Nothing matched: widen the tolerance, drop the package or "
                        "the ratings, or the snapshot may not hold such a part."
                    ]
                    if not found
                    else []
                ),
            ],
        }

    def fetch_datasheet(
        self, part: str | None = None, url: str | None = None
    ) -> dict[str, Any]:
        """Fetch a part's datasheet from its maker, once, into Kairo's folder.

        Args:
            part: A KiCad symbol as ``Library:Name``, whose Datasheet field
                gives the link, or a part number the parts catalogue lists.
            url: The link itself, which wins over ``part``.

        Returns:
            The kept file's name, which `read_datasheet` takes, with where
            it came from, its hash and the day it was fetched.

        Raises:
            ToolError: ``E_DATASHEET_NO_LINK`` if no link is found; the
                codes of `kairo.parts.fetch.fetch_datasheet`.
        """
        link = url
        found_by = "given"
        if link is None and part:
            if ":" in part:
                symbol = self._index().symbol(part)
                link = symbol.datasheet if symbol is not None else None
                found_by = f"the Datasheet field of {part}"
            else:
                catalogue = self._catalogue()
                listed = catalogue.lookup(part) if catalogue is not None else None
                link = listed.datasheet if listed is not None else None
                found_by = f"the parts catalogue's listing of {part}"
        if not link or not link.lower().startswith(("http://", "https://")):
            raise ToolError(
                f"E_DATASHEET_NO_LINK: no link to a datasheet for {part or 'that'}"
                + (f" ({found_by} holds {link!r})" if link else "")
                + ". Give the maker's link as url"
            )
        folder = self._workspace.settings.cache_dir / DATASHEETS
        try:
            kept = fetch_datasheet(folder, link)
        except FetchError as exc:
            raise ToolError(str(exc)) from exc
        out = kept.as_dict()
        out["link_from"] = found_by
        out["next"] = (
            f"read_datasheet with file {kept.file!r}; cite the maker's document "
            "number and page for each figure you use"
        )
        return out

    def read_datasheet(
        self,
        file: str,
        search: str | None = None,
        pages: list[int] | int | str | None = None,
    ) -> dict[str, Any]:
        """Read the limit tables of a datasheet PDF.

        Args:
            file: The PDF, under an allowed root or in the ``datasheets``
                folder of Kairo's cache.
            search: Only rows whose text holds every one of these words.
            pages: Only these pages, from 1; every page if ``None``. One
                page may be a number, and text such as ``5``, ``5-7``
                or ``5, 9`` is read too.

        Returns:
            The rows, each with the figures under MIN, TYP and MAX kept
            apart, its unit and its page.

        Raises:
            ToolError: ``E_PATH_NOT_ALLOWED``; ``E_DATASHEET_READER_MISSING``
                or ``E_DATASHEET_UNREADABLE`` from the reader.
        """
        path = Path(file)
        kept = self._workspace.settings.cache_dir / DATASHEETS
        if not path.is_absolute() and (kept / path).is_file():
            path = kept / path
        elif not path.resolve().is_relative_to(kept.resolve()):
            path = self._workspace.allowed(file)
        try:
            rows = read_rows(path, _pages(pages))
        except DatasheetError as exc:
            raise ToolError(str(exc)) from exc
        found = matching(rows, search) if search else rows
        out: dict[str, Any] = {
            "file": path.name,
            "rows_found": len(found),
            "rows": [row.as_dict() for row in found[:DATASHEET_ROWS]],
            "notes": [
                "Read from the page by position. A figure under typical is not "
                "a limit: only minimum and maximum are. Give a limit to a part "
                "as a param, list it under limits as min or max, and cite the "
                "document and page as its source.",
                "A parameter's name is the words on its line as they lie, so a "
                "subscript is lost and a name on two lines is in two pieces: "
                "check a row against the page before relying on it.",
            ],
        }
        if len(found) > DATASHEET_ROWS:
            out["rows_omitted"] = len(found) - DATASHEET_ROWS
            out["notes"].append("Narrow with search words or pages to see the rest.")
        if not rows:
            out["notes"].append(
                "No table with MIN and MAX headings was found on the pages read."
            )
        return out

    def save_part(
        self,
        mpn: str,
        manufacturer: str,
        document: str,
        revision: str,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        """Remember a part's datasheet figures under its part number.

        Args:
            mpn: The maker's part number.
            manufacturer: Who makes it.
            document: The datasheet's title or number, and its publisher.
            revision: Its revision or date.
            facts: Each figure by its parameter name, as ``{"value":
                "0.611V", "page": "5", "limit": "max"}``; ``limit`` is
                ``min`` or ``max`` for a datasheet limit and left out for
                a typical or nominal figure, and ``condition`` may give
                the conditions.

        Returns:
            The part number, the figures now saved for it and the file.

        Raises:
            ToolError: ``E_PART_INVALID`` if a figure is not laid out as
                above, names no parameter of a design or has the wrong
                unit; ``E_PART_REVIEWED`` if it would change a figure a
                person has marked reviewed.
        """
        try:
            known = self._parts.get(mpn)
        except PartsError as exc:
            raise ToolError(f"E_PART_INVALID: {exc}") from exc
        saved = dict(known.facts) if known is not None else {}
        if not facts:
            raise ToolError("E_PART_INVALID: facts is empty: give at least one figure")
        for name, given in facts.items():
            unit = PARAMETERS.get(name, "")
            if name not in PARAMETERS or name == "mpn":
                raise ToolError(
                    f"E_PART_INVALID: {name!r} is not a parameter of a design; "
                    f"they are {', '.join(sorted(set(PARAMETERS) - {'mpn'}))}"
                )
            entry = cast("dict[str, Any]", given) if isinstance(given, dict) else {}
            if not {"value", "page"} <= set(entry):
                raise ToolError(
                    f"E_PART_INVALID: give {name} as "
                    '{"value": "0.611V", "page": "5"}, with "limit": "min" or '
                    '"max" if it is a datasheet limit'
                )
            try:
                if unit is not None:
                    parse_quantity(str(entry["value"]), unit)
                fact = Fact(
                    value=str(entry["value"]),
                    source=Source(
                        document=document, revision=revision, page=str(entry["page"])
                    ),
                    obtained="extracted",
                    limit=entry.get("limit"),
                    condition=str(entry.get("condition", "")),
                )
            except (QuantityError, ValidationError) as exc:
                raise ToolError(f"E_PART_INVALID: {name}: {exc}") from exc
            before = saved.get(name)
            if (
                before is not None
                and before.review == "reviewed"
                and (before.value, before.limit) != (fact.value, fact.limit)
            ):
                raise ToolError(
                    f"E_PART_REVIEWED: {mpn}'s {name} is {before.value}, checked "
                    f"by a person against {before.source.cite()}; it was not "
                    "changed. State another figure in the design itself."
                )
            if before is None or before.review != "reviewed":
                saved[name] = fact
        try:
            part = Part(
                mpn=mpn,
                manufacturer=manufacturer,
                facts=saved,
                dc_bias=known.dc_bias if known is not None else None,
            )
        except ValidationError as exc:
            raise ToolError(f"E_PART_INVALID: {exc}") from exc
        path = self._parts.put(part)
        return {
            "mpn": mpn,
            "saved": {
                name: {
                    "value": fact.value,
                    "limit": fact.limit,
                    "source": fact.source.cite(),
                    "review": fact.review,
                }
                for name, fact in part.facts.items()
            },
            "file": str(path),
            "notes": [
                "A part that gives this number as params.mpn, in a design or "
                "in a drawn schematic's fields, now takes these figures where "
                "it states none.",
                "Each figure is unreviewed until a person checks it against "
                "the page and sets its review to reviewed in the file; results "
                "that rest on one cite it as unreviewed.",
            ],
        }

    def _catalogue(self) -> Catalogue | None:
        """The parts catalogue, if one is set or is in the cache folder."""
        if self._catalogue_opened:
            return self._catalogue_found
        self._catalogue_opened = True
        settings = self._workspace.settings
        path = settings.parts_catalogue or settings.cache_dir / FILE_NAME
        if path.is_file():
            try:
                self._catalogue_found = Catalogue(path)
            except CatalogueError as exc:
                raise ToolError(f"E_CATALOGUE_UNAVAILABLE: {exc}") from exc
        return self._catalogue_found

    def _noted(self, out: dict[str, Any]) -> dict[str, Any]:
        """Add what the catalogue did for the design just read, if anything."""
        if self._remembered:
            out["parts_store"] = self._remembered.as_dict()
        if self._filled:
            out["catalogue"] = self._filled.as_dict()
        return out

    def design_schema(self, part: str = "design") -> dict[str, Any]:
        """Give the exact shape of a design, or of the operations that edit one.

        Args:
            part: ``design`` for what writing a design needs, ``patch``
                for the operations ``edit_design`` takes, ``board`` for the
                board ``make_board`` lays out, ``all`` for all three.

        Returns:
            The JSON schemas asked for, and with a design's the parameter
            names with units and the rules a schema cannot state.

        Raises:
            ToolError: ``E_SCHEMA_PART`` if ``part`` is not one of the four.
        """
        if part not in ("design", "patch", "board", "all"):
            raise ToolError(
                f"E_SCHEMA_PART: part is design, patch, board or all; got {part!r}"
            )
        return design_schema(cast("Any", part))

    def accuracy_report(self, design: dict[str, Any] | str) -> dict[str, Any]:
        """Report what is and is not established about a design.

        Args:
            design: The design, or the JSON text of it.

        Returns:
            The status, what to resolve, counts, results and tolerances.
        """
        grounded = self._grounded(self._design(design))
        _, results = evaluate_requirements(grounded, self._parts)
        simulators, missing = self._simulators()
        settings = self._workspace.settings
        try:
            registry = (
                ModelRegistry(settings.models_dir)
                if settings.models_dir is not None
                else None
            )
        except RegistryError:
            registry = None
        report = dataclasses.replace(
            accuracy_report(grounded.design, results),
            simulation=simulation_accuracy(
                grounded, simulators, missing, store=self._parts, registry=registry
            ),
        )
        return self._noted(report.as_dict())

    def _models(self) -> ModelRegistry | None:
        """The models folder the server was given, if any."""
        folder = self._workspace.settings.models_dir
        return ModelRegistry(folder) if folder is not None else None

    def _simulators(self) -> tuple[list[Simulator], list[str]]:
        """The simulators installed, and a line for each that is not."""
        settings = self._workspace.settings
        simulators: list[Simulator] = []
        missing: list[str] = []
        engines: tuple[tuple[Engine, Path | None], ...] = (
            ("ngspice", settings.ngspice),
            ("xyce", settings.xyce),
        )
        for engine, explicit in engines:
            found, searched = find_simulator(engine, explicit)
            if found is None:
                missing.append(f"{engine} (searched {'; '.join(searched)})")
            else:
                simulators.append(found)
        return simulators, missing

    # Propose tier

    def calc(self, name: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run one calculation from the catalogue.

        Args:
            name: The calculation's name.
            inputs: Each input by name: text with its unit and, if it
                has one, its tolerance (``10k 1%`` or ``3.3V +/-2%``),
                or an object with ``value``, and optionally
                ``tolerance`` and ``source``.

        Returns:
            The result with its accuracy block and worst case.
        """
        if name not in CATALOGUE:
            raise ToolError(
                f"E_CALC_UNKNOWN: no calculation is called {name!r}. Available: "
                f"{', '.join(sorted(CATALOGUE))}."
            )
        calculation = CATALOGUE[name]
        given: dict[str, Given] = {}
        for key, raw in inputs.items():
            if key not in calculation.inputs:
                takes = ", ".join(
                    f"{k} ({u or 'a number'})" for k, u in calculation.inputs.items()
                )
                raise ToolError(
                    f"E_CALC_INPUT: {name} takes {takes}; it has no input {key!r}."
                )
            unit = calculation.inputs[key]
            item: Mapping[str, Any] = (
                cast("Mapping[str, Any]", raw)
                if isinstance(raw, Mapping)
                else _value_and_tolerance(str(raw))
            )
            try:
                value = parse_quantity(
                    str(item.get("value", "")), unit, bare=unit in _BARE_UNITS
                )
                tolerance = (
                    parse_quantity(str(item["tolerance"]), "%").value
                    if item.get("tolerance") is not None
                    else None
                )
            except QuantityError as exc:
                raise ToolError(
                    f"E_CALC_INPUT: {key}: {exc}. Give an input as text with its "
                    "unit and tolerance, such as '10k 1%', or as "
                    "{value, tolerance, source}"
                ) from exc
            given[key] = Given(
                key,
                value,
                tolerance=tolerance,
                source=str(item.get("source", "user-stated"))
                if tolerance is not None
                else "missing",
            )
        try:
            return calculate(name, given).as_dict()
        except ValueError as exc:
            raise ToolError(f"E_CALC_INPUT: {exc}") from exc

    def analyze_power_tree(self, design: dict[str, Any] | str) -> dict[str, Any]:
        """Work out each rail's current and check every requirement.

        Args:
            design: The design, or the JSON text of it.

        Returns:
            The rails, the regulators and one result per requirement.
        """
        return self._noted(self._power(self._grounded(self._design(design))))

    def check_schematic(
        self, project: str, facts: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Check a schematic a person drew against the facts the caller states.

        The parts and connections are KiCad's netlist of the drawing. A
        net has no role, and a part no datasheet values, until ``facts``
        states them; see `kairo.kicad.drawn`.

        Args:
            project: The project, as the read tools take it.
            facts: Net roles and voltages, parts' parameters, loads and
                requirements.

        Returns:
            What was read, the nets with supply pins whose role is not
            stated, the rails, the regulators and one result per
            requirement.

        Raises:
            ToolError: ``E_FACTS_INVALID`` or ``E_DRAWN_UNSUPPORTED`` from
                the import; ``E_KICAD_UNAVAILABLE`` or a netlist error if
                KiCad cannot give the netlist.
        """
        root = self._workspace.schematic(project)
        facts, kept = self._stated(root, facts)
        try:
            drawn = read_drawn(
                self._workspace.netlist(root),
                design_name(root.stem),
                facts,
                self._catalogue(),
                self._parts,
            )
        except DrawnError as exc:
            raise ToolError(str(exc)) from exc
        design = drawn.grounded.design
        unstated = sorted(drawn.unstated.items())
        self._filled, self._remembered = drawn.filled, drawn.remembered
        return self._noted(
            {
                **kept,
                "read": {
                    "parts": len(design.components),
                    "nets": len(design.nets),
                    "pins_left_open_on_purpose": len(design.no_connect),
                    "part_numbers_from_the_drawing": len(drawn.numbered),
                },
                "unstated_supply_nets": {
                    "total": len(unstated),
                    "listed": [
                        {"net": net, "supply_pins": list(pins[:6]), "pins": len(pins)}
                        for net, pins in unstated[:UNSTATED_LISTED]
                    ],
                    "to_resolve": (
                        "State each one's role in facts.nets, with its voltage if "
                        "it is a power net; until then it is not a rail"
                        if unstated
                        else None
                    ),
                },
                **self._power(drawn.grounded),
            }
        )

    def _power(self, grounded: Grounded) -> dict[str, Any]:
        tree, results = evaluate_requirements(grounded, self._parts)
        return {
            "rails": {
                name: {
                    "voltage": f"{rail.voltage:g} V",
                    "current": f"{'>= ' if rail.is_bound else ''}"
                    f"{rail.current * 1000:.4g} mA",
                    "made_by": rail.source,
                    "draws": [
                        {"what": d.what, "current": f"{d.current * 1000:.4g} mA"}
                        for d in rail.draws
                    ],
                    "unknown": [
                        {"part": u.ref, "why": u.why, "to_resolve": u.resolve}
                        for u in rail.unknown
                    ],
                }
                for name, rail in tree.rails.items()
            },
            "regulators": [
                {
                    "ref": r.ref,
                    "kind": r.kind,
                    "from": r.input_net,
                    "makes": r.output_net,
                }
                for r in tree.regulators
            ],
            "requirements": [_brief(result) for result in results],
            "review": _review(grounded),
        }

    def plan_design(self, design: dict[str, Any] | str) -> dict[str, Any]:
        """Check a design fully without writing anything.

        Args:
            design: The design, or the JSON text of it.

        Returns:
            Whether it could be applied, the findings, what the sheet
            would hold, and each requirement's result.
        """
        parsed = self._design(design)
        findings: list[Finding] = []
        reached = ["L1"]
        try:
            grounded = ground(parsed, self._index())
        except GroundingError as exc:
            return _plan(make_report(list(exc.findings), reached).as_dict(), [], None)
        reached.append("L2")
        findings += check_structure(grounded)
        preview: dict[str, Any] | None = None
        try:
            written = write_schematic(grounded, self._index(), models=self._models())
        except WriterError as exc:
            findings.append(Finding("L2", "E_CANNOT_DRAW", "error", str(exc)))
        else:
            findings += [*written.findings, *check_layout(written.drawing)]
            preview = {
                "paper": written.paper,
                "parts": len(parsed.components),
                "nets": len(parsed.connections),
                "connected_pins": len(grounded.pins),
                "note": "nothing was written; KiCad's netlist and rule check "
                "run when the design is applied",
            }
        _, results = evaluate_requirements(grounded, self._parts)
        return self._noted(
            _plan(make_report(findings, reached).as_dict(), results, preview)
        )

    def simulate(
        self,
        design: dict[str, Any] | str,
        request: dict[str, Any],
        nets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Simulate a design and compare its response with design practice.

        Args:
            design: The design, or the JSON text of it.
            request: The analysis, sources and limits; see
                `kairo.spice.request.SimRequest`.
            nets: Only report metrics on these nets; all if ``None``.

        Returns:
            The status and what stands behind it first, then the limits,
            the practice remarks, the metrics, and where the graphs, the
            curves and the netlist were saved.

        Raises:
            ToolError: ``E_SIM_REQUEST_INVALID`` for a request that does
                not parse or does not fit the design, and
                ``E_SIMULATOR_UNAVAILABLE`` when ngspice is not found.
        """
        return self._simulate(self._grounded(self._design(design)), request, nets)

    def _simulate(
        self, grounded: Grounded, request: dict[str, Any], nets: list[str] | None
    ) -> dict[str, Any]:
        parsed = grounded.design
        try:
            asked = SimRequest.model_validate(request)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or 'request'}: "
                f"{error['msg']}"
                for error in exc.errors()[:10]
            )
            raise ToolError(f"E_SIM_REQUEST_INVALID: {problems}") from exc
        settings = self._workspace.settings
        simulators, missing = self._simulators()
        if not simulators or simulators[0].engine != "ngspice":
            raise ToolError(
                f"E_SIMULATOR_UNAVAILABLE: engine not installed: {missing[0]}. "
                "Install ngspice, or start the server with --ngspice PATH."
            )
        registry = (
            ModelRegistry(settings.models_dir)
            if settings.models_dir is not None
            else None
        )
        if asked.loop is not None:
            try:
                measured = measure_loop(
                    grounded, asked, simulators, store=self._parts, registry=registry
                )
            except CircuitError as exc:
                raise ToolError(f"E_SIM_REQUEST_INVALID: {exc}") from exc
            except RegistryError as exc:
                raise ToolError(str(exc)) from exc
            answer = measured.as_dict()
            if missing:
                answer["unverified_because"].append(
                    f"engine not installed: {missing[0]}"
                )
                if answer["status"] == "checked":
                    answer["status"] = "unverified"
            return answer
        try:
            run = worst_case if asked.sweep else simulate
            result = run(
                grounded, asked, simulators, store=self._parts, registry=registry
            )
        except CircuitError as exc:
            raise ToolError(f"E_SIM_REQUEST_INVALID: {exc}") from exc
        except RegistryError as exc:
            raise ToolError(str(exc)) from exc

        out = result.as_dict()
        if missing:
            out["notes"].append(
                f"engine not installed: {missing[0]}, so one simulator's "
                "numbers stand unchecked by another"
            )
            out["status"] = (
                "unverified" if out["status"] == "checked" else out["status"]
            )
        out["practice"] = [a.as_dict() for a in review(grounded, asked, result)]
        if asked.monte_carlo is not None and result.status != "failed":
            try:
                out["monte_carlo"] = monte_carlo(
                    grounded, asked, simulators, store=self._parts, registry=registry
                ).as_dict()
            except CircuitError as exc:
                raise ToolError(f"E_SIM_REQUEST_INVALID: {exc}") from exc
        wanted = [m for m in out["metrics"] if nets is None or m["where"] in nets]
        if len(wanted) > MAX_METRICS:
            out["metrics_omitted"] = len(wanted) - MAX_METRICS
            wanted = wanted[:MAX_METRICS]
        out["metrics"] = wanted

        number = self._simulations = self._simulations + 1
        folder = (
            settings.cache_dir
            / "simulations"
            / f"{parsed.design}-{asked.analysis.kind}-{number:03d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        files = dict(charts(result, parsed.design))
        curves = table(result)
        if curves:
            files["curves.csv"] = curves
        files["netlist.cir"] = netlist(
            result.circuit,
            "ngspice",
            commands(
                "ngspice",
                asked.analysis,
                ACCURATE,
                sweep_source(result.circuit, asked),
                asked.stimuli,
                pspice=result.circuit.pspice,
            ),
        )
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8", newline="\n")
        out["files"] = {"folder": str(folder), "saved": sorted(files)}
        return out

    def simulate_schematic(
        self,
        project: str,
        parts: list[str],
        request: dict[str, Any],
        facts: dict[str, Any] | None = None,
        nets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Simulate some parts of a schematic a person drew.

        Args:
            project: The project, as the read tools take it.
            parts: The references to simulate together; the rest of the
                board is left out, and what it draws from these parts'
                nets is whatever ``facts.loads`` states.
            request: The analysis, sources and limits, as `simulate` takes.
            facts: Net roles and voltages, parts' parameters and loads,
                as `check_schematic` takes.
            nets: Only report metrics on these nets; all if ``None``.

        Returns:
            What `simulate` returns, with the parts simulated and the
            facts that did not apply to them.

        Raises:
            ToolError: ``E_FACTS_INVALID`` or ``E_DRAWN_UNSUPPORTED`` from
                the import, and what `simulate` raises.
        """
        root = self._workspace.schematic(project)
        facts, saved = self._stated(root, facts)
        try:
            smaller, kept, left_out = restrict(
                self._workspace.netlist(root), parts, facts
            )
            drawn = read_drawn(
                smaller,
                design_name(root.stem),
                kept,
                self._catalogue(),
                self._parts,
            )
        except DrawnError as exc:
            raise ToolError(str(exc)) from exc
        out = self._simulate(drawn.grounded, request, nets)
        self._filled, self._remembered = drawn.filled, drawn.remembered
        out = self._noted(out)
        out.update(saved)
        out["simulated"] = {
            "parts": sorted(drawn.grounded.design.components),
            "nets": sorted(drawn.grounded.design.nets),
            "facts_left_out": left_out,
            "note": "Only these parts are in the circuit. Whatever else the "
            "board draws from their nets is there only if facts.loads states it.",
        }
        return out

    def verify_design(self, folder: str) -> dict[str, Any]:
        """Check a project this server built, against its design.

        Args:
            folder: The project folder.

        Returns:
            The verifier's report: each layer's status and the findings,
            and how KiCad's own SPICE netlist of the project compares
            with the design's resistors, capacitors and inductors.
        """
        path, session = self._session(folder, must_exist=True)
        design = session.designs[-1]
        cli = self._workspace.kicad()
        report = verify_project(
            design, path, self._index(), cli, models=self._models()
        ).as_dict()
        report["spice_netlist"] = self._spice_cross_check(design, path)
        return report

    def _spice_cross_check(self, design: Design, path: Path) -> dict[str, Any]:
        """Compare the design's passives with KiCad's SPICE netlist of the project."""
        try:
            grounded = ground(design, self._index())
            names = write_schematic(
                grounded, self._index(), models=self._models()
            ).net_names
        except (GroundingError, WriterError) as exc:
            return {"status": "unavailable", "why": str(exc)}
        exported = self._workspace.kicad().export_netlist(
            path / f"{design.design}.kicad_sch", fmt="spice"
        )
        if exported.text is None:
            return {
                "status": "unavailable",
                "why": f"KiCad gave no SPICE netlist: {exported.reason}",
            }
        differences = cross_check(grounded, names, exported.text)
        return {
            "status": "differs" if differences else "agrees",
            "compared": "each two-pin resistor, capacitor and inductor: its "
            "two nets and its value, between the design and the SPICE "
            "netlist KiCad writes from the schematic file",
            "differences": differences,
        }

    # Write tier

    def apply_design(self, design: dict[str, Any] | str, folder: str) -> dict[str, Any]:
        """Build a design into a project folder and check it with KiCad.

        Args:
            design: The design, or the JSON text of it.
            folder: The project folder, under an allowed root. It must be
                new, or a project this server built.

        Returns:
            Whether it passed, what was written and each check's result.
            A design that fails a check, or whose requirement cannot be
            met, writes nothing.
        """
        path, session = self._session(folder, must_exist=False)
        return self._noted(self._write(path, session, self._design(design)))

    def edit_design(
        self, folder: str, operations: list[dict[str, Any]] | str
    ) -> dict[str, Any]:
        """Change a project this server built, by patch operations.

        Args:
            folder: The project folder.
            operations: The operations, as ``design_schema`` describes,
                or the JSON text of the list.

        Returns:
            As for ``apply_design``, with the lines changed per file.
        """
        listed = json_list(operations, "operations")
        path, session = self._session(folder, must_exist=True)
        try:
            changed = apply_patch(session.designs[-1], parse_patch(listed))
        except PatchError as exc:
            raise ToolError(str(exc)) from exc
        return self._write(path, session, changed)

    def _stated(
        self, root: Path, given: dict[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The facts kept with a project with a call's laid over them.

        Returns the facts to use, and for the result a line naming the
        kept file when there is one.
        """
        try:
            kept = load_facts(root.parent)
        except FactsFileError as exc:
            raise ToolError(str(exc)) from exc
        note: dict[str, Any] = {}
        if kept:
            note["kept_facts"] = {
                "file": str(root.parent / FACTS_FILE),
                "note": "Facts kept with the project were used, with any given "
                "in this call laid over them",
            }
        return laid_over(kept, given or {}), note

    def save_facts(
        self, project: str, facts: dict[str, Any], replace: bool = False
    ) -> dict[str, Any]:
        """Keep facts about a schematic a person drew in the project itself.

        Args:
            project: The project, as the read tools take it.
            facts: The facts, as `check_schematic` takes them.
            replace: Keep only these, dropping what was kept before; by
                default they are laid over what is kept.

        Returns:
            Where they are kept and how much they hold.

        Raises:
            ToolError: ``E_FACTS_INVALID`` if the facts do not fit the
                schematic, so nothing is written; ``E_FACTS_FILE`` if the
                kept file cannot be read; ``E_FILE_CHANGED`` if it changed
                while this ran.
        """
        root = self._workspace.schematic(project)
        folder = root.parent
        try:
            kept = {} if replace else load_facts(folder)
        except FactsFileError as exc:
            raise ToolError(str(exc)) from exc
        merged = laid_over(kept, facts)
        try:
            # The same reading check_schematic makes, so facts that name
            # a net or part the drawing lacks are refused before writing.
            read_drawn(
                self._workspace.netlist(root),
                design_name(root.stem),
                merged,
                self._catalogue(),
                self._parts,
            )
        except DrawnError as exc:
            raise ToolError(str(exc)) from exc
        session = self._edits.setdefault(folder, Session(folder))
        name = FACTS_FILE.as_posix()
        try:
            if (folder / FACTS_FILE).exists():
                session.read(name)
            report = session.write({name: facts_text(merged)})
        except TransactionError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "written": report.written,
            "file": str(folder / FACTS_FILE),
            "holds": {
                key: len(cast("dict[str, Any] | list[Any]", value))
                if isinstance(value, (dict, list))
                else 1
                for key, value in sorted(merged.items())
            },
            "note": "check_schematic and simulate_schematic now use these "
            "without being given them; facts given in a call are laid over them",
        }

    def set_value(self, project: str, ref: str, value: str) -> dict[str, Any]:
        """Change one part's value in a schematic a person drew.

        Args:
            project: The project, as the read tools take it.
            ref: The part's reference.
            value: The new value.

        Returns:
            What changed, file by file with the count of changed lines,
            the old value, and what KiCad confirmed.

        Raises:
            ToolError: ``E_REF_NOT_FOUND``, ``E_SHARED_SHEET`` or
                ``E_NOT_A_SCHEMATIC`` from the edit; ``E_KICAD_UNAVAILABLE``
                if KiCad cannot give the netlist to compare with;
                ``E_FILE_CHANGED`` if a file changed while this ran.
        """
        root = self._workspace.schematic(project)
        folder = root.parent
        cli = self._workspace.kicad()
        before = cli.export_netlist(root)
        if before.text is None:
            raise ToolError(
                f"E_KICAD_UNAVAILABLE: KiCad gave no netlist for {root.name} "
                "to compare with, so nothing was changed"
            )
        nets_before = _connections(before.text)
        values_before = _values(before.text)
        if ref not in values_before:
            raise ToolError(f"E_REF_NOT_FOUND: KiCad's netlist has no part {ref}")
        erc_before = _erc_counts(cli.run_erc(root))
        try:
            edit = set_value(root, ref, value)
        except EditError as exc:
            raise ToolError(str(exc)) from exc
        session = self._edits.setdefault(folder, Session(folder))
        try:
            for name in edit.files:
                session.read(name)
        except TransactionError as exc:
            raise ToolError(str(exc)) from exc

        def check(_: Path) -> list[str]:
            after = cli.export_netlist(root)
            if after.text is None:
                return ["KiCad gave no netlist for the changed schematic"]
            problems: list[str] = []
            if _connections(after.text) != nets_before:
                problems.append("the connections changed")
            wanted = {**values_before, ref: value}
            got = _values(after.text)
            differing = sorted(r for r in wanted if got.get(r) != wanted[r])
            if differing or set(got) != set(wanted):
                problems.append(
                    f"parts other than {ref} differ, or {ref} did not take the "
                    f"value: {', '.join(differing[:5]) or 'the set of parts changed'}"
                )
            erc_after = _erc_counts(cli.run_erc(root))
            if erc_after != erc_before:
                problems.append(
                    f"the rule check changed: {erc_before} before, {erc_after} after"
                )
            return problems

        try:
            report = session.write(edit.files, check=check)
        except TransactionError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "written": report.written,
            "ref": ref,
            "old_value": list(edit.old),
            "new_value": value,
            "symbols_changed": edit.symbols,
            "files": [
                {
                    "file": str(change.path),
                    "changed_lines": change.changed_lines,
                    "total_lines": change.previous_lines,
                }
                for change in report.changes
            ],
            "problems": [str(problem) for problem in report.problems],
            "confirmed": (
                "KiCad's netlist has the same connections and the same parts, "
                f"with only {ref}'s value different, and its rule check gives "
                "the same counts as before"
                if report.written
                else "nothing was changed: the file was put back"
            ),
            "undo": "undo with this project's folder puts the old value back",
        }

    def edit_schematic(
        self,
        project: str,
        operations: list[dict[str, Any]] | str,
        max_changed_ratio: float | None = None,
    ) -> dict[str, Any]:
        """Add, remove or reconnect parts in a schematic a person drew.

        Args:
            project: The project, as the read tools take it.
            operations: The changes (or the JSON text of the list), applied
                in order and written together or not at all. Each has
                ``op``: ``add_part`` (with ``ref``, ``lib_id``, ``value``,
                ``pins`` and optionally
                ``footprint``, ``no_connect``, ``beside``, ``sheet``),
                ``remove_part`` (with ``ref``) or ``reconnect`` (with
                ``pin`` and ``net``).
            max_changed_ratio: The most of any one file that may change,
                as changed lines over the file's lines; a write beyond it
                is refused. ``None`` for no bound.

        Returns:
            Whether the change was written, what each operation did, what
            changed file by file, and what KiCad confirmed.

        Raises:
            ToolError: ``E_OPERATION`` for an operation that is not shaped
                as above; an edit's own code (see `kairo.kicad.drawn_edit`);
                ``E_KICAD_UNAVAILABLE`` if KiCad cannot give the netlist to
                compare with; ``E_FILE_CHANGED`` if a file changed while
                this ran.
        """
        operations = json_list(operations, "operations")
        if not operations:
            raise ToolError("E_OPERATION: no operations were given")
        root = self._workspace.schematic(project)
        folder = root.parent
        cli = self._workspace.kicad()
        before = cli.export_netlist(root)
        if before.text is None:
            raise ToolError(
                f"E_KICAD_UNAVAILABLE: KiCad gave no netlist for {root.name} "
                "to compare with, so nothing was changed"
            )
        expected = {name: set(pins) for name, pins in _connections(before.text).items()}
        values = _values(before.text)
        named: dict[tuple[str, str], str] = {}
        done: list[dict[str, Any]] = []
        try:
            drawn = Project(root)
            for number, operation in enumerate(operations, 1):
                done.append(
                    _apply_operation(
                        drawn, self._index, operation, number, expected, values, named
                    )
                )
        except EditError as exc:
            raise ToolError(str(exc)) from exc
        files = drawn.files()
        # The baseline: what KiCad's rule check finds before anything is
        # written. A real board has findings of its own, and only new
        # ones count against an edit.
        erc_before = _erc_counts(cli.run_erc(root))
        session = self._edits.setdefault(folder, Session(folder))
        changes: list[dict[str, Any]] = []
        over: list[str] = []
        try:
            for name in files:
                held = session.read(name)
                sheet = drawn.sheets[(drawn.folder / name).resolve()]
                if held != sheet.original:
                    raise ToolError(
                        f"E_FILE_CHANGED: {name} changed on disk while the edit "
                        "was worked out, so nothing was written. Call again."
                    )
                change = file_change(folder / name, held.encode("utf-8"), files[name])
                changes.append(
                    {
                        "file": name,
                        "changed_lines": change.changed_lines,
                        "total_lines": change.previous_lines,
                        "ratio": round(change.ratio or 0.0, 4),
                    }
                )
                if max_changed_ratio is not None and (change.ratio or 0.0) > (
                    max_changed_ratio
                ):
                    over.append(
                        f"{name}: {change.changed_lines} of {change.previous_lines} "
                        f"lines would change ({change.ratio:.1%}), over the bound "
                        f"of {max_changed_ratio:.1%}"
                    )
        except TransactionError as exc:
            raise ToolError(str(exc)) from exc
        if over:
            return {
                "written": False,
                "operations": done,
                "files": changes,
                "problems": over,
                "confirmed": "nothing was written: the change is larger than the "
                "bound given. Narrow it, or give a larger bound.",
            }

        def check(_: Path) -> list[str]:
            after = cli.export_netlist(root)
            if after.text is None:
                return ["KiCad gave no netlist for the changed schematic"]
            got = _connections(after.text)
            problems = _connection_problems(expected, got)
            for (ref, pin), net in sorted(named.items()):
                if (ref, pin) not in got.get(net, frozenset()):
                    found = [n for n, pins in got.items() if (ref, pin) in pins]
                    problems.append(
                        f"{ref} pin {pin} is on {found[0] if found else 'no net'}, "
                        f"not on {net}"
                    )
            if _values(after.text) != values:
                now = _values(after.text)
                differing = sorted(
                    r for r in values.keys() | now.keys() if values.get(r) != now.get(r)
                )
                problems.append(
                    f"the parts are not as expected: {', '.join(differing[:8])}"
                )
            erc_after = _erc_counts(cli.run_erc(root))
            worse = [
                f"{kind} ({erc_before.get(kind, 0)} before, {count} after)"
                for kind, count in erc_after.items()
                if count > erc_before.get(kind, 0)
            ]
            if worse:
                problems.append(
                    "the rule check has violations it did not have before: "
                    + ", ".join(worse)
                )
            return problems

        try:
            report = session.write(files, check=check)
        except TransactionError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "written": report.written,
            "operations": done,
            "files": changes,
            "problems": [str(problem) for problem in report.problems],
            "confirmed": (
                "KiCad's netlist has exactly the connections these operations "
                "should give, every other part as it was, and its rule check has "
                "no violation it did not have before"
                if report.written
                else "nothing was changed: the files were put back"
            ),
            "to_see_it": "KiCad 10 does not reload a schematic that changed on "
            "disk: revert or reopen the sheet",
            "undo": "undo with this project's folder puts the files back",
        }

    def make_board(
        self,
        folder: str,
        design: dict[str, Any] | str | None = None,
        passes: int = 30,
        effort: float = 1.0,
        replace_places: bool = False,
        export: bool = True,
        engine: str = "kairo",
    ) -> dict[str, Any]:
        """Lay out, route, check and save a built project's board.

        Args:
            folder: The built project's folder.
            design: The design, if this server did not build the project
                in this run; else the design it last applied.
            passes: The most router passes.
            effort: A multiplier on the placer's moves.
            replace_places: Place afresh rather than keep the places the
                last call saved.
            export: Write the fab files when nothing blocks fabrication.
            engine: ``kairo`` (the default) or ``freerouting``.

        Returns:
            The summary, the files written and what to do next.
        """
        path = self._workspace.allowed(folder)
        session = self._sessions.get(path)
        if design is not None:
            chosen = self._design(design)
        elif session is not None and session.designs:
            chosen = session.designs[-1]
        else:
            raise ToolError(
                "E_NO_DESIGN: give the design, or build the project with "
                "apply_design in this run first."
            )
        if chosen.board is None:
            raise ToolError(
                "E_NO_BOARD: the design states no board; add board with its "
                "width, height, sides, edges, pours, net_classes and rules."
            )
        files = session.files if session is not None else Session(path)
        settings = self._workspace.settings
        tools = settings.tools_dir or settings.cache_dir / "tools"
        places = None if replace_places else kept_places(path)
        try:
            made = make_board(
                chosen,
                path,
                self._index(),
                self._workspace.kicad(),
                tools,
                passes=passes,
                effort=effort,
                places=places,
                engine=engine,
            )
            summary = summarize(made)
            written = save_board(files, chosen, made)
            fab: list[str] = []
            unsourced: list[str] = []
            if export and summary["ready"]:
                catalogue = self._catalogue()

                def lcsc_for(mpn: str) -> str | None:
                    found = catalogue.lookup(mpn) if catalogue else None
                    return f"C{found.lcsc}" if found else None

                _, fab, unsourced = export_fab(
                    files, chosen, self._workspace.kicad(), lcsc_for
                )
        except (LayoutError, RoutingError, TransactionError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            **summary,
            "kept_places": places is not None,
            "written": [change.path.name for change in written.changes],
            "fab": fab,
            # JLCPCB orders by LCSC number: parts with none are to be sourced.
            **(
                {
                    "jlcpcb_unsourced": unsourced[:5],
                    "jlcpcb_unsourced_count": len(unsourced),
                }
                if unsourced
                else {}
            ),
            "next": next_step(summary, chosen),
        }

    def undo(self, folder: str) -> dict[str, Any]:
        """Put a project back as it was before the last write.

        Args:
            folder: The project folder.

        Returns:
            The files restored or removed.
        """
        edited = self._edits.get(self._workspace.allowed(folder))
        if edited is not None:
            # A schematic a person drew, changed by set_value.
            try:
                restored = edited.undo()
            except TransactionError as exc:
                raise ToolError(str(exc)) from exc
            return {
                "undone": True,
                "files": [str(path) for path in restored],
                "design": None,
            }
        _, session = self._session(folder, must_exist=True)
        try:
            restored = session.files.undo()
        except TransactionError as exc:
            raise ToolError(str(exc)) from exc
        session.designs.pop()
        session.stalled = 0
        return {
            "undone": True,
            "files": [str(path) for path in restored],
            "design": session.designs[-1].design if session.designs else None,
        }

    # Shared

    def _write(
        self, path: Path, session: DesignSession, design: Design
    ) -> dict[str, Any]:
        if session.attempts >= MAX_ROUNDS:
            raise ToolError(
                f"E_ROUND_LIMIT: {MAX_ROUNDS} write attempts have been made on "
                f"{path.name} in this run. Stop and tell the user what is "
                "still failing."
            )
        if session.stalled >= STALL_ROUNDS:
            raise ToolError(
                f"E_NO_PROGRESS: the last {STALL_ROUNDS} attempts on {path.name} "
                "failed without improving. Stop and tell the user what is "
                "failing; do not try again without their answer."
            )
        session.attempts += 1
        try:
            grounded = ground(design, self._index())
        except GroundingError as exc:
            report = make_report(list(exc.findings), ["L1"])
            return self._failed(session, report.as_dict(), report.score)
        _, results = evaluate_requirements(grounded, self._parts)
        broken = [result for result in results if result.status == "fail"]
        if broken:
            # A requirement the design cannot meet: say so with the
            # numbers, and write nothing.
            session.stalled += 1
            return {
                "written": False,
                "code": "E_REQUIREMENT_UNSATISFIABLE",
                "message": "A requirement cannot be met by this design. Nothing "
                "was written. Tell the user, with the arithmetic below; do not "
                "loosen the requirement.",
                "failed_requirements": [_detail(result) for result in broken],
                "attempts": {"made": session.attempts, "limit": MAX_ROUNDS},
            }
        accuracy = accuracy_report(design, results)
        try:
            built = build_project(
                design,
                path,
                self._index(),
                self._workspace.kicad(),
                session=session.files,
                extra={REPORT_FILE: accuracy.as_markdown()},
                models=self._models(),
            )
        except TransactionError as exc:
            raise ToolError(str(exc)) from exc
        if not built.passed:
            return self._failed(session, built.report.as_dict(), built.report.score)
        session.designs.append(design)
        session.best_score, session.stalled = 1.0, 0
        return {
            "written": True,
            "accuracy": {
                "status": accuracy.status,
                "to_resolve": [item["supply"] for item in accuracy.to_resolve],
                "counts": accuracy.counts,
                "report_file": str(path / REPORT_FILE),
            },
            "files": [
                {
                    "file": str(change.path),
                    "created": change.created,
                    "changed_lines": change.changed_lines,
                    "changed_ratio": None
                    if change.ratio is None
                    else round(change.ratio, 4),
                }
                for change in built.changes
            ],
            "verifier": built.report.as_dict(),
            "requirements": [_brief(result) for result in results],
            "attempts": {"made": session.attempts, "limit": MAX_ROUNDS},
            # The same design drawn with wires, for a person to read; the
            # project's sheet is the one every check is built on.
            "wired_drawing": next(
                (str(p) for p in built.wired if p.suffix == ".kicad_sch"), None
            ),
        }

    def _failed(
        self, session: DesignSession, report: dict[str, Any], score: float
    ) -> dict[str, Any]:
        if score > session.best_score:
            session.best_score, session.stalled = score, 0
        else:
            session.stalled += 1
        stop = session.stalled >= STALL_ROUNDS or session.attempts >= MAX_ROUNDS
        return {
            "written": False,
            "stop": stop,
            "message": "Nothing was written. "
            + (
                "This is the last attempt allowed without the user: tell them "
                "what is failing."
                if stop
                else "Fix the first findings and try again."
            ),
            "verifier": report,
            "attempts": {
                "made": session.attempts,
                "limit": MAX_ROUNDS,
                "without_improvement": session.stalled,
            },
        }

    def _design(self, given: dict[str, Any] | str) -> Design:
        design = json_object(given, "design")
        # A part that names its part number takes the figures the
        # catalogue lists for it, where the design states none.
        # What was saved from a datasheet comes before a listing.
        design, self._remembered = fill_from_store(design, self._parts)
        self._filled = None
        catalogue = self._catalogue()
        if catalogue is not None:
            design, self._filled = fill(design, catalogue)
        try:
            return Design.model_validate(design)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or 'design'}: "
                f"{error['msg']}"
                for error in exc.errors()[:10]
            )
            raise ToolError(
                f"E_DESIGN_INVALID: {problems}. A design is shaped like "
                f"{json.dumps(EXAMPLE, separators=(',', ':'))}: nets, components "
                "and connections are objects keyed by name, not lists. "
                "design_schema with part 'design' gives the exact shape."
            ) from exc

    def _grounded(self, design: Design) -> Grounded:
        try:
            return ground(design, self._index())
        except GroundingError as exc:
            listed = "; ".join(finding.message for finding in exc.findings[:5])
            raise ToolError(f"{exc.findings[0].code}: {listed}") from exc

    def _index(self) -> Any:
        return self._workspace.library()

    def _session(self, folder: str, *, must_exist: bool) -> tuple[Path, DesignSession]:
        path = self._workspace.allowed(folder)
        session = self._sessions.get(path)
        if session is None or (must_exist and not session.designs):
            if must_exist:
                raise ToolError(
                    f"E_NO_SESSION: {path} was not built by this server in this "
                    "run. Kairo edits only designs it has applied; apply_design "
                    "first."
                )
            session = DesignSession(Session(path))
            self._sessions[path] = session
        return path, session


def _review(grounded: Grounded) -> dict[str, Any]:
    """Kairo's faults and suggestions for a design, a bounded number of each."""
    found = review_design(grounded)
    out: dict[str, Any] = {
        "faults": {
            "total": len(found.faults),
            "listed": [r.as_dict() for r in found.faults[:REMARKS_LISTED]],
        },
        "suggestions": {
            "total": len(found.suggestions),
            "listed": [r.as_dict() for r in found.suggestions[:REMARKS_LISTED]],
        },
        "note": (
            "Neither kind blocks a write. A fault does not work as drawn; a "
            "suggestion is where a sound design could be done Kairo's way."
        ),
    }
    if found.skipped:
        out["rules_not_run"] = (
            f"{found.skipped} rules need to know which nets are supplies and "
            "were not run: state net roles first"
        )
    return out


def _brief(result: RequirementResult) -> dict[str, Any]:
    return {
        "requirement": result.index,
        "type": result.type,
        "status": result.status,
        "to_resolve": list(result.to_resolve),
        "summary": result.summary,
    }


def _detail(result: RequirementResult) -> dict[str, Any]:
    return {
        **_brief(result),
        "arithmetic": [
            {
                "equation": calculation.equation,
                "inputs": calculation.as_dict()["inputs"],
                "value": calculation.text(),
                "corners": calculation.as_dict()["corners"],
            }
            for calculation in result.results
        ],
    }


def json_object(value: dict[str, Any] | str, name: str) -> dict[str, Any]:
    """An object argument, read from JSON text if it came as text.

    Small local models often send an object as the JSON string of it,
    which the tool's own argument check would refuse before Kairo saw it.

    Args:
        value: The argument as sent.
        name: Its name, for the message.

    Returns:
        The object.

    Raises:
        ToolError: ``E_JSON_INVALID`` if the text is not JSON, saying where
            it breaks, or holds something other than an object.
    """
    read = _read_json(value, name)
    if not isinstance(read, dict):
        raise ToolError(
            f"E_JSON_INVALID: {name} must be an object; the text held "
            f"{type(read).__name__}"
        )
    return cast("dict[str, Any]", read)


def json_list(value: list[dict[str, Any]] | str, name: str) -> list[dict[str, Any]]:
    """A list argument, read from JSON text if it came as text.

    Args:
        value: The argument as sent.
        name: Its name, for the message.

    Returns:
        The list.

    Raises:
        ToolError: ``E_JSON_INVALID`` as for `json_object`, or if the text
            holds something other than a list.
    """
    read = _read_json(value, name)
    if not isinstance(read, list):
        raise ToolError(
            f"E_JSON_INVALID: {name} must be a list; the text held "
            f"{type(read).__name__}"
        )
    return cast("list[dict[str, Any]]", read)


def _read_json(value: Any, name: str) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        # The text either side of the fault is what a model can act on; a
        # character count alone it would have to count out.
        before = value[max(exc.pos - 40, 0) : exc.pos]
        after = value[exc.pos : exc.pos + 20]
        raise ToolError(
            f"E_JSON_INVALID: {name} came as text that is not JSON: {exc.msg} "
            f"at character {exc.pos} (line {exc.lineno}, column {exc.colno}), "
            f"after {before!r} and before {after!r}. Sending {name} as JSON "
            "itself, not a string of it, avoids this."
        ) from exc


def _plan(
    verifier: dict[str, Any],
    results: list[RequirementResult],
    preview: dict[str, Any] | None,
) -> dict[str, Any]:
    failed = [r for r in results if r.status == "fail"]
    errors = sum(layer["errors"] for layer in verifier["layers"].values())
    return {
        "can_apply": not errors and not failed and preview is not None,
        "unverified": [r.type for r in results if r.status == "unverified"],
        "requirements": [
            _detail(r) if r.status == "fail" else _brief(r) for r in results
        ],
        "verifier": verifier,
        "preview": preview,
    }


def _listed(part: CataloguePart) -> dict[str, Any]:
    """A catalogue part as a tool shows it: only the figures it has."""
    units = {"resistor": "ohm", "capacitor": "F", "inductor": "H"}
    out: dict[str, Any] = {
        "mpn": part.mpn,
        "lcsc": f"C{part.lcsc}",
        "manufacturer": part.manufacturer,
        "package": part.package,
    }
    if part.value is not None and part.kind in units:
        out["value"] = f"{part.value:.6g} {units[part.kind]}"
    figures = (
        ("tolerance", part.tolerance, "%"),
        ("v_rating", part.v_rating, " V"),
        ("p_rating", part.p_rating, " W"),
        ("i_sat", part.i_sat, " A"),
        ("i_rating", part.i_rating, " A"),
        ("dcr", part.dcr, " ohm"),
        ("esr_listed", part.esr, " ohm"),
        ("tempco", part.tempco, " ppm/degC"),
        ("v_f", part.v_f, " V"),
        ("v_f_tolerance", part.v_f_tolerance, "%"),
    )
    for name, figure, unit in figures:
        if figure is not None:
            out[name] = f"{figure:.4g}{unit}"
    if part.dielectric:
        out["dielectric"] = part.dielectric
    if part.kind == "regulator":
        out["topology"] = part.topology
        if part.v_out_min is not None and part.v_out_max is not None:
            out["output"] = (
                f"{part.v_out_max:.4g} V"
                if part.v_out_min == part.v_out_max
                else f"{part.v_out_min:.4g} V to {part.v_out_max:.4g} V, adjustable"
            )
        listed = (
            ("i_out_listed", part.i_out, 1.0, " A"),
            ("v_in_min_listed", part.v_in_min, 1.0, " V"),
            ("v_in_max_listed", part.v_in_max, 1.0, " V"),
            ("dropout_listed", part.v_dropout, 1.0, " V"),
            ("i_q_listed", part.i_q, 1e6, " uA"),
            ("f_sw_listed", part.f_sw, 1e-3, " kHz"),
        )
        for name, figure, scale, unit in listed:
            if figure is not None:
                out[name] = f"{figure * scale:.4g}{unit}"
        if part.datasheet:
            out["datasheet"] = part.datasheet
    out["stock"] = part.stock
    out["basic_part"] = part.basic
    return out


def _connections(netlist_text: str) -> dict[str, frozenset[tuple[str, str]]]:
    """A KiCad netlist's nets, each as the set of pins on it."""
    return {
        net.name: frozenset((node.ref, node.pin) for node in net.nodes)
        for net in parse_netlist(netlist_text).nets
    }


def _apply_operation(
    drawn: Project,
    index: Callable[[], LibraryIndex],
    operation: dict[str, Any],
    number: int,
    expected: dict[str, set[tuple[str, str]]],
    values: dict[str, str],
    named: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Apply one operation, and bring what KiCad should then report up to date."""

    def text(key: str, required: bool = True) -> str | None:
        value = operation.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value:
            raise ToolError(
                f"E_OPERATION: operation {number} needs {key} as text; got {value!r}"
            )
        return value

    kind = operation.get("op")
    if kind == "remove_part":
        ref = cast("str", text("ref"))
        removed = remove_part(drawn, ref)
        for pins in expected.values():
            pins.difference_update({pin for pin in pins if pin[0] == ref})
        values.pop(ref, None)
        for pin in [pin for pin in named if pin[0] == ref]:
            del named[pin]
        return {"op": kind, **asdict(removed)}
    if kind == "reconnect":
        pin = cast("str", text("pin"))
        net = cast("str", text("net"))
        moved = reconnect(drawn, pin, net)
        ref, _, pin_number = pin.rpartition(".")
        for pins in expected.values():
            pins.discard((ref, pin_number))
        expected.setdefault(net, set()).add((ref, pin_number))
        named[(ref, pin_number)] = net
        return {"op": kind, **asdict(moved)}
    if kind == "add_part":
        ref = cast("str", text("ref"))
        pins_given = operation.get("pins")
        open_given = operation.get("no_connect", [])
        if not isinstance(pins_given, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in cast("dict[object, object]", pins_given).items()
        ):
            raise ToolError(
                f"E_OPERATION: operation {number} needs pins as an object of pin "
                "name or number to net name"
            )
        if not isinstance(open_given, list) or not all(
            isinstance(item, str) for item in cast("list[object]", open_given)
        ):
            raise ToolError(
                f"E_OPERATION: operation {number} needs no_connect as a list of pins"
            )
        added = add_part(
            drawn,
            index(),
            ref=ref,
            lib_id=cast("str", text("lib_id")),
            value=cast("str", text("value")),
            footprint=text("footprint", required=False),
            pins=cast("dict[str, str]", pins_given),
            no_connect=cast("list[str]", open_given),
            beside=text("beside", required=False),
            sheet=text("sheet", required=False),
        )
        for pin_number, (net, _) in added.endings.items():
            expected.setdefault(net, set()).add((ref, pin_number))
            named[(ref, pin_number)] = net
        for pin_number in added.open_pins:
            expected[f"(open) {ref}.{pin_number}"] = {(ref, pin_number)}
        values[ref] = cast("str", operation["value"])
        return {"op": kind, **asdict(added)}
    raise ToolError(
        f"E_OPERATION: operation {number} has op {kind!r}; it is add_part, "
        "remove_part or reconnect"
    )


def _connection_problems(
    expected: Mapping[str, set[tuple[str, str]]],
    got: Mapping[str, frozenset[tuple[str, str]]],
) -> list[str]:
    """How KiCad's nets differ from the ones expected, whatever their names."""
    want = {frozenset(pins) for pins in expected.values() if pins}
    have = {pins for pins in got.values() if pins}
    problems: list[str] = []

    def show(pins: frozenset[tuple[str, str]]) -> str:
        some = sorted(pins)[:6]
        more = f" and {len(pins) - 6} more" if len(pins) > 6 else ""
        return ", ".join(f"{ref}.{pin}" for ref, pin in some) + more

    for pins in sorted(want - have, key=sorted)[:5]:
        problems.append(f"KiCad has no net joining exactly: {show(pins)}")
    for pins in sorted(have - want, key=sorted)[:5]:
        problems.append(f"KiCad has a net that was not expected: {show(pins)}")
    return problems


def _values(netlist_text: str) -> dict[str, str]:
    """Each part's value in a KiCad netlist."""
    return {c.ref: c.value for c in parse_netlist(netlist_text).components}


def _erc_counts(result: ErcResult) -> dict[str, int]:
    """How many violations of each kind a rule check found, waived ones aside."""
    counts: dict[str, int] = {}
    for violation in result.violations:
        if not violation.excluded:
            key = f"{violation.severity}:{violation.type}"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# A tolerance follows its value after a space or a plus-minus sign, so a
# percentage on its own (an efficiency of 90%) stays a value.
_WITH_TOLERANCE = re.compile(
    r"^(?P<value>\S.*?)(?:\s+(?:\+/-|\u00b1)?|\s*(?:\+/-|\u00b1))\s*"
    r"(?P<tol>[0-9.]+\s*%)$"
)


def _value_and_tolerance(text: str) -> dict[str, Any]:
    """Split ``10k 1%`` or ``3.3V +/-2%`` into a value and its tolerance.

    Text that is a percentage alone, such as an efficiency of ``90%``, is
    a value with no tolerance.
    """
    found = _WITH_TOLERANCE.match(text.strip())
    if found is None or not re.search(r"[0-9]", found.group("value")):
        return {"value": text}
    return {"value": found.group("value"), "tolerance": found.group("tol")}


def _pages(pages: list[int] | int | str | None) -> list[int] | None:
    """Pages as a list, from a list, one number, or text like ``5-7, 9``.

    Raises:
        ToolError: ``E_PAGES_INVALID`` if the text is not page numbers.
    """
    if pages is None or isinstance(pages, list):
        return pages
    if isinstance(pages, int):
        return [pages]
    out: list[int] = []
    for part in pages.replace(" ", "").split(","):
        first, dash, last = part.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise ToolError(
                f"E_PAGES_INVALID: pages {pages!r} are not page numbers; give a "
                "list such as [5, 6], one number, or text such as '5-7'"
            )
        out.extend(range(int(first), int(last or first) + 1))
    return out
