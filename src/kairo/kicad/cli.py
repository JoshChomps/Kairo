"""Run ``kicad-cli``: find it, read its version, run ERC and export netlists.

KiCad's own engine is the referee for ERC and connectivity, so Kairo asks
``kicad-cli`` rather than checking these itself. Every flag used here was
checked against ``kicad-cli --help`` on KiCad 10.0.6, and the ERC report
format and exit codes against reports that version wrote (see
``tests/fixtures/kicad10/erc``).

An ERC result has one of four statuses, and only `ErcStatus.CLEAN` means
the schematic passed:

- ``clean``: kicad-cli ran, exited as documented, and its report lists no
  violation that is not excluded.
- ``findings``: the same, but the report lists violations.
- ``malformed``: a report was written but cannot be trusted as read (not
  JSON, an unknown schema, a missing field, or an exit code that
  contradicts it).
- ``unavailable``: no trustworthy report was obtained at all (kicad-cli not
  found, too old, timed out, crashed, or could not load the file).

Running kicad-cli leaves the user's project as it was. It writes its
reports into a temporary folder that is always removed. While it runs it
holds a lock file (``~<project>.kicad_pro.lck``) in the project folder; if
Kairo has to kill it on a timeout, Kairo removes that lock too, but only a
lock that appeared during the run and names this machine and user, so a
lock held by someone's open KiCad is never touched. kicad-cli 10.0.6 also
writes a local settings file (``<project>.kicad_prl``) into a project that
has none (measured for ERC, netlist and SVG export); Kairo removes one that
appeared during the run and says so in the result's notes.
"""

from __future__ import annotations

import dataclasses
import enum
import getpass
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from kairo.process import ProcessError, ProcessResult, run_process

#: Environment variable naming the kicad-cli executable to use.
ENV_VAR = "KAIRO_KICAD_CLI"

#: The only ERC report schema Kairo reads.
ERC_SCHEMA = "https://schemas.kicad.org/erc.v1.json"

#: Netlist formats ``kicad-cli sch export netlist`` accepts (10.0.6 help).
NETLIST_FORMATS = frozenset(
    {
        "kicadsexpr",
        "kicadxml",
        "cadstar",
        "orcadpcb2",
        "spice",
        "spicemodel",
        "pads",
        "allegro",
    }
)

#: Oldest KiCad major version Kairo runs (the plan's non-goals exclude
#: anything older than 9).
MIN_MAJOR_VERSION = 9

#: Default time limit for ERC and netlist export, in seconds. The largest
#: KiCad demo (a ten-sheet board) takes about 6 s on the development machine.
DEFAULT_TIMEOUT_S = 300.0

#: Time limit for ``kicad-cli version``, in seconds.
VERSION_TIMEOUT_S = 30.0

# Exit codes kicad-cli 10.0.6 gives for `sch erc --exit-code-violations`,
# and for any command whose input file cannot be loaded.
_EXIT_OK = 0
_EXIT_LOAD_FAILED = 3
_EXIT_VIOLATIONS = 5


@dataclass(frozen=True, slots=True)
class KicadVersion:
    """A KiCad version as ``kicad-cli version`` prints it.

    Attributes:
        raw: The text kicad-cli printed, stripped.
        major: Major version, for example 10.
        minor: Minor version.
        patch: Patch version.
    """

    raw: str
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> KicadVersion | None:
        """Read a version from ``kicad-cli version`` output.

        Args:
            text: The command's standard output.

        Returns:
            The version, or ``None`` if the first line does not start with
            ``major.minor.patch``.
        """
        lines = text.strip().splitlines()
        first = lines[0].strip() if lines else ""
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", first)
        if match is None:
            return None
        major, minor, patch = (int(g) for g in match.groups())
        return cls(first, major, minor, patch)


@dataclass(frozen=True, slots=True)
class Discovery:
    """Where kicad-cli was found, and everywhere that was looked.

    Attributes:
        path: The executable, or ``None`` if none was found.
        searched: Each place looked, in order, for the "not found" message.
    """

    path: Path | None
    searched: tuple[str, ...]


