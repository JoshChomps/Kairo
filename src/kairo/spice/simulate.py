"""Simulate a design and report what was measured, and how far to trust it.

One call runs one analysis on every simulator given, twice each: once with
the `ACCURATE` profile and once tightened. A number is reported as checked
only when the tightened run agrees with it (the run is numerically stable),
the simulators agree with each other, and every model in the circuit can
support that kind of result. Otherwise it is ``unverified`` and says why.

Everything is simulated at nominal values. Tolerances are not swept here,
so a limit that holds is reported as holding at nominal, which is not a
pass; a limit that fails at nominal fails.
"""

from __future__ import annotations

import contextlib
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from kairo import progress
from kairo.library.grounding import Grounded
from kairo.parts.store import PartStore
from kairo.power.tree import build_tree
from kairo.spice import measure, nodal
from kairo.spice.circuit import (
    GROUND,
    PASSIVES,
    Circuit,
    CircuitError,
    build_circuit,
    netlist,
    scaled,
)
from kairo.spice.engines import (
    ACCURATE,
    TEMPERATURE,
    Profile,
    Run,
    Simulator,
    commands,
    run_netlist,
    tighter,
)
from kairo.spice.measure import MeasureError, Waveform
from kairo.spice.raw import Plot
from kairo.spice.registry import ModelRegistry
from kairo.spice.request import METRICS, Limit, SimRequest
from kairo.units import parse_quantity

Status = Literal["checked", "unverified"]
Verdict = Literal["pass", "fail", "unverified"]

#: How far a number may move between the accurate and the tightened run,
#: and between simulators, as a fraction of its size. Voltage and current
#: are the plan's stability thresholds (one tenth of the reconciliation
#: tolerances); the plan gives none for times and frequencies, and 1% is
#: Kairo's choice for them.
THRESHOLDS: dict[str, float] = {
    "V": 0.001,
    "A": 0.005,
    "W": 0.01,
    "s": 0.01,
    "Hz": 0.01,
    "dB": 0.001,
    "%": 0.001,
}
#: Below these sizes a difference is rounding, not disagreement.
_FLOORS: dict[str, float] = {
    "V": 1e-6,
    "A": 1e-9,
    "W": 1e-9,
    "s": 1e-12,
    "Hz": 1e-6,
    "dB": 1e-3,
    "%": 1e-3,
}
#: The band used for settling time when the request gives none, as a
#: percentage of the step.
SETTLE_BAND = 1.0
#: The fall in gain that marks a cutoff: half power, 10 * log10(1/2) dB.
CUTOFF_DB = 10.0 * math.log10(0.5)
#: The name exact nodal analysis is shown under beside the simulators.
EXACT = "exact nodal analysis"
#: How far calculation and simulation may differ on a rail's current
#: before it is a finding (the plan's reconciliation tolerance for a DC
#: branch current).
RECONCILE_CURRENT = 0.05

Key = tuple[str, str]


@dataclass(slots=True)
class Metric:
    """One measured number.

    Attributes:
        metric: What it is, for example ``peak`` or ``settling_time``.
        where: The net or part it belongs to.
        value: The value from the first simulator's accurate run.
        unit: Its unit.
        status: ``checked`` or ``unverified``.
        reasons: Why it is unverified; empty when checked.
        tightened: The same number from the tightened run.
        engines: The number from each simulator's accurate run.
    """

    metric: str
    where: str
    value: float
    unit: str
    status: Status = "checked"
    reasons: list[str] = field(default_factory=list[str])
    tightened: float | None = None
    engines: dict[str, float] = field(default_factory=dict[str, float])

    def as_dict(self) -> dict[str, Any]:
        """The metric as plain data, shortest form first."""
        out: dict[str, Any] = {
            "metric": self.metric,
            "where": self.where,
            "value": float(f"{self.value:.6g}"),
            "unit": self.unit,
            "status": self.status,
        }
        if self.reasons:
            out["unverified_because"] = self.reasons
        return out


