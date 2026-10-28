"""Turn a simulation's curves into files, and say what a simulation rests on."""

from __future__ import annotations

from typing import Any

from kairo.library.grounding import Grounded
from kairo.parts.store import PartStore
from kairo.spice.circuit import CircuitError, build_circuit, varied_values
from kairo.spice.engines import Simulator
from kairo.spice.loop import CROSSOVER_AGREEMENT, MARGIN_AGREEMENT
from kairo.spice.plot import Curve, line_chart
from kairo.spice.registry import ModelRegistry, RegistryError
from kairo.spice.request import SimRequest
from kairo.spice.simulate import RECONCILE_CURRENT, THRESHOLDS, SimResult

#: The label prefix `kairo.spice.simulate` gives current curves.
_CURRENT = "current from"


def charts(result: SimResult, title: str) -> dict[str, str]:
    """The charts of a result, as SVG text by file name.

    Voltages and currents are drawn on separate charts, since they share
    no axis. An operating point has no curves and gives no charts.

    Args:
        result: The simulation's result.
        title: What the charts are of, for their titles.

    Returns:
        ``voltages.svg`` and ``currents.svg`` for a run in time,
        ``gain.svg`` for a frequency sweep, ``sweep.svg`` for a DC sweep.
    """
    kind = result.request.analysis.kind
    if not result.waves or kind == "op":
        return {}
    marked = "" if result.status == "checked" else " (unverified: see the result)"
    if kind == "ac":
        return {
            "gain.svg": line_chart(
                result.waves,
                title=f"{title}: gain against frequency{marked}",
                x_unit="Hz",
                y_unit="dB",
                log_x=True,
            )
        }
    if kind == "dc":
        return {
            "sweep.svg": line_chart(
                result.waves,
                title=(
                    f"{title}: voltages against {result.request.analysis.sweep}{marked}"
                ),
                x_unit="V",
                y_unit="V",
            )
        }
    volts: dict[str, Curve] = {}
    amps: dict[str, Curve] = {}
    for label, curve in result.waves.items():
        (amps if label.startswith(_CURRENT) else volts)[label] = curve
    out: dict[str, str] = {}
    if volts:
        out["voltages.svg"] = line_chart(
            volts, title=f"{title}: voltages{marked}", x_unit="s", y_unit="V"
        )
    if amps:
        out["currents.svg"] = line_chart(
            amps, title=f"{title}: currents{marked}", x_unit="s", y_unit="A"
        )
    return out


def table(result: SimResult) -> str:
    """The curves as CSV text, one column a curve, on the first curve's x.

    Every curve of a run shares its x values, so the first curve's are
    the first column.

    Args:
        result: The simulation's result.

    Returns:
        The CSV text, or an empty string if there are no curves.
    """
    if not result.waves:
        return ""
    labels = list(result.waves)
    x = result.waves[labels[0]][0]
    head = {"ac": "frequency_Hz", "dc": "sweep_V"}.get(
        result.request.analysis.kind, "time_s"
    )
    quoted = ",".join('"' + label.replace('"', '""') + '"' for label in labels)
    lines = [f"{head},{quoted}"]
    for index, value in enumerate(x):
        row = ",".join(repr(result.waves[label][1][index]) for label in labels)
        lines.append(f"{value!r},{row}")
    return "\n".join(lines) + "\n"


def simulation_accuracy(
    grounded: Grounded,
    found: list[Simulator],
    missing: list[str],
    *,
    store: PartStore | None = None,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    """What a simulation of a design would rest on, without running one.

    This is the simulation side of the accuracy report: which model
    stands in for each part and of what class, which parts are left
    open, which values a worst case would need a tolerance for, which
    simulators are installed and whether their versions are ones the
    accuracy set has been recorded on, and the thresholds in force.

    Args:
        grounded: The design with its pins resolved.
        found: The simulators installed.
        missing: A line for each simulator not found, saying where it
            was looked for.
        store: Datasheet facts, for capacitor DC-bias curves.
        registry: Registered model files.

    Returns:
        The report as plain data. If the design cannot be made into a
        circuit, why not.
    """
    request = SimRequest.model_validate({"analysis": {"kind": "op"}})
    try:
        circuit = build_circuit(grounded, request, store=store, registry=registry)
    except (CircuitError, RegistryError) as exc:
        return {"unavailable": f"this design cannot be made into a circuit: {exc}"}
    varied = varied_values(grounded, circuit)
    return {
        "nothing_was_simulated": "This is what a simulation of the design would "
        "rest on. Call simulate for results.",
        "models": [note.as_dict() for note in circuit.models],
        "left_open": [
            {"part": item.ref, "why": item.why, "to_resolve": item.resolve}
            for item in circuit.unmodelled
        ],
        "values_with_no_tolerance": [
            {"value": item.label, "to_resolve": item.resolve}
            for item in varied
            if item.tolerance is None
        ],
        "engines": [
            {
                "name": simulator.engine,
                "installed": True,
                "version": simulator.version,
                "validated": simulator.validated,
            }
            for simulator in found
        ]
        + [
            {"name": line.split(" ")[0], "installed": False, "detail": line}
            for line in missing
        ],
        "second_simulator": len(found) >= 2,
        "thresholds": {
            "agreement_between_runs_and_simulators": {
                unit: f"{share * 100:g}% of the number"
                for unit, share in THRESHOLDS.items()
            },
            "calculated_against_simulated_current": f"{RECONCILE_CURRENT * 100:g}%",
            "loop_margins": f"{MARGIN_AGREEMENT:g} degrees or dB; crossover "
            f"{CROSSOVER_AGREEMENT * 100:g}%",
            "changed_by_the_project": False,
        },
    }