def find_kicad_cli(
    explicit: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Discovery:
    """Find the kicad-cli executable.

    The search order is: ``explicit``; the ``KAIRO_KICAD_CLI`` environment
    variable; ``kicad-cli`` on ``PATH``; on Windows, the install folders
    KiCad's installer records in the registry (its uninstall entries), the
    newest version first. A path given explicitly or by the environment
    variable is final: if it does not exist the search stops there, so a
    mistyped setting never quietly runs some other KiCad.

    Args:
        explicit: A path the caller chose, if any.
        environ: Environment to read; the process environment if ``None``.

    Returns:
        The path found (or ``None``) and the places searched.
    """
    env = os.environ if environ is None else environ
    if explicit is not None:
        found = explicit if explicit.is_file() else None
        return Discovery(found, (f"given path {explicit}",))
    configured = env.get(ENV_VAR)
    if configured:
        path = Path(configured)
        return Discovery(path if path.is_file() else None, (f"{ENV_VAR}={path}",))
    searched: list[str] = []
    if "PATH" in env:
        searched.append("PATH")
        on_path = shutil.which("kicad-cli", path=env["PATH"])
        if on_path is not None:
            return Discovery(Path(on_path), tuple(searched))
    if sys.platform == "win32":
        for candidate in _registered_windows_installs():
            searched.append(f"registry: {candidate}")
            if candidate.is_file():
                return Discovery(candidate, tuple(searched))
    return Discovery(None, tuple(searched))


#: ERC findings that say a library was not found on this machine, not that
#: the design is wrong (measured on KiCad's own demos with a standard install).
LIBRARY_FINDINGS = frozenset(
    {"footprint_link_issues", "lib_symbol_issues", "lib_symbol_mismatch"}
)


class ErcStatus(enum.StrEnum):
    """How an ERC run came out. Only ``CLEAN`` is a pass."""

    CLEAN = "clean"
    FINDINGS = "findings"
    MALFORMED = "malformed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ErcItem:
    """One schematic item a violation points at.

    Attributes:
        uuid: The item's UUID in the schematic.
        description: KiCad's description, for example ``Symbol P1 [DB25_Female]``.
        x: Horizontal position exactly as the report gives it. The report
            calls its units mm, but kicad-cli 10.0.6 writes values one
            hundredth of the item's position in the schematic (measured: a
            label at 189.23 mm is reported at 1.8923), so this is a length
            in mm only for 10.0.7 and later, and Kairo does not convert it.
            This is KiCad issue 25582, fixed in 10.0.7 (measured: the same
            label at 189.23, and 1,443 items exactly 100 times their 10.0.6
            values). The report's `ErcResult.kicad_version` says which.
        y: Vertical position, as for ``x``.
    """

    uuid: str
    description: str
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class ErcViolation:
    """One ERC violation as KiCad reported it.

    Attributes:
        sheet: The sheet path, for example ``/`` or ``/power/``.
        type: KiCad's key for the check, for example ``pin_not_connected``.
        severity: ``error`` or ``warning``; or ``ignored`` for a violation
            of a check the project has switched off, which KiCad still
            lists (with an empty severity) when a person has also excluded
            it. An ignored violation never counts as an error or warning.
        description: KiCad's description of the violation.
        items: The items it points at.
        excluded: True if a person waived it in KiCad.
    """

    sheet: str
    type: str
    severity: Literal["error", "warning", "ignored"]
    description: str
    items: tuple[ErcItem, ...]
    excluded: bool


@dataclass(frozen=True, slots=True)
class ErcResult:
    """The outcome of one ERC run.

    Attributes:
        status: Clean, findings, malformed or unavailable.
        reason: One line saying why, for example ``4 errors, 32 warnings``.
        violations: Every violation in the report, excluded ones included.
        ignored_checks: Keys of checks the project turned off, so a clean
            result can say what it did not check.
        kicad_version: The version the report says wrote it.
        notes: Anything else worth knowing, for example a stale lock file
            that was removed.
        process: The kicad-cli run, if one happened.
    """

    status: ErcStatus
    reason: str
    violations: tuple[ErcViolation, ...] = ()
    ignored_checks: tuple[str, ...] = ()
    kicad_version: str | None = None
    notes: tuple[str, ...] = ()
    process: ProcessResult | None = None

    @property
    def error_count(self) -> int:
        """Violations of severity error that are not excluded."""
        return sum(
            1 for v in self.violations if v.severity == "error" and not v.excluded
        )

    @property
    def warning_count(self) -> int:
        """Violations of severity warning that are not excluded."""
        return sum(
            1 for v in self.violations if v.severity == "warning" and not v.excluded
        )

    @property
    def excluded_count(self) -> int:
        """Violations a person waived in KiCad."""
        return sum(1 for v in self.violations if v.excluded)


class _MalformedError(Exception):
    """A report field is missing or of the wrong kind."""


def classify_erc_report(text: str, *, exit_code: int | None = None) -> ErcResult:
    """Read an ERC report and classify it.

    Every field Kairo uses is checked against KiCad's ``erc.v1`` report
    schema; anything missing or of the wrong kind makes the result
    ``malformed`` rather than being skipped. The report must say its
    positions are in mm (see `ErcItem` for what KiCad actually writes).

    Args:
        text: The report, as ``kicad-cli sch erc --format json`` wrote it.
        exit_code: kicad-cli's exit code when run with
            ``--exit-code-violations``, or ``None`` for a saved report.
            When given, it must agree with the report: 0 with no active
            violations, 5 with at least one violation.

    Returns:
        ``clean``, ``findings`` or ``malformed``; never ``unavailable``.
    """
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError as exc:
        return ErcResult(ErcStatus.MALFORMED, f"report is not valid JSON: {exc}")
    try:
        report = _object(data, "report")
        schema = report.get("$schema")
        if schema != ERC_SCHEMA:
            raise _MalformedError(
                f"report schema is {schema!r}; Kairo reads only {ERC_SCHEMA}"
            )
        units = report.get("coordinate_units")
        if units != "mm":
            raise _MalformedError(f"coordinate units are {units!r}, expected 'mm'")
        _string(report, "source", "report")
        _string(report, "date", "report")
        kicad_version = _string(report, "kicad_version", "report")
        ignored = tuple(
            _string(_object(check, "ignored check"), "key", "ignored check")
            for check in _list(report.get("ignored_checks", []), "ignored_checks")
        )
        violations = tuple(
            violation
            for sheet in _list(report.get("sheets"), "sheets")
            for violation in _sheet_violations(_object(sheet, "sheet"), ignored)
        )
    except _MalformedError as exc:
        return ErcResult(ErcStatus.MALFORMED, str(exc))

    result = ErcResult(
        ErcStatus.CLEAN,
        "",
        violations=violations,
        ignored_checks=ignored,
        kicad_version=kicad_version,
    )
    active = result.error_count + result.warning_count
    notes: list[str] = []
    if exit_code is not None:
        if exit_code not in (_EXIT_OK, _EXIT_VIOLATIONS):
            return dataclasses.replace(
                result,
                status=ErcStatus.MALFORMED,
                reason=f"kicad-cli exited with unexpected code {exit_code}",
            )
        if exit_code == _EXIT_OK and active:
            return dataclasses.replace(
                result,
                status=ErcStatus.MALFORMED,
                reason=f"kicad-cli exited 0 but its report lists {active} violations",
            )
        if exit_code == _EXIT_VIOLATIONS and not violations:
            return dataclasses.replace(
                result,
                status=ErcStatus.MALFORMED,
                reason="kicad-cli reported violations but its report lists none",
            )
        if exit_code == _EXIT_VIOLATIONS and not active:
            notes.append(
                "kicad-cli's exit code counted excluded violations; the report "
                "lists only excluded ones, so the result follows the report"
            )
    counts = f"{result.error_count} errors, {result.warning_count} warnings"
    if result.excluded_count:
        counts += f", {result.excluded_count} excluded"
    if active:
        return dataclasses.replace(
            result, status=ErcStatus.FINDINGS, reason=counts, notes=tuple(notes)
        )
    reason = "no violations" if not violations else f"no active violations ({counts})"
    return dataclasses.replace(result, reason=reason, notes=tuple(notes))


class NetlistStatus(enum.StrEnum):
    """Whether a netlist was exported."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class NetlistResult:
    """The outcome of one netlist export.

    Attributes:
        status: ``ok`` with the netlist text, or ``unavailable``.
        reason: One line saying why, when unavailable.
        format: The netlist format asked for.
        text: The netlist, exactly as kicad-cli wrote it.
        kicad_version: The kicad-cli version that wrote it.
        notes: Anything else worth knowing.
        process: The kicad-cli run, if one happened.
    """

    status: NetlistStatus
    reason: str
    format: str
    text: str | None = None
    kicad_version: str | None = None
    notes: tuple[str, ...] = ()
    process: ProcessResult | None = None


class RenderStatus(enum.StrEnum):
    """Whether a schematic was drawn."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RenderResult:
    """The outcome of drawing a schematic as SVG.

    Attributes:
        status: ``ok`` with the files, or ``unavailable``.
        reason: One line saying why, when unavailable.
        files: One SVG per sheet, in name order.
        kicad_version: The kicad-cli version that drew them.
        notes: Anything else worth knowing.
        process: The kicad-cli run, if one happened.
    """

    status: RenderStatus
    reason: str
    files: tuple[Path, ...] = ()
    kicad_version: str | None = None
    notes: tuple[str, ...] = ()
    process: ProcessResult | None = None


@dataclass(slots=True)
class _Run:
    """What happened when kicad-cli was run for one output file."""

    failure: str | None = None
    output: str | None = None
    files: list[Path] = field(default_factory=lambda: [])
    process: ProcessResult | None = None
    notes: list[str] = field(default_factory=lambda: [])


class KicadCli:
    """One kicad-cli installation.

    Args:
        command: The executable and any fixed leading arguments. Usually just
            the path; tests put a stand-in script here.
        temp_root: Where temporary output folders go; the system temporary
            folder if ``None``.
    """

    def __init__(
        self, command: Sequence[str], *, temp_root: Path | None = None
    ) -> None:
        """Wrap a kicad-cli command line."""
        if not command:
            raise ValueError("command must name an executable")
        self.command: tuple[str, ...] = tuple(command)
        self.temp_root = temp_root
        self._version: KicadVersion | None = None

    @classmethod
    def find(
        cls, explicit: Path | None = None, *, temp_root: Path | None = None
    ) -> tuple[KicadCli | None, Discovery]:
        """Find kicad-cli the way `find_kicad_cli` does and wrap it.

        Args:
            explicit: A path the caller chose, if any.
            temp_root: Passed to the constructor.

        Returns:
            The wrapper (or ``None``) and where the search looked.
        """
        discovery = find_kicad_cli(explicit)
        if discovery.path is None:
            return None, discovery
        return cls((str(discovery.path),), temp_root=temp_root), discovery

    def version(self) -> KicadVersion | None:
        """Read the KiCad version, once per instance.

        Returns:
            The version, or ``None`` if kicad-cli could not be run or printed
            something that is not a version.
        """
        if self._version is None:
            try:
                proc = run_process(
                    [*self.command, "version"], timeout_s=VERSION_TIMEOUT_S
                )
            except (OSError, ProcessError):
                return None
            if proc.returncode == _EXIT_OK:
                self._version = KicadVersion.parse(proc.stdout)
        return self._version

    def run_erc(
        self, schematic: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> ErcResult:
        """Run ERC on a schematic and classify the result.

        Runs ``kicad-cli sch erc --format json --severity-all --units mm
        --exit-code-violations`` from the schematic's folder, so the project's
        own settings and library tables apply.

        Args:
            schematic: The root ``.kicad_sch`` file.
            timeout_s: Seconds before kicad-cli and anything it started are
                killed.

        Returns:
            The classified result; ``unavailable`` whenever no trustworthy
            report was obtained.
        """
        run = self._run_for_output(
            schematic,
            "erc.json",
            lambda out, sch: [
                "sch",
                "erc",
                "--format",
                "json",
                "--severity-all",
                "--units",
                "mm",
                "--exit-code-violations",
                "--output",
                str(out),
                str(sch),
            ],
            timeout_s,
        )
        if run.failure is not None or run.output is None or run.process is None:
            reason = run.failure or "kicad-cli wrote no report"
            return ErcResult(
                ErcStatus.UNAVAILABLE,
                reason,
                notes=tuple(run.notes),
                process=run.process,
            )
        result = classify_erc_report(run.output, exit_code=run.process.returncode)
        cli_version = self._version.raw if self._version else None
        if result.kicad_version is not None and result.kicad_version != cli_version:
            run.notes.append(
                f"report says KiCad {result.kicad_version}, "
                f"kicad-cli says {cli_version}"
            )
        return dataclasses.replace(
            result, notes=result.notes + tuple(run.notes), process=run.process
        )

    def export_netlist(
        self,
        schematic: Path,
        *,
        fmt: str = "kicadsexpr",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> NetlistResult:
        """Export a schematic's netlist with ``kicad-cli sch export netlist``.

        Args:
            schematic: The root ``.kicad_sch`` file.
            fmt: One of `NETLIST_FORMATS`.
            timeout_s: Seconds before kicad-cli and anything it started are
                killed.

        Returns:
            The netlist text, or ``unavailable`` with the reason.

        Raises:
            ValueError: If ``fmt`` is not a format kicad-cli accepts.
        """
        if fmt not in NETLIST_FORMATS:
            raise ValueError(f"unknown netlist format {fmt!r}")
        run = self._run_for_output(
            schematic,
            "netlist.out",
            lambda out, sch: [
                "sch",
                "export",
                "netlist",
                "--format",
                fmt,
                "--output",
                str(out),
                str(sch),
            ],
            timeout_s,
        )
        version = self._version.raw if self._version else None
        if run.failure is None and run.process is not None:
            if run.process.returncode != _EXIT_OK:
                run.failure = f"kicad-cli exited with code {run.process.returncode}"
            elif run.output is None:
                run.failure = "kicad-cli wrote no netlist"
        if run.failure is not None:
            return NetlistResult(
                NetlistStatus.UNAVAILABLE,
                run.failure,
                fmt,
                kicad_version=version,
                notes=tuple(run.notes),
                process=run.process,
            )
        return NetlistResult(
            NetlistStatus.OK,
            "",
            fmt,
            text=run.output,
            kicad_version=version,
            notes=tuple(run.notes),
            process=run.process,
        )

    def export_svg(
        self,
        schematic: Path,
        folder: Path,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> RenderResult:
        """Draw every sheet of a schematic with ``kicad-cli sch export svg``.

        Args:
            schematic: The root ``.kicad_sch`` file.
            folder: Where the SVG files go; created if absent. Files already
                there with the same names are replaced.
            timeout_s: Seconds before kicad-cli and anything it started are
                killed.

        Returns:
            The SVG files, one per sheet, or ``unavailable`` with the reason.
        """
        run = self._run_for_output(
            schematic,
            "svg",
            lambda out, sch: ["sch", "export", "svg", "--output", str(out), str(sch)],
            timeout_s,
            move_to=folder,
        )
        version = self._version.raw if self._version else None
        if run.failure is None and run.process is not None:
            if run.process.returncode != _EXIT_OK:
                run.failure = f"kicad-cli exited with code {run.process.returncode}"
            elif not run.files:
                run.failure = "kicad-cli drew no sheet"
        if run.failure is not None:
            return RenderResult(
                RenderStatus.UNAVAILABLE,
                run.failure,
                kicad_version=version,
                notes=tuple(run.notes),
                process=run.process,
            )
        return RenderResult(
            RenderStatus.OK,
            "",
            files=tuple(run.files),
            kicad_version=version,
            notes=tuple(run.notes),
            process=run.process,
        )

    def _run_for_output(
        self,
        schematic: Path,
        filename: str,
        arguments: Callable[[Path, Path], list[str]],
        timeout_s: float,
        move_to: Path | None = None,
    ) -> _Run:
        """Run one kicad-cli command and collect what it wrote.

        The command writes either one file, which is read into the result,
        or a folder of files, which are moved into ``move_to``.
        """
        run = _Run()
        schematic = schematic.resolve()
        if not schematic.is_file():
            run.failure = f"file not found: {schematic}"
            return run
        version = self.version()
        if version is None:
            run.failure = f"could not read the kicad-cli version from {self.command[0]}"
            return run
        if version.major < MIN_MAJOR_VERSION:
            run.failure = (
                f"KiCad {version.raw} is older than Kairo supports "
                f"({MIN_MAJOR_VERSION} or newer)"
            )
            return run

        folder = schematic.parent
        locks_before = _lock_files(folder)
        settings_before = set(folder.glob("*.kicad_prl"))
        temp = Path(tempfile.mkdtemp(prefix="kairo-kicad-", dir=self.temp_root))
        try:
            output = temp / filename
            argv = [*self.command, *arguments(output, schematic)]
            try:
                run.process = run_process(argv, timeout_s=timeout_s, cwd=folder)
            except (OSError, ProcessError) as exc:
                run.failure = f"could not start kicad-cli: {exc}"
                return run
            if run.process.timed_out:
                run.failure = f"kicad-cli timed out after {timeout_s:g} s"
                for lock in _new_own_locks(folder, locks_before):
                    try:
                        lock.unlink()
                        run.notes.append(f"removed the stale lock file {lock}")
                    except OSError as exc:
                        run.notes.append(
                            f"could not remove the lock file {lock}: {exc}"
                        )
                return run
            if run.process.returncode == _EXIT_LOAD_FAILED:
                said = _last_line(run.process.stdout + "\n" + run.process.stderr)
                run.failure = (
                    f"kicad-cli could not load {schematic.name} (exit 3): {said}"
                )
                return run
            if output.is_file():
                # Decoded from bytes so line endings stay as kicad-cli wrote
                # them.
                try:
                    run.output = output.read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    run.failure = f"could not read what kicad-cli wrote: {exc}"
            elif output.is_dir() and move_to is not None:
                try:
                    move_to.mkdir(parents=True, exist_ok=True)
                    for written in sorted(p for p in output.iterdir() if p.is_file()):
                        target = move_to / written.name
                        target.unlink(missing_ok=True)
                        shutil.move(written, target)
                        run.files.append(target)
                except OSError as exc:
                    run.failure = f"could not keep what kicad-cli wrote: {exc}"
            return run
        finally:
            for created in sorted(set(folder.glob("*.kicad_prl")) - settings_before):
                try:
                    created.unlink()
                    run.notes.append(
                        f"removed {created.name}, a local settings file kicad-cli "
                        "created in the project folder"
                    )
                except OSError as exc:
                    run.notes.append(f"could not remove {created}: {exc}")
            problem = _remove_tree(temp)
            if problem is not None:
                run.notes.append(problem)


def run_erc(
    schematic: Path,
    *,
    cli: KicadCli | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ErcResult:
    """Run ERC, finding kicad-cli first if none is given.

    Args:
        schematic: The root ``.kicad_sch`` file.
        cli: The kicad-cli to use; found with `find_kicad_cli` if ``None``.
        timeout_s: Seconds before kicad-cli is killed.

    Returns:
        The classified result. With no kicad-cli found it is ``unavailable``
        and says where Kairo looked; it is never ``clean``.
    """
    if cli is None:
        cli, discovery = KicadCli.find()
        if cli is None:
            searched = "; ".join(discovery.searched) or "nowhere"
            return ErcResult(
                ErcStatus.UNAVAILABLE, f"kicad-cli not found (searched: {searched})"
            )
    return cli.run_erc(schematic, timeout_s=timeout_s)


def _sheet_violations(
    sheet: dict[str, object], ignored_checks: tuple[str, ...]
) -> list[ErcViolation]:
    path = _string(sheet, "path", "sheet")
    _string(sheet, "uuid_path", "sheet")
    found: list[ErcViolation] = []
    for raw in _list(sheet.get("violations"), f"sheet {path} violations"):
        where = f"violation on sheet {path}"
        violation = _object(raw, where)
        kind = _string(violation, "type", where)
        raw_severity = violation.get("severity")
        severity: Literal["error", "warning", "ignored"]
        if raw_severity == "error":
            severity = "error"
        elif raw_severity == "warning":
            severity = "warning"
        elif raw_severity == "" and kind in ignored_checks:
            # KiCad 10.0.6 writes an empty severity for a violation of a check
            # the project has set to "ignore" (measured on a real board).
            severity = "ignored"
        else:
            raise _MalformedError(f"{where} has severity {raw_severity!r}")
        excluded = violation.get("excluded", False)
        if not isinstance(excluded, bool):
            raise _MalformedError(f"{where} has a non-boolean 'excluded'")
        items = tuple(
            _item(_object(item, f"{where} item"), where)
            for item in _list(violation.get("items"), f"{where} items")
        )
        found.append(
            ErcViolation(
                sheet=path,
                type=kind,
                severity=severity,
                description=_string(violation, "description", where),
                items=items,
                excluded=excluded,
            )
        )
    return found


def _item(item: dict[str, object], where: str) -> ErcItem:
    pos = _object(item.get("pos"), f"{where} item position")
    return ErcItem(
        uuid=_string(item, "uuid", f"{where} item"),
        description=_string(item, "description", f"{where} item"),
        x=_number(pos, "x", f"{where} item position"),
        y=_number(pos, "y", f"{where} item position"),
    )


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _MalformedError(f"{where} is not a JSON object")
    mapping = cast("dict[object, object]", value)
    return {str(k): v for k, v in mapping.items()}


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise _MalformedError(f"{where} is missing or not a list")
    return list(cast("list[object]", value))


def _string(obj: dict[str, object], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise _MalformedError(f"{where} has no text field {key!r}")
    return value


def _number(obj: dict[str, object], key: str, where: str) -> float:
    value = obj.get(key)
    # bool is a subclass of int in Python, and is not a coordinate.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _MalformedError(f"{where} has no number {key!r}")
    return float(value)


def _lock_files(folder: Path) -> set[Path]:
    return set(folder.glob("~*.lck"))


def _new_own_locks(folder: Path, before: set[Path]) -> list[Path]:
    """Lock files that appeared during the run and name this machine and user."""
    return sorted(p for p in _lock_files(folder) - before if _lock_is_ours(p))


def _lock_is_ours(lock: Path) -> bool:
    # KiCad 10.0.6 writes {"hostname": ..., "username": ...}; the host name
    # it writes is upper case on Windows, so names are compared without case.
    try:
        data: object = json.loads(lock.read_text(encoding="utf-8"))
        user = getpass.getuser()
    except (OSError, ValueError, KeyError, ImportError):
        # Python 3.12's getuser raises KeyError or ImportError when it cannot
        # name the user; 3.13 raises OSError.
        return False
    try:
        fields = _object(data, "lock file")
    except _MalformedError:
        return False
    host = fields.get("hostname")
    name = fields.get("username")
    if not isinstance(host, str) or not isinstance(name, str):
        return False
    ours = socket.gethostname().casefold()
    return name.casefold() == user.casefold() and host.casefold() in (
        ours,
        ours.split(".", 1)[0],
    )


def _remove_tree(path: Path) -> str | None:
    """Delete a folder, retrying briefly for Windows handles still closing."""
    last: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            last = exc
            time.sleep(0.1 * (attempt + 1))
    return f"could not remove the temporary folder {path}: {last}"


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no message"


def _registered_windows_installs() -> list[Path]:
    """kicad-cli paths under the install folders in KiCad's uninstall entries."""
    if sys.platform != "win32":
        return []
    import winreg

    roots = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    ]
    found: list[tuple[tuple[int, ...], Path]] = []
    for hive, key in roots:
        try:
            with winreg.OpenKey(hive, key) as uninstall:
                for index in range(winreg.QueryInfoKey(uninstall)[0]):
                    name = winreg.EnumKey(uninstall, index)
                    if not name.startswith("KiCad "):
                        continue
                    # An entry without these values is skipped on its own, so
                    # it cannot hide the entries after it.
                    try:
                        with winreg.OpenKey(uninstall, name) as entry:
                            location = str(
                                winreg.QueryValueEx(entry, "InstallLocation")[0]
                            )
                            version = str(
                                winreg.QueryValueEx(entry, "DisplayVersion")[0]
                            )
                    except OSError:
                        continue
                    order = tuple(int(n) for n in re.findall(r"\d+", version))
                    found.append((order, Path(location) / "bin" / "kicad-cli.exe"))
        except OSError:
            continue
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in found]