@dataclass(frozen=True, slots=True)
class Judged:
    """A limit and how it came out.

    Attributes:
        limit: The limit asked for.
        verdict: ``pass``, ``fail`` or ``unverified``.
        detail: The number and the bound, or what is missing.
        held: True if the limit held at nominal values and is unverified
            only because tolerances were not swept; `kairo.spice.sweep`
            takes these further.
    """

    limit: Limit
    verdict: Verdict
    detail: str
    held: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The verdict as plain data."""
        return {
            "metric": self.limit.metric,
            "where": self.limit.net or self.limit.ref,
            "verdict": self.verdict,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Compared:
    """A calculated number beside the simulated one.

    Attributes:
        what: What is compared.
        calculated: The calculation's value.
        simulated: The simulation's value.
        unit: Their unit.
        agree: Whether they are within the reconciliation tolerance.
        note: What the comparison does and does not show.
    """

    what: str
    calculated: float
    simulated: float
    unit: str
    agree: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        """The comparison as plain data."""
        return {
            "what": self.what,
            "calculated": float(f"{self.calculated:.6g}"),
            "simulated": float(f"{self.simulated:.6g}"),
            "unit": self.unit,
            "agree": self.agree,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class SimResult:
    """Everything one simulation gave.

    Attributes:
        status: ``checked`` if there are metrics and every one is,
            ``unverified`` if any is not or there are none, ``failed``
            if no simulator produced results.
        request: What was asked.
        circuit: The circuit that was simulated.
        runs: Every run made.
        metrics: The measured numbers.
        limits: Each limit's verdict.
        compared: Calculated values beside simulated ones.
        notes: Remarks that apply to the whole result.
        waves: For a ``tran`` or ``ac`` run, the first simulator's curves
            by net, as ``(x, y)`` lists, for plotting.
        on_defaults: Each of Kairo's default margins the limits'
            verdicts rested on, a line each.
    """

    status: Literal["checked", "unverified", "failed"]
    request: SimRequest
    circuit: Circuit
    runs: tuple[Run, ...]
    metrics: tuple[Metric, ...]
    limits: tuple[Judged, ...]
    compared: tuple[Compared, ...]
    notes: tuple[str, ...]
    waves: dict[str, tuple[tuple[float, ...], tuple[float, ...]]]
    on_defaults: tuple[str, ...] = ()

    def metric(self, name: str, where: str) -> Metric | None:
        """Find one metric.

        Args:
            name: The metric's name.
            where: The net or part.

        Returns:
            The metric, or ``None`` if it was not measured.
        """
        return next(
            (m for m in self.metrics if m.metric == name and m.where == where), None
        )

    def as_dict(self) -> dict[str, Any]:
        """The result as plain data, with the status and reasons first."""
        because: list[str] = []
        for item in self.metrics:
            for reason in item.reasons:
                if reason not in because:
                    because.append(reason)
        return {
            "status": self.status,
            "unverified_because": because,
            "to_resolve": [u.resolve for u in self.circuit.unmodelled],
            "notes": list(self.notes),
            "limits": [j.as_dict() for j in self.limits],
            "limits_basis": "default margins" if self.on_defaults else "stated values",
            "on_default_margins": list(self.on_defaults),
            "metrics": [m.as_dict() for m in self.metrics],
            "compared": [c.as_dict() for c in self.compared],
            "models": [n.as_dict() for n in self.circuit.models],
            "engines": [
                r.as_dict() for r in self.runs if r.profile.name == ACCURATE.name
            ],
            "failed_runs": [
                f"{r.simulator.engine} ({r.profile.name}): {r.error}"
                for r in self.runs
                if r.plot is None
            ],
        }


def simulate(
    grounded: Grounded,
    request: SimRequest,
    simulators: list[Simulator],
    *,
    store: PartStore | None = None,
    profile: Profile = ACCURATE,
    registry: ModelRegistry | None = None,
    factors: dict[str, float] | None = None,
) -> SimResult:
    """Run one analysis of a design and measure it.

    Args:
        grounded: The design with its pins resolved.
        request: The analysis, sources and limits.
        simulators: The simulators to run; the first is the one whose
            numbers are reported, the others check it.
        store: Datasheet facts, for capacitor DC-bias curves.
        profile: The numerical settings; the tightened run is derived
            from it.
        registry: Registered model files, if a models folder is set. A
            simulator that a model in the circuit does not run on is not
            run, and the result says no second simulator checked it.
        factors: Multipliers for some of the circuit's values, by element
            name, for a run away from nominal; see
            `kairo.spice.circuit.scaled`.

    Returns:
        The measurements, each with its status.

    Raises:
        CircuitError: If the request does not fit the design.
        ValueError: If no simulator is given.
    """
    progress.stage("running the simulators")
    if not simulators:
        raise ValueError("simulate needs at least one simulator")
    circuit = scaled(
        build_circuit(grounded, request, store=store, registry=registry),
        factors or {},
    )
    asked = len(simulators)
    simulators = [s for s in simulators if s.engine in circuit.engines]
    if not simulators:
        raise CircuitError(
            "a model in this circuit runs on none of the simulators given"
        )
    runs: list[Run] = []
    exact: dict[Key, tuple[float, str]] = {}
    measured: dict[tuple[str, str], dict[Key, tuple[float, str]]] = {}
    waves: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
    band, start = step_reference(request)
    jobs = [
        (simulator, used)
        for simulator in simulators
        for used in (profile, tighter(profile))
    ]

    def one(job: tuple[Simulator, Profile]) -> Run:
        simulator, used = job
        text = netlist(
            circuit,
            simulator.engine,
            commands(
                simulator.engine,
                request.analysis,
                used,
                sweep_source(circuit, request),
                request.stimuli,
                pspice=circuit.pspice,
            ),
        )
        return whole_run(
            run_netlist(simulator, text, used, pspice=circuit.pspice), request
        )

    # Each run is its own process in its own folder, so they run side by
    # side; the results are kept in the order the jobs were listed.
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        runs.extend(pool.map(one, jobs))
    for (simulator, used), run in zip(jobs, runs, strict=True):
        if run.plot is None:
            continue
        measured[(simulator.engine, used.name)] = measure_plot(
            circuit, request, run.plot, band, start
        )
        if simulator is simulators[0] and used is profile:
            waves = _waves(circuit, request, run.plot)
            exact = _exact(circuit, request, run.plot, band, start)

    notes: list[str] = [
        "Simulated at nominal values. Tolerances are not swept, so a limit "
        "that holds here holds at nominal only."
    ]
    first = (simulators[0].engine, profile.name)
    if first not in measured:
        return SimResult(
            "failed", request, circuit, tuple(runs), (), (), (), tuple(notes), {}
        )

    tight_name = tighter(profile).name
    blanket = _blanket_reasons(circuit, request)
    if len(simulators) < min(asked, 2):
        blanket.append(
            f"a model in this circuit runs only in {simulators[0].engine}, so no "
            "second simulator checks it"
        )
    metrics: list[Metric] = []
    for key, (value, unit) in measured[first].items():
        item = Metric(key[0], key[1], value, unit)
        item.reasons.extend(blanket)
        tight = measured.get((simulators[0].engine, tight_name), {}).get(key)
        if tight is None:
            item.reasons.append(
                "the tightened run gave no such number, so stability is not checked"
            )
        else:
            item.tightened = tight[0]
            if not close_enough(value, tight[0], unit):
                item.reasons.append(
                    f"numerically unstable: {value:.6g} {unit} with the accurate "
                    f"settings and {tight[0]:.6g} {unit} tightened"
                )
        item.engines[simulators[0].engine] = value
        for other in simulators[1:]:
            theirs = measured.get((other.engine, profile.name), {}).get(key)
            if theirs is None:
                item.reasons.append(f"{other.engine} gave no such number")
                continue
            item.engines[other.engine] = theirs[0]
            if not close_enough(value, theirs[0], unit):
                item.reasons.append(
                    f"the simulators disagree: {simulators[0].engine} "
                    f"{value:.6g} {unit}, {other.engine} {theirs[0]:.6g} {unit}"
                )
        if key in exact:
            item.engines[EXACT] = exact[key][0]
            if not close_enough(value, exact[key][0], unit):
                item.reasons.append(
                    f"exact nodal analysis of the same circuit gives "
                    f"{exact[key][0]:.6g} {unit}"
                )
        if key[0] == "peak_current" and request.analysis.kind == "tran":
            item.reasons.extend(_inrush_reasons(grounded, request))
        if key[0] == "ripple":
            item.reasons.extend(_ripple_reasons(grounded, key[1]))
        if item.reasons:
            item.status = "unverified"
        metrics.append(item)
    if len(simulators) == 1:
        notes.append(
            f"Only {simulators[0].engine} ran, so no second simulator checks it."
        )
    if exact:
        notes.append(
            "The circuit is linear, so each number was also worked out exactly "
            f"from the same circuit with no simulator ({EXACT})."
        )
    for run in runs:
        if not run.simulator.validated:
            notes.append(
                f"{run.simulator.engine} {run.simulator.version} is not a "
                "version Kairo's accuracy set has been recorded on."
            )
            break
    for run in runs:
        if run.aids:
            notes.append(
                f"{run.simulator.engine} used a convergence aid "
                f"({', '.join(run.aids)}) with the {run.profile.name} settings."
            )

    limits = tuple(_judge(limit, metrics) for limit in request.limits)
    compared = _reconcile(grounded, metrics) if request.analysis.kind == "op" else ()
    # A run that measured nothing has checked nothing, whatever the
    # reason: the same reasons that would mark a number mark the run.
    status: Literal["checked", "unverified"] = (
        "checked"
        if metrics and all(m.status == "checked" for m in metrics)
        else "unverified"
    )
    if not metrics:
        notes.append(
            "No number was measured, so nothing is checked"
            + (": " + "; ".join(blanket) if blanket else ".")
        )
    return SimResult(
        status,
        request,
        circuit,
        tuple(runs),
        tuple(metrics),
        limits,
        compared,
        tuple(notes),
        waves,
    )


def _exact(
    circuit: Circuit, request: SimRequest, plot: Plot, band: float, start: float
) -> dict[Key, tuple[float, str]]:
    """The metrics exact nodal analysis gives, for a linear steady state or sweep.

    A sweep is solved at the frequencies the simulator used, so a cutoff
    found between them is interpolated the same way and only the solving
    is compared.
    """
    kind = request.analysis.kind
    if (
        kind not in ("op", "ac")
        or request.loop is not None
        or not nodal.linear(circuit)
    ):
        return {}
    try:
        solved = (
            nodal.steady_state(circuit)
            if kind == "op"
            else nodal.sweep(circuit, plot.real("frequency"))
        )
    except nodal.NodalError:
        return {}
    return measure_plot(circuit, request, solved, band, start)


def sweep_source(circuit: Circuit, request: SimRequest) -> str | None:
    """The source element a ``dc`` analysis sweeps.

    Args:
        circuit: The circuit.
        request: The request.

    Returns:
        The element's name, or ``None`` for another kind of analysis.

    Raises:
        CircuitError: If the swept net has no source.
    """
    if request.analysis.kind != "dc":
        return None
    net = request.analysis.sweep
    assert net is not None
    if net not in circuit.sources:
        raise CircuitError(
            f"{net} has no source to sweep: give it a dc stimulus, or sweep a "
            "supply that no regulator makes"
        )
    return circuit.sources[net]


def step_reference(request: SimRequest) -> tuple[float, float]:
    """The settling band and the time the earliest step begins.

    Args:
        request: The request.

    Returns:
        The band as a percentage of the step, and the start time in
        seconds, from which settling time is counted.
    """
    band = request.settle_band.value if request.settle_band is not None else SETTLE_BAND
    delays = [
        s.delay.value if s.delay is not None else 0.0
        for s in request.stimuli
        if s.kind == "step"
    ]
    return band, min(delays, default=0.0)


def whole_run(run: Run, request: SimRequest) -> Run:
    """Turn a run that stopped before its end into a failed one.

    A simulator can give up part way and still leave a file. Measuring
    half a response as if it were all of it would be worse than no result.
    """
    if run.plot is None:
        return run
    analysis = request.analysis
    if analysis.kind == "tran":
        assert analysis.duration is not None
        reached, wanted, unit = (
            run.plot.real("time")[-1],
            analysis.duration.value,
            "s",
        )
    elif analysis.kind == "dc":
        assert analysis.v_stop is not None
        reached, wanted, unit = (
            run.plot.real("sweep")[-1],
            analysis.v_stop.value,
            "V",
        )
    elif analysis.kind == "ac":
        assert analysis.f_stop is not None
        reached, wanted, unit = (
            run.plot.real("frequency")[-1],
            analysis.f_stop.value,
            "Hz",
        )
    else:
        return run
    if reached >= wanted * (1 - 1e-6):
        return run
    return replace(
        run,
        plot=None,
        error=f"stopped at {reached:.6g} {unit} of {wanted:.6g} {unit}"
        + (f"; {run.error}" if run.error else ""),
    )


def close_enough(first: float, second: float, unit: str) -> bool:
    """Whether two readings of one number agree within its threshold."""
    difference = abs(first - second)
    if difference <= _FLOORS[unit]:
        return True
    if unit in ("dB", "%"):
        # Already a ratio, so the threshold is in its own unit: 0.1 dB,
        # or 0.1 percentage points of a step.
        return difference <= THRESHOLDS[unit] * 100
    return difference <= THRESHOLDS[unit] * max(abs(first), abs(second))


def _blanket_reasons(circuit: Circuit, request: SimRequest) -> list[str]:
    """Reasons that apply to every number of a run."""
    reasons = [f"{u.why}; it is left open" for u in circuit.unmodelled]
    reasons += [note.unproven for note in circuit.models if note.unproven]
    asked = request.analysis.temperature
    if asked is not None and asked.value != TEMPERATURE:
        for note in circuit.models:
            if note.fidelity == "behavioural":
                reasons.append(
                    f"{note.ref} is a behavioural model ({note.source}) with "
                    f"no temperature data, so at {asked.value:g} degC it "
                    f"behaves as at {TEMPERATURE:g} degC"
                )
    if request.analysis.kind not in ("op", "dc"):
        for note in circuit.models:
            if note.fidelity == "behavioural":
                reasons.append(
                    f"{note.ref} is a behavioural model ({note.source}), which "
                    "does not represent how the part behaves in time or "
                    "against frequency"
                )
    return reasons


def _inrush_reasons(grounded: Grounded, request: SimRequest) -> list[str]:
    """Why a peak current in time may be set by something not stated."""
    design = grounded.design
    bare = [s.net for s in request.stimuli if s.kind == "step" and s.resistance is None]
    # Only a capacitor straight across the driven net is charged with
    # nothing stated to limit it.
    on_net: dict[str, set[str]] = {}
    for pin in grounded.pins:
        on_net.setdefault(pin.ref, set()).add(pin.net)
    no_esr = [
        ref
        for ref, part in design.components.items()
        if PASSIVES.get(part.lib_id, ("", ""))[0] == "C"
        and part.quantity("esr") is None
        and any(net in on_net.get(ref, set()) for net in bare)
    ]
    if bare and no_esr:
        return [
            f"the source on {', '.join(bare)} has no resistance stated and "
            f"{', '.join(no_esr)} no ESR, so the peak current is set by the "
            "edge alone; give the source's resistance or the capacitors' ESR"
        ]
    return []


def _ripple_reasons(grounded: Grounded, net: str) -> list[str]:
    """Why a net's simulated ripple may leave out what sets most of it.

    A capacitor's ESR carries the whole ripple current, so with a ceramic
    of a few milliohms or an electrolytic of an ohm it can be most of the
    ripple; a capacitor with none stated is modelled ideal and its net's
    ripple comes out low. TI's SLVA477C gives a buck's output ripple as
    dIL * (ESR + 1 / (8 * fsw * Cout)), the formula `buck_output_ripple`
    uses, in which the ESR term is often the larger.
    """
    design = grounded.design
    on_net = {pin.ref for pin in grounded.pins if pin.net == net}
    no_esr = sorted(
        ref
        for ref in on_net
        if PASSIVES.get(design.components[ref].lib_id, ("", ""))[0] == "C"
        and design.components[ref].quantity("esr") is None
    )
    if not no_esr:
        return []
    return [
        f"{', '.join(no_esr)} on {net} {'has' if len(no_esr) == 1 else 'have'} "
        "no ESR stated and is modelled without one, though ESR can set most "
        "of a ripple; give esr in its params"
    ]


def _node_wave(plot: Plot, node: str) -> Waveform:
    return Waveform(plot.real("time"), plot.real(f"v({node})"))


def measure_plot(
    circuit: Circuit, request: SimRequest, plot: Plot, band: float, start: float = 0.0
) -> dict[Key, tuple[float, str]]:
    """Every metric of one run, by ``(metric, where)``.

    ``start`` is when the earliest step begins; settling time is counted
    from then, not from the start of the record.
    """
    out: dict[Key, tuple[float, str]] = {}
    kind = request.analysis.kind
    nets = {net: node for net, node in circuit.nodes.items() if node != GROUND}
    if kind == "op":
        for net, node in nets.items():
            out[("voltage", net)] = (plot.real(f"v({node})")[-1], "V")
        for net, source in (*circuit.sources.items(), *circuit.regulated.items()):
            # A source's branch current is positive into its + terminal,
            # and a regulator's output pin carries current from the net
            # into the pin, so what either supplies is the negative.
            out[("supplied_current", net)] = (
                -plot.real(f"i({source.lower()})")[-1],
                "A",
            )
        nodes_of: dict[str, list[tuple[str, str]]] = {}
        for element in circuit.elements:
            if element.name in circuit.pins:
                ref, _ = circuit.pins[element.name]
                nodes_of.setdefault(ref, []).append((element.name, element.nodes[0]))

        def volts(node: str) -> float:
            return 0.0 if node == GROUND else plot.real(f"v({node})")[-1]

        for ref, ends in nodes_of.items():
            current = abs(plot.real(f"i({ends[0][0].lower()})")[-1])
            out[("current", ref)] = (current, "A")
            if len(ends) == 2:
                # A two-pin part dissipates the voltage across it times
                # the current through it.
                across = abs(volts(ends[0][1]) - volts(ends[1][1]))
                out[("power", ref)] = (across * current, "W")
        return out
    if kind == "dc":
        for net, node in nets.items():
            values = plot.real(f"v({node})")
            out[("start", net)] = (values[0], "V")
            out[("final", net)] = (values[-1], "V")
            out[("peak", net)] = (max(values), "V")
            out[("lowest", net)] = (min(values), "V")
        return out
    if kind == "tran":
        for net, node in nets.items():
            wave = _node_wave(plot, node)
            out[("start", net)] = (wave.first, "V")
            out[("final", net)] = (wave.last, "V")
            out[("peak", net)] = (wave.highest, "V")
            out[("lowest", net)] = (wave.lowest, "V")
            step = wave.last - wave.first
            if abs(step) <= _FLOORS["V"]:
                continue
            edge = abs(step) * band / 100.0
            if not measure.has_settled(wave, edge):
                continue
            try:
                out[("overshoot", net)] = (
                    100.0 * measure.overshoot(wave, wave.first, wave.last),
                    "%",
                )
                out[("rise_time", net)] = (
                    measure.rise_time(wave, wave.first, wave.last),
                    "s",
                )
                out[("settling_time", net)] = (
                    max(0.0, measure.settling_time(wave, wave.last, edge) - start),
                    "s",
                )
            except MeasureError:
                continue
        for net, source in circuit.sources.items():
            current = plot.real(f"i({source.lower()})")
            out[("peak_current", f"source {net}")] = (max(abs(i) for i in current), "A")
        duration = request.analysis.duration
        assert duration is not None
        settled = request.analysis.ripple_from
        begin = settled.value if settled is not None else duration.value / 2
        for limit in request.limits:
            # Ripple only where it is asked for: a small spread of a large
            # value is the first number to differ between runs, and would
            # unsettle every run in time that did not want it.
            if limit.metric == "ripple" and limit.net in nets:
                wave = _node_wave(plot, nets[limit.net])
                with contextlib.suppress(MeasureError):
                    out[("ripple", limit.net)] = (
                        measure.peak_to_peak(wave, after=begin),
                        "V",
                    )
        for limit in request.limits:
            if limit.metric == "peak_current" and limit.ref is not None:
                name = next(
                    (n for n, (ref, _) in circuit.pins.items() if ref == limit.ref),
                    None,
                )
                if name is not None:
                    current = plot.real(f"i({name.lower()})")
                    out[("peak_current", limit.ref)] = (
                        max(abs(i) for i in current),
                        "A",
                    )
        return out
    frequency = plot.real("frequency")
    for net, node in nets.items():
        if net in circuit.sources:
            continue
        values = plot.values[f"v({node})"]
        if any(v == 0 for v in values):
            continue
        gains = tuple(measure.gain_db(v, 1) for v in values)
        out[("gain_at_start", net)] = (gains[0], "dB")
        out[("gain_at_stop", net)] = (gains[-1], "dB")
        with contextlib.suppress(MeasureError):
            out[("cutoff", net)] = (
                measure.frequency_at_gain(frequency, gains, gains[0] + CUTOFF_DB),
                "Hz",
            )
    return out


def _waves(
    circuit: Circuit, request: SimRequest, plot: Plot
) -> dict[str, tuple[tuple[float, ...], tuple[float, ...]]]:
    """The curves to draw: each net's voltage, or its gain in dB."""
    kind = request.analysis.kind
    if kind == "op":
        return {}
    out: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
    for net, node in circuit.nodes.items():
        if node == GROUND:
            continue
        if kind == "tran":
            out[net] = (plot.real("time"), plot.real(f"v({node})"))
        elif kind == "dc":
            out[net] = (plot.real("sweep"), plot.real(f"v({node})"))
        else:
            values = plot.values[f"v({node})"]
            if all(v != 0 for v in values):
                out[net] = (
                    plot.real("frequency"),
                    tuple(measure.gain_db(v, 1) for v in values),
                )
    if kind == "tran":
        for net, source in circuit.sources.items():
            out[f"current from the source on {net}"] = (
                plot.real("time"),
                tuple(-i for i in plot.real(f"i({source.lower()})")),
            )
    return out


