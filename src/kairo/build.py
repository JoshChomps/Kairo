"""Take a design from description to a checked KiCad project on disk.

The verifier's layers run in order, and a layer that finds an error stops
the build there, so what comes back is a root cause:

- **L1** ground every symbol, footprint and pin in the installed
  libraries;
- **L2** check the circuit's structure, draw the schematic, and check the
  drawing can be read;
- **L0** write the files and read the schematic back;
- **L3** have KiCad export the written schematic's netlist and compare it
  with the design;
- **L4** have KiCad run its rule check.

L0 comes after L2 only because there is no file to read back until the
drawing exists. A project that fails a check is put back as it was.

Beside the project, in its ``wired`` folder, goes the same design drawn
for a person: a sheet whose pins are joined by wires rather than by a
label on every pin (the writer's ``wired`` style). It is checked as the
project is, its layout, KiCad's netlist of it against the design, and
KiCad's rule check, in a scratch folder first, and written with the
project only if it passes. A wired drawing that cannot be made or does not
pass is reported as a warning and left out; it never stops the project.
KiCad is the referee for L3 and L4; if it cannot be run, the build is not
a pass.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kairo import progress
from kairo.ir import Design
from kairo.kicad.cli import (
    LIBRARY_FINDINGS,
    ErcResult,
    ErcStatus,
    KicadCli,
    NetlistStatus,
)
from kairo.kicad.formats import SCHEMATIC, check_format
from kairo.kicad.netlist import NetlistFormatError, parse_netlist
from kairo.kicad.project import parse_project
from kairo.kicad.project_writer import write_project
from kairo.kicad.schematic_writer import WriterError, WrittenSchematic, write_schematic
from kairo.kicad.sexpr import parse
from kairo.kicad.transaction import FileChange, Session
from kairo.library.grounding import Grounded, GroundingError, ground
from kairo.library.index import LibraryIndex
from kairo.spice.registry import ModelRegistry
from kairo.verify.equivalence import compare_netlist
from kairo.verify.findings import Finding, Where
from kairo.verify.layout import check_layout
from kairo.verify.report import Report, make_report
from kairo.verify.structure import StructureRules, check_structure

#: The folder, inside the project's, that holds the drawing for a person.
WIRED_FOLDER = "wired"


@dataclass(frozen=True, slots=True)
class BuildResult:
    """How a build came out.

    Attributes:
        passed: True only if every layer ran and found no error.
        report: What each layer found.
        files: The files written and kept; empty when the build failed.
        erc: KiCad's rule check result, if that layer was reached.
        changes: What the write did to each file.
        wired: The wired drawing's files, written with the project; empty
            if it could not be made or did not pass.
    """

    passed: bool
    report: Report
    files: tuple[Path, ...] = ()
    erc: ErcResult | None = field(default=None)
    changes: tuple[FileChange, ...] = ()
    wired: tuple[Path, ...] = ()

    @property
    def findings(self) -> tuple[Finding, ...]:
        """Every finding, by layer."""
        return self.report.findings


def build_project(
    design: Design,
    folder: Path,
    index: LibraryIndex,
    cli: KicadCli,
    *,
    session: Session | None = None,
    rules: StructureRules | None = None,
    extra: Mapping[str, str] | None = None,
    models: ModelRegistry | None = None,
    wired: bool = True,
) -> BuildResult:
    """Write a KiCad project for a design and check it, layer by layer.

    Args:
        design: The design.
        folder: The folder for the project.
        index: The library index to ground the design in.
        cli: The kicad-cli to check the result with.
        session: The session on the folder to write through; a new one is
            used if none is given, which refuses to replace any file
            already there.
        rules: Which optional structure rules are on.
        extra: Other files to write with the project, by name within the
            folder, kept or put back together with it.
        models: The models folder, for KiCad's simulation fields on each
            part with a registered model.
        wired: Whether to draw the design for a person as well, in the
            project's ``wired`` folder.

    Returns:
        Whether it passed, the report, and the files kept.

    Raises:
        TransactionError: If the files cannot be written, for example
            because the project already exists and this session has not
            read it.
    """
    findings: list[Finding] = []
    reached: list[str] = ["L1"]

    def failed(erc: ErcResult | None = None) -> BuildResult:
        return BuildResult(False, make_report(findings, reached), erc=erc)

    progress.stage("checking every part and pin against the libraries")
    try:
        grounded = ground(design, index)
    except GroundingError as exc:
        findings += exc.findings
        return failed()

    reached.append("L2")
    progress.stage("checking the structure and drawing the sheet")
    findings += check_structure(grounded, rules)
    if _has_error(findings):
        return failed()
    # A design with sheets is drawn as a hierarchical project whose sheets
    # are already wired for a person; one without is one labelled sheet.
    try:
        if design.sheets:
            drawn = write_project(grounded, index, models=models)
            sheets = {
                name: drawn.files[name] for name in drawn.files if name != drawn.root
            }
            root_text, net_names = drawn.files[drawn.root], drawn.net_names
            findings += drawn.findings
            for one in drawn.sheets.values():
                findings += check_layout(one.drawing)
            wired = False
        else:
            written = write_schematic(grounded, index, models=models)
            sheets, root_text, net_names = {}, written.text, written.net_names
            findings += written.findings
            findings += check_layout(written.drawing)
    except WriterError as exc:
        findings.append(Finding("L2", "E_CANNOT_DRAW", "error", str(exc)))
        return failed()
    if _has_error(findings):
        return failed()

    share = Path(cli.command[0]).parent.parent / "share" / "kicad"
    template = share / "template" / "kicad.kicad_pro"
    try:
        project = parse_project(template.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        reached.append("L0")
        findings.append(
            Finding(
                "L0",
                "E_KICAD_TEMPLATE_UNAVAILABLE",
                "error",
                f"KiCad's new-project file {template} could not be used: {exc}",
            )
        )
        return failed()
    project.data.setdefault("meta", {})["filename"] = f"{design.design}.kicad_pro"

    names = (f"{design.design}.kicad_pro", f"{design.design}.kicad_sch")
    ran: list[ErcResult] = []

    def check(project_folder: Path) -> list[Finding]:
        sheet = project_folder / names[1]
        reached.append("L0")
        # What is on disk must read back as the schematics written.
        for name, expected in {names[1]: root_text, **sheets}.items():
            path = project_folder / name
            try:
                on_disk = path.read_bytes().decode("utf-8")
                check_format(parse(on_disk), SCHEMATIC)
            except (OSError, ValueError) as exc:
                return [Finding("L0", "E_FILE_UNREADABLE", "error", f"{name}: {exc}")]
            if on_disk != expected:
                return [
                    Finding(
                        "L0",
                        "E_FILE_DIFFERS",
                        "error",
                        f"{name} on disk is not what was written",
                    )
                ]
        reached.append("L3")
        progress.stage("comparing KiCad's netlist with the design")
        exported = cli.export_netlist(sheet)
        if exported.status is not NetlistStatus.OK or exported.text is None:
            return [
                Finding(
                    "L3",
                    "E_NETLIST_UNAVAILABLE",
                    "error",
                    f"KiCad could not export the netlist: {exported.reason}",
                )
            ]
        try:
            netlist = parse_netlist(exported.text)
        except NetlistFormatError as exc:
            return [
                Finding("L3", "E_NETLIST_MALFORMED", "error", f"KiCad's netlist: {exc}")
            ]
        differences = compare_netlist(grounded, net_names, netlist)
        if differences:
            return differences
        reached.append("L4")
        progress.stage("running KiCad's rule check")
        erc = cli.run_erc(sheet)
        ran.append(erc)
        return [] if erc.status is ErcStatus.CLEAN else _erc_findings(erc)

    companion: dict[str, str] = {}
    if wired:
        progress.stage("drawing the design wired, for a person, and checking it")
        companion, notes = _wired_drawing(
            grounded, index, cli, models, project.to_text(), names
        )
        findings += notes
    session = session or Session(folder)
    files = {
        names[0]: project.to_text(),
        names[1]: root_text,
        **sheets,
        **companion,
        **(extra or {}),
    }
    report = session.write(files, check)
    erc = ran[0] if ran else None
    if not report.written:
        findings += [p for p in report.problems if isinstance(p, Finding)]
        return failed(erc)
    return BuildResult(
        True,
        make_report(findings, reached),
        files=tuple(change.path for change in report.changes),
        erc=erc,
        changes=report.changes,
        wired=tuple(
            change.path
            for change in report.changes
            if change.path.parent.name == WIRED_FOLDER
        ),
    )


def _wired_drawing(
    grounded: Grounded,
    index: LibraryIndex,
    cli: KicadCli,
    models: ModelRegistry | None,
    project: str,
    names: tuple[str, str],
) -> tuple[dict[str, str], list[Finding]]:
    """Draw the design wired and check it in a scratch folder.

    Returns:
        The files to write, by name within the project folder, and what
        was found. The files are empty if the drawing could not be made or
        did not pass; every finding is then a warning.
    """
    try:
        written = write_schematic(grounded, index, models=models, style="wired")
    except WriterError as exc:
        return {}, [_wired_note(f"it could not be drawn: {exc}")]
    problems = [f for f in check_layout(written.drawing) if f.severity == "error"]
    notes = [
        Finding(f.layer, f.code, "warning", f"wired drawing: {f.message}", f.where)
        for f in written.findings
    ]
    if problems:
        return {}, [*notes, _wired_note(f"its layout failed: {problems[0].message}")]
    with tempfile.TemporaryDirectory(prefix="kairo-wired-") as scratch:
        sheet = Path(scratch) / names[1]
        sheet.write_bytes(written.text.encode("utf-8"))
        (Path(scratch) / names[0]).write_bytes(project.encode("utf-8"))
        failure = _judged(grounded, written, cli, sheet)
    if failure:
        return {}, [*notes, _wired_note(failure)]
    return {
        f"{WIRED_FOLDER}/{names[0]}": project,
        f"{WIRED_FOLDER}/{names[1]}": written.text,
    }, notes


def _judged(
    grounded: Grounded, written: WrittenSchematic, cli: KicadCli, sheet: Path
) -> str:
    """KiCad's judgement of a written sheet: empty if its netlist and ERC pass."""
    exported = cli.export_netlist(sheet)
    if exported.status is not NetlistStatus.OK or exported.text is None:
        return f"KiCad could not export its netlist: {exported.reason}"
    try:
        netlist = parse_netlist(exported.text)
    except NetlistFormatError as exc:
        return f"KiCad's netlist of it: {exc}"
    differences = compare_netlist(grounded, written.net_names, netlist)
    if differences:
        return (
            f"KiCad's netlist of it differs from the design: {differences[0].message}"
        )
    erc = cli.run_erc(sheet)
    if erc.status is not ErcStatus.CLEAN:
        return f"KiCad's rule check of it is {erc.status.name.lower()}"
    return ""


