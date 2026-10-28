"""Run the accuracy set on every installed simulator, at several settings.

``uv run python evals/accuracy/run.py`` prints each check's error at each
profile and saves the numbers under ``evals/accuracy/results``. The
profile Kairo uses (`kairo.spice.engines.ACCURATE`) is the loosest one
tried at which every check is inside its budget on both simulators.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kairo.spice.circuit import Engine
from kairo.spice.engines import (
    ACCURATE,
    Profile,
    Simulator,
    commands,
    find_simulator,
    run_netlist,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from circuits import Case, cases

#: The settings tried, loosest first. The first is close to SPICE's own
#: defaults.
PROFILES: tuple[Profile, ...] = (
    replace(ACCURATE, name="default-like", reltol=1e-3, steps=50),
    replace(ACCURATE, name="reltol 1e-3, 500 steps", reltol=1e-3, steps=500),
    replace(ACCURATE, name="reltol 1e-4, 500 steps", reltol=1e-4, steps=500),
    replace(ACCURATE, name="reltol 1e-4, 2000 steps", reltol=1e-4, steps=2000),
    replace(ACCURATE, name="reltol 1e-5, 8000 steps", reltol=1e-5, steps=8000),
    replace(ACCURATE, name="reltol 1e-4, 2000 steps, gear", method="gear", steps=2000),
)


def run_case(simulator: Simulator, case: Case, profile: Profile) -> dict[str, Any]:
    """Run one case and compare each check with its exact value.

    Args:
        simulator: The simulator to run.
        case: The circuit.
        profile: The settings.

    Returns:
        The error of each check and whether all are within budget.
    """
    lines = [
        case.name,
        *case.lines(simulator.engine),
        *commands(simulator.engine, case.analysis, profile),
    ]
    run = run_netlist(simulator, "\n".join([*lines, ".end"]) + "\n", profile)
    if run.plot is None:
        return {"within_budget": False, "error": run.error, "checks": []}
    checks: list[dict[str, Any]] = []
    for check in case.checks:
        measured = check.measure(run.plot)
        error = abs(measured - check.exact)
        checks.append(
            {
                "label": check.label,
                "exact": check.exact,
                "measured": measured,
                "error": error,
                "budget": check.budget,
                "within": error <= check.budget,
            }
        )
    return {
        "within_budget": all(c["within"] for c in checks),
        "points": len(next(iter(run.plot.values.values()))),
        "checks": checks,
    }


def main() -> int:
    """Run the set, print a table and save the numbers.

    Returns:
        0 if `ACCURATE` is within budget everywhere, otherwise 1.
    """
    found: list[Simulator] = []
    engines: tuple[Engine, ...] = ("ngspice", "xyce")
    for engine in engines:
        simulator, searched = find_simulator(engine)
        if simulator is None:
            print(f"{engine}: not found (searched {'; '.join(searched)})")
        else:
            print(f"{engine} {simulator.version} at {simulator.path}")
            found.append(simulator)
    if not found:
        return 1
    profiles = (*PROFILES, ACCURATE)
    record: dict[str, Any] = {
        "engines": {s.engine: s.version for s in found},
        "profiles": {},
    }
    ok = True
    for profile in profiles:
        entry: dict[str, Any] = {"settings": profile.as_dict(), "results": {}}
        print(f"\n{profile.name}")
        for simulator in found:
            for case in cases():
                result = run_case(simulator, case, profile)
                entry["results"][f"{simulator.engine}: {case.name}"] = result
                worst = max(
                    (c["error"] / c["budget"] for c in result["checks"]), default=0.0
                )
                verdict = "ok  " if result["within_budget"] else "OVER"
                detail = result.get("error", f"worst error {worst:.3g} of budget")
                print(f"  {verdict} {simulator.engine:8} {case.name:22} {detail}")
                if profile is ACCURATE and not result["within_budget"]:
                    ok = False
        record["profiles"][profile.name] = entry
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    out = Path(__file__).resolve().parent / "results" / f"{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(f"\nsaved {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
