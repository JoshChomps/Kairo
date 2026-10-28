"""Run the reference designs through Kairo and score the result.

Each folder in ``evals/reference_designs`` holds a spec in words
(``spec.md``), a known-good design (``design.json``) and what should
happen (``expected.json``): either a project whose KiCad netlist is the
one given, or a refusal naming the requirement that cannot be met.

A run builds every design with the installed KiCad and saves, per design,
everything the verdict rests on: the design, KiCad's netlist as text, the
rule check's counts, each requirement's result, and the written
schematic. Scoring reads only those files, so::

    uv run python evals/reference.py run
    uv run python evals/reference.py rescore evals/results/<run>

``rescore`` needs neither KiCad nor the libraries and must give the same
verdicts as the run recorded. No model is called anywhere: this is the
scripted baseline that a model's own runs are later compared with.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from kairo import __version__
from kairo.kicad.netlist import parse_netlist

HERE = Path(__file__).resolve().parent
DESIGNS = HERE / "reference_designs"
RESULTS = HERE / "results"
#: Datasheet facts the designs rely on, such as a capacitor's DC-bias curve,
#: kept here so a run reads the same facts on any computer.
PARTS = HERE / "parts"


def expected_of(folder: Path) -> dict[str, Any]:
    """Read a reference design's expected outcome.

    Args:
        folder: The design's folder.

    Returns:
        The expectation.
    """
    return json.loads((folder / "expected.json").read_text(encoding="utf-8"))


def verdict(
    expected: dict[str, Any], record: dict[str, Any], netlist: str | None
) -> dict[str, Any]:
    """Score one design from what a run saved.

    Args:
        expected: The design's ``expected.json``.
        record: The run's ``record.json`` for the design.
        netlist: KiCad's netlist text from the run, if a project was built.

    Returns:
        ``passed`` (the strict verdict) and one line per check.
    """
    checks: dict[str, bool] = {}
    checks["outcome is as expected"] = record["outcome"] == expected["outcome"]
    if expected["outcome"] == "refusal":
        checks["refused for the expected requirement"] = (
            expected["refused_for"] in record["failed_requirements"]
        )
        checks["nothing was written"] = not record["files"]
    else:
        checks["every verifier layer passed"] = record.get("layers") == {
            layer: "pass" for layer in ("L0", "L1", "L2", "L3", "L4")
        }
        checks["KiCad's rule check is clean"] = record.get("erc") == {
            "status": "clean",
            "errors": 0,
            "warnings": 0,
        }
        got: dict[str, list[str]] = {}
        if netlist is not None:
            for net in parse_netlist(netlist).nets:
                got[net.name] = sorted(f"{node.ref}.{node.pin}" for node in net.nodes)
        want = {name: sorted(pins) for name, pins in expected["netlist"].items()}
        checks["KiCad's netlist is the expected one"] = got == want
        checks["the unverified requirements are the expected ones"] = sorted(
            record["unverified_requirements"]
        ) == sorted(expected["unverified"])
        checks["no requirement fails"] = not record["failed_requirements"]
        checks["no text overlaps or is left out"] = record.get("text") == {
            "overlapping": 0,
            "ambiguous": 0,
            "unplaced": 0,
        }
    return {"passed": all(checks.values()), "checks": checks}


def run(output: Path) -> int:
    """Build every reference design and record the run.

    Args:
        output: The folder for this run's records.

    Returns:
        0 if every design's verdict is a pass, otherwise 1.
    """
    from kairo.build import build_project
    from kairo.ir import Design
    from kairo.kicad.cli import KicadCli
    from kairo.kicad.schematic_writer import write_schematic
    from kairo.library import LibraryIndex
    from kairo.library.grounding import GroundingError, ground
    from kairo.parts.store import PartStore
    from kairo.server import default_cache_dir
    from kairo.verify.requirements import evaluate_requirements

    cli, discovery = KicadCli.find()
    if cli is None:
        sys.exit(f"kicad-cli not found (searched: {'; '.join(discovery.searched)})")
    database = default_cache_dir(os.environ) / "library-kicad10.db"
    if not database.is_file():
        sys.exit(f"no library index at {database}; run the server once to build it")
    index = LibraryIndex(database)
    version = cli.version()
    summary: dict[str, Any] = {
        "kairo": __version__,
        "kicad": version.raw if version else None,
        "kind": "scripted baseline; no model was called",
        "designs": {},
    }
    output.mkdir(parents=True)
    for folder in sorted(p for p in DESIGNS.iterdir() if (p / "design.json").is_file()):
        name = folder.name
        saved = output / name
        saved.mkdir()
        shutil.copyfile(folder / "design.json", saved / "design.json")
        shutil.copyfile(folder / "expected.json", saved / "expected.json")
        design = Design.model_validate(
            json.loads((folder / "design.json").read_text(encoding="utf-8"))
        )
        record: dict[str, Any] = {
            "outcome": "error",
            "files": [],
            "failed_requirements": [],
            "unverified_requirements": [],
            "findings": [],
        }
        netlist: str | None = None
        try:
            grounded = ground(design, index)
        except GroundingError as exc:
            record["findings"] = [f.as_dict() for f in exc.findings]
        else:
            _, results = evaluate_requirements(grounded, PartStore(PARTS))
            record["requirements"] = [
                {"type": r.type, "status": r.status, "summary": r.summary}
                for r in results
            ]
            record["failed_requirements"] = [
                r.type for r in results if r.status == "fail"
            ]
            record["unverified_requirements"] = [
                r.type for r in results if r.status == "unverified"
            ]
            if record["failed_requirements"]:
                # What apply_design does: refuse, and write nothing.
                record["outcome"] = "refusal"
            else:
                with tempfile.TemporaryDirectory() as work:
                    project = Path(work) / design.design
                    built = build_project(design, project, index, cli)
                    record["layers"] = {
                        layer: built.report.status(layer)
                        for layer in ("L0", "L1", "L2", "L3", "L4")
                    }
                    record["findings"] = [f.as_dict() for f in built.findings]
                    if built.erc is not None:
                        record["erc"] = {
                            "status": built.erc.status.value,
                            "errors": built.erc.error_count,
                            "warnings": built.erc.warning_count,
                        }
                    score = write_schematic(grounded, index).text_score
                    record["text"] = {
                        "overlapping": score.overlapping,
                        "ambiguous": score.ambiguous,
                        "unplaced": score.unplaced,
                    }
                    if built.passed:
                        record["outcome"] = "design"
                        sheet = project / f"{design.design}.kicad_sch"
                        exported = cli.export_netlist(sheet)
                        netlist = exported.text
                        shutil.copyfile(sheet, saved / sheet.name)
                        record["files"] = [sheet.name]
                    else:
                        record["outcome"] = "failed checks"
        if netlist is not None:
            (saved / "netlist.net").write_text(netlist, encoding="utf-8", newline="")
        (saved / "record.json").write_text(
            json.dumps(record, indent=1) + "\n", encoding="utf-8"
        )
        result = verdict(expected_of(folder), record, netlist)
        summary["designs"][name] = result
        print(f"{'PASS' if result['passed'] else 'FAIL'}  {name}")
        for check, ok in result["checks"].items():
            if not ok:
                print(f"      not met: {check}")
    passed = sum(1 for r in summary["designs"].values() if r["passed"])
    summary["strict_pass"] = f"{passed} of {len(summary['designs'])}"
    (output / "summary.json").write_text(
        json.dumps(summary, indent=1) + "\n", encoding="utf-8"
    )
    print(f"strict pass: {summary['strict_pass']}; recorded in {output}")
    return 0 if passed == len(summary["designs"]) else 1


def rescore(folder: Path) -> int:
    """Recompute a run's verdicts from its saved files and compare.

    Args:
        folder: A run's folder.

    Returns:
        0 if every recomputed verdict equals the recorded one, otherwise 1.
    """
    recorded = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    same = True
    for name, was in recorded["designs"].items():
        saved = folder / name
        record = json.loads((saved / "record.json").read_text(encoding="utf-8"))
        netlist_file = saved / "netlist.net"
        netlist = (
            netlist_file.read_bytes().decode("utf-8")
            if netlist_file.is_file()
            else None
        )
        # A run keeps the expectation it was judged against, since the
        # reference designs' own can change later. Runs from before that
        # was saved are judged against today's.
        kept = saved if (saved / "expected.json").is_file() else DESIGNS / name
        now = verdict(expected_of(kept), record, netlist)
        agrees = now == was
        same = same and agrees
        print(
            f"{'same' if agrees else 'DIFFERS'}  {name}: "
            f"{'pass' if now['passed'] else 'fail'}"
        )
    print("every verdict reproduced" if same else "a verdict did not reproduce")
    return 0 if same else 1


def main() -> int:
    """Run or rescore, as the command line asks."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="build every reference design and record the run")
    again = commands.add_parser("rescore", help="recompute a run's verdicts offline")
    again.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.command == "rescore":
        return rescore(args.folder)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H%M%SZ")
    return run(RESULTS / f"{stamp}-scripted")


if __name__ == "__main__":
    raise SystemExit(main())