def _wired_note(why: str) -> Finding:
    return Finding(
        "L2",
        "W_WIRED_NOT_WRITTEN",
        "warning",
        f"the wired drawing for a person was not written: {why}. The project "
        "itself is unaffected",
    )


def verify_project(
    design: Design,
    folder: Path,
    index: LibraryIndex,
    cli: KicadCli,
    *,
    models: ModelRegistry | None = None,
) -> Report:
    """Check a project on disk against its design, writing nothing.

    The design is grounded and its structure checked; the schematic file
    is read; KiCad's netlist of it is compared with the design; and
    KiCad's rule check is run. A file that differs from what Kairo would
    write (because it was edited in KiCad) is reported as a warning, and
    the checks go on against the file as it is.

    Args:
        design: The design the project was built from.
        folder: The project folder.
        index: The library index.
        cli: The kicad-cli to check with.
        models: The models folder the project was built with.

    Returns:
        The report.
    """
    findings: list[Finding] = []
    reached = ["L1"]
    try:
        grounded = ground(design, index)
    except GroundingError as exc:
        return make_report(list(exc.findings), reached)
    reached.append("L2")
    findings += check_structure(grounded)
    try:
        written = write_schematic(grounded, index, models=models)
    except WriterError as exc:
        findings.append(Finding("L2", "E_CANNOT_DRAW", "error", str(exc)))
        return make_report(findings, reached)
    if _has_error(findings):
        return make_report(findings, reached)
    sheet = folder / f"{design.design}.kicad_sch"
    reached.append("L0")
    try:
        on_disk = sheet.read_bytes().decode("utf-8")
        check_format(parse(on_disk), SCHEMATIC)
    except (OSError, ValueError) as exc:
        findings.append(
            Finding("L0", "E_FILE_UNREADABLE", "error", f"{sheet.name}: {exc}")
        )
        return make_report(findings, reached)
    if on_disk != written.text:
        findings.append(
            Finding(
                "L0",
                "W_FILE_EDITED",
                "warning",
                f"{sheet.name} is not as Kairo would write it; it has been "
                "edited since. The checks below are on the file as it is.",
            )
        )
    reached.append("L3")
    exported = cli.export_netlist(sheet)
    if exported.status is not NetlistStatus.OK or exported.text is None:
        findings.append(
            Finding(
                "L3",
                "E_NETLIST_UNAVAILABLE",
                "error",
                f"KiCad could not export the netlist: {exported.reason}",
            )
        )
        return make_report(findings, reached)
    try:
        netlist = parse_netlist(exported.text)
    except NetlistFormatError as exc:
        findings.append(
            Finding("L3", "E_NETLIST_MALFORMED", "error", f"KiCad's netlist: {exc}")
        )
        return make_report(findings, reached)
    differences = compare_netlist(grounded, written.net_names, netlist)
    findings += differences
    if differences:
        return make_report(findings, reached)
    reached.append("L4")
    erc = cli.run_erc(sheet)
    if erc.status is not ErcStatus.CLEAN:
        findings += _erc_findings(erc)
    return make_report(findings, reached)


def _has_error(findings: list[Finding]) -> bool:
    return any(finding.severity == "error" for finding in findings)


def _erc_findings(erc: ErcResult) -> list[Finding]:
    """KiCad's rule check as findings; a check that did not run is one too.

    Every finding is an error here, warnings included: there is no way
    yet to waive one with a reason, and passing a design with unread
    warnings would waive them silently.
    """
    if erc.status in (ErcStatus.UNAVAILABLE, ErcStatus.MALFORMED):
        return [
            Finding(
                "L4",
                "E_ERC_UNAVAILABLE"
                if erc.status is ErcStatus.UNAVAILABLE
                else "E_ERC_MALFORMED",
                "error",
                f"KiCad's rule check gave no usable result: {erc.reason}",
            )
        ]
    findings: list[Finding] = []
    for violation in erc.violations:
        if violation.excluded or violation.severity == "ignored":
            continue
        kind = "library" if violation.type in LIBRARY_FINDINGS else "design"
        items = "; ".join(item.description for item in violation.items)
        findings.append(
            Finding(
                "L4",
                f"ERC_{violation.type}",
                "error",
                f"KiCad's rule check ({violation.severity}, a {kind} finding): "
                f"{violation.description}: {items}",
                Where(),
            )
        )
    return findings