def _judge(limit: Limit, metrics: list[Metric]) -> Judged:
    """Compare one limit with what was measured."""
    where = limit.net if limit.net is not None else limit.ref
    assert where is not None
    unit = METRICS[limit.metric]
    found = next(
        (m for m in metrics if m.metric == limit.metric and m.where == where), None
    )
    if found is None:
        return Judged(
            limit,
            "unverified",
            f"{limit.metric} was not measured on {where}: this analysis does "
            "not give it, or the response had not settled by the end of the run",
        )
    # The request has already checked that each bound reads in this unit.
    bounds = [
        (name, parse_quantity(text, unit).value)
        for name, text in (("max", limit.max), ("min", limit.min))
        if text is not None
    ]
    broken = [
        f"{found.value:.6g} {unit} is {'above' if name == 'max' else 'below'} "
        f"the {name} of {bound:g} {unit}"
        for name, bound in bounds
        if (found.value > bound if name == "max" else found.value < bound)
    ]
    if found.status != "checked":
        return Judged(
            limit,
            "unverified",
            f"{found.value:.6g} {unit}, which is not checked: {found.reasons[0]}",
        )
    if broken:
        return Judged(limit, "fail", "; ".join(broken) + ", at nominal values")
    held = " and ".join(f"{name} {bound:g} {unit}" for name, bound in bounds)
    return Judged(
        limit,
        "unverified",
        f"{found.value:.6g} {unit} is within {held} at nominal values; "
        "tolerances are not swept, so this is not a pass",
        held=True,
    )


def _reconcile(grounded: Grounded, metrics: list[Metric]) -> tuple[Compared, ...]:
    """Put the power tree's currents beside the simulated ones."""
    tree = build_tree(grounded)
    out: list[Compared] = []
    by_key = {(m.metric, m.where): m for m in metrics}
    for net, rail in tree.rails.items():
        simulated = by_key.get(("supplied_current", net))
        if simulated is None:
            continue
        calculated = rail.current
        scale = max(abs(calculated), abs(simulated.value))
        agree = (
            scale <= _FLOORS["A"]
            or abs(calculated - simulated.value) <= RECONCILE_CURRENT * scale
        )
        note = (
            "Both read the same design values, so agreement shows the "
            "formulas fit this circuit, not that the part data is right"
        )
        if rail.is_bound:
            missing = ", ".join(sorted({u.ref for u in rail.unknown}))
            note += (
                f". Both leave out what is not given for {missing}, so the "
                "real current is higher"
            )
        out.append(
            Compared(
                f"current supplied to {net}",
                calculated,
                simulated.value,
                "A",
                agree,
                note,
            )
        )
    return tuple(out)
