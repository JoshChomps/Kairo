"""Simulate the reference designs and save the graphs and numbers.

``uv run python evals/simulation/run.py`` runs each scenario below on the
installed simulators and writes, under ``evals/simulation/results/<time>``,
one folder a scenario with its graphs, its curves and a ``result.json``.
The scenarios are the questions a steady-state check cannot answer: how a
rail comes up, what a live plug-in does to the input, how long a filter
takes to settle.

Needs KiCad's library index (run the server once) and ngspice; Xyce joins
when it is installed. ``--models-dir DIR`` names a folder of registered
maker models; without it a part that has one is simulated with Kairo's
own stand-in. ``evals/simulation/models`` holds the facts for the models
used here, without the files, which are their makers' to distribute.

``--reconcile`` runs instead every reference design at rest and puts
each rail's calculated current beside the simulated one, which is
M2.3's check that calculation and simulation agree on every design
that has models. A design with a part no model covers is listed with
that part, since its rail currents leave the part out.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.server import default_cache_dir
from kairo.spice.engines import Simulator, find_simulator
from kairo.spice.practice import review
from kairo.spice.registry import ModelRegistry
from kairo.spice.report import charts, table
from kairo.spice.request import SimRequest
from kairo.spice.simulate import simulate

ROOT = Path(__file__).resolve().parent.parent
#: Each scenario: its name, the reference design, and the request.
SCENARIOS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("regulator at rest", "ldo_3v3", {"analysis": {"kind": "op"}}),
    (
        "regulator, supply rising in 1 ms",
        "ldo_3v3",
        {
            "analysis": {"kind": "tran", "duration": "5ms"},
            "stimuli": [
                {"net": "VIN_5V", "kind": "step", "value": "5V", "rise": "1ms"}
            ],
        },
    ),
    (
        "regulator, plugged in live through 1 uH and 50 mohm",
        "ldo_3v3",
        {
            "analysis": {"kind": "tran", "duration": "400us"},
            "stimuli": [
                {
                    "net": "VIN_5V",
                    "kind": "step",
                    "value": "5V",
                    "rise": "1us",
                    "resistance": "50mohm",
                    "inductance": "1uH",
                }
            ],
        },
    ),
    (
        "regulator, plugged in live with 1 ohm added in series",
        "ldo_3v3",
        {
            "analysis": {"kind": "tran", "duration": "400us"},
            "stimuli": [
                {
                    "net": "VIN_5V",
                    "kind": "step",
                    "value": "5V",
                    "rise": "1us",
                    "resistance": "1.05ohm",
                    "inductance": "1uH",
                }
            ],
        },
    ),
    (
        "filter, 3.3 V step",
        "rc_lowpass",
        {
            "analysis": {"kind": "tran", "duration": "1ms"},
            "stimuli": [{"net": "IN", "kind": "step", "value": "3.3V", "rise": "1us"}],
        },
    ),
    (
        "filter, frequency sweep",
        "rc_lowpass",
        {
            "analysis": {"kind": "ac", "f_start": "10Hz", "f_stop": "1MHz"},
            "stimuli": [{"net": "IN", "kind": "ac"}],
        },
    ),
    ("indicator at rest", "led_indicator", {"analysis": {"kind": "op"}}),
    ("LM1117 at rest", "ldo_lm1117", {"analysis": {"kind": "op"}}),
    (
        "LM1117, supply rising in 100 us",
        "ldo_lm1117",
        {
            "analysis": {"kind": "tran", "duration": "2ms"},
            "stimuli": [
                {"net": "VIN_5V", "kind": "step", "value": "5V", "rise": "100us"}
            ],
        },
    ),
    (
        "LM1117, plugged in live through 1 uH and 50 mohm",
        "ldo_lm1117",
        {
            "analysis": {"kind": "tran", "duration": "400us"},
            "stimuli": [
                {
                    "net": "VIN_5V",
                    "kind": "step",
                    "value": "5V",
                    "rise": "1us",
                    "resistance": "50mohm",
                    "inductance": "1uH",
                }
            ],
        },
    ),
)


def reconcile(
    index: LibraryIndex,
    simulators: list[Simulator],
    registry: ModelRegistry | None,
    out: Path,
) -> bool:
    """Every reference design at rest, calculation beside simulation.

    Returns:
        True when every rail compared on a fully modelled design agrees.
    """
    rows: dict[str, Any] = {}
    ok = True
    for folder in sorted((ROOT / "reference_designs").iterdir()):
        source = folder / "design.json"
        if not source.is_file():
            continue
        grounded = ground(
            Design.model_validate_json(source.read_text(encoding="utf-8")), index
        )
        result = simulate(
            grounded,
            SimRequest.model_validate({"analysis": {"kind": "op"}}),
            simulators,
            registry=registry,
        )
        unmodelled = [u.ref for u in result.circuit.unmodelled]
        compared = [c.as_dict() for c in result.compared]
        agree = all(c.agree for c in result.compared)
        if not unmodelled:
            ok = ok and agree and result.status != "failed"
        rows[folder.name] = {
            "status": result.status,
            "unmodelled": unmodelled,
            "compared": compared,
            "all_agree": agree,
        }
        shown = ", ".join(
            f"{c.what}: {c.calculated:.4g} / {c.simulated:.4g} {c.unit}"
            + ("" if c.agree else " DISAGREE")
            for c in result.compared
        )
        left = f" (no model: {', '.join(unmodelled)})" if unmodelled else ""
        print(
            f"{folder.name:22} {result.status:10} {shown or 'no rail compared'}{left}"
        )
    out.mkdir(parents=True)
    (out / "reconcile.json").write_text(
        json.dumps(
            {"engines": {s.engine: s.version for s in simulators}, "designs": rows},
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"saved {out}")
    return ok


def main() -> int:
    """Run every scenario and save what it gave.

    Returns:
        0 when every scenario produced results, otherwise 1.
    """
    database = default_cache_dir(os.environ) / "library-kicad10.db"
    if not database.is_file():
        sys.exit(f"no library index at {database}; run the server once to build it")
    index = LibraryIndex(database)
    simulators: list[Simulator] = []
    for engine in ("ngspice", "xyce"):
        found, searched = find_simulator(engine)
        if found is None:
            print(f"{engine}: not found (searched {'; '.join(searched)})")
        else:
            simulators.append(found)
    if not simulators or simulators[0].engine != "ngspice":
        sys.exit("ngspice is needed")
    registry: ModelRegistry | None = None
    if "--models-dir" in sys.argv:
        registry = ModelRegistry(Path(sys.argv[sys.argv.index("--models-dir") + 1]))
        print(f"models from {registry.folder}: {len(registry.entries())} registered")
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    out = Path(__file__).resolve().parent / "results" / stamp
    if "--reconcile" in sys.argv:
        return 0 if reconcile(index, simulators, registry, out) else 1
    ok = True
    summary: dict[str, Any] = {
        "engines": {s.engine: s.version for s in simulators},
        "scenarios": {},
    }
    for name, design_name, request_data in SCENARIOS:
        design = Design.model_validate_json(
            (ROOT / "reference_designs" / design_name / "design.json").read_text(
                encoding="utf-8"
            )
        )
        grounded = ground(design, index)
        request = SimRequest.model_validate(request_data)
        result = simulate(grounded, request, simulators, registry=registry)
        advice = review(grounded, request, result)
        folder = out / name.replace(",", "").replace(" ", "_")
        folder.mkdir(parents=True)
        record = result.as_dict()
        record["practice"] = [a.as_dict() for a in advice]
        record["request"] = request_data
        (folder / "result.json").write_text(
            json.dumps(record, indent=1) + "\n", encoding="utf-8"
        )
        for file_name, text in charts(result, name).items():
            (folder / file_name).write_text(text, encoding="utf-8")
        curves = table(result)
        if curves:
            (folder / "curves.csv").write_text(curves, encoding="utf-8")
        ok = ok and result.status != "failed"
        summary["scenarios"][name] = {
            "status": result.status,
            "failed_runs": record["failed_runs"],
            "practice": [f"{a.severity}: {a.code} on {a.where}" for a in advice],
        }
        print(f"{result.status:10} {name}")
        for item in advice:
            print(f"           {item.severity}: {item.message}")
        for line in record["failed_runs"]:
            print(f"           failed run: {line[:150]}")
    (out / "summary.json").write_text(
        json.dumps(summary, indent=1) + "\n", encoding="utf-8"
    )
    print(f"saved {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
