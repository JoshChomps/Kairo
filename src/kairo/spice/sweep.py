"""Judge simulated limits at the worst case over the stated tolerances.

A limit that holds at nominal values proves nothing about a board built
from parts at the ends of their tolerances. For each such limit this
module:

1. moves each toleranced value to each end of its tolerance, one at a
   time, to learn which way the measured number goes with it;
2. simulates the corner where every value sits at the end that pushes the
   number toward the limit, in full: both settings, every simulator;
3. confirms the corner by moving each value back to nominal in turn, which
   must not push the number further.

The limit passes only if the corner's number is checked, is inside the
limit, and the confirmation holds. It fails if a checked number at the
corner is outside. Anything else is ``unverified`` with the reason: a
value with no tolerance that the number depends on, a run that failed.

A number that does not move one way with a value has its worst case
somewhere between that value's ends, where no corner looks. Those values
are searched: each in turn is tried at evenly spaced points across its
tolerance with the others held, twice round, and the worst point found
is simulated in full. A checked number there that breaks the limit is a
failure, since it is a board that could be built. A search that finds
nothing proves nothing, so the limit is then ``unverified`` with the
worst number found: a simulator gives no bound on what lies between the
points tried.

What is varied is each resistor, capacitor and inductor, each fixed-drop
diode's forward voltage and each source. Stated loads are taken as
maxima. Capacitor ESR, temperature and anything inside a maker's model
are not varied, and the result says so.

This is the plan's sensitivity-guided worst case, with the sensitivities
taken by finite differences for every kind of analysis.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from kairo.defaults import collecting
from kairo.library.grounding import Grounded
from kairo.parts.store import PartStore
from kairo.spice.circuit import (
    Varied,
    build_circuit,
    netlist,
    scaled,
    varied_values,
)
from kairo.spice.engines import (
    ACCURATE,
    TEMPERATURE,
    Profile,
    Simulator,
    commands,
    run_netlist,
)
from kairo.spice.registry import ModelRegistry
from kairo.spice.request import METRICS, Limit, SimRequest
from kairo.spice.simulate import (
    RECONCILE_CURRENT,
    Compared,
    Judged,
    Key,
    SimResult,
    measure_plot,
    simulate,
    step_reference,
    sweep_source,
    whole_run,
)
from kairo.units import parse_quantity
from kairo.verify.requirements import greatest_rail_current

#: The change, in percent, used to see whether a number depends on a value
#: whose tolerance the design does not give.
PROBE = 5.0
#: The most values one sweep will vary. Each costs two runs to learn its
#: direction and one more to confirm each corner.
MAX_VARIED = 24
#: A change smaller than this share of a number is taken to be the
#: simulator's own noise, not a dependence. It is far below the stability
#: thresholds on purpose: a value whose tolerance moves a number by less
#: than a threshold still belongs in the worst case.
NOISE = 1e-4
#: Simulator runs made side by side.
WORKERS = 4
#: Points tried across one value's tolerance when its worst case may lie
#: between the ends, and again between the neighbours of the worst of them.
SAMPLES = 9
#: Times each such value is gone through, the others held where they
#: were last found worst.
ROUNDS = 2

SCOPE = (
    "Limits were judged at the worst case over the stated tolerances of the "
    "resistors, capacitors, inductors, diode forward voltages and sources. "
    "Loads are the stated maxima; capacitor ESR, temperature and the insides "
    "of maker models were not varied. The metrics listed are at nominal values."
)

Measured = dict[Key, tuple[float, str]]


def worst_case(
    grounded: Grounded,
    request: SimRequest,
    simulators: list[Simulator],
    *,
    store: PartStore | None = None,
    profile: Profile = ACCURATE,
    registry: ModelRegistry | None = None,
) -> SimResult:
    """Simulate a design and judge its limits at the worst case.

    With temperature corners in the request, the limits are judged at
    27 degC and again at each corner, where every value's tolerance is
    widened by what its part states of its change with temperature. A
    limit passes only if it passes at every one of them.

    Args:
        grounded: The design with its pins resolved.
        request: The analysis, sources and limits.
        simulators: The simulators to run.
        store: Datasheet facts, for capacitor DC-bias curves.
        profile: The numerical settings.
        registry: Registered model files.

    Returns:
        The result at 27 degC, with each limit's verdict the worst of
        its verdicts at 27 degC and at every corner.

    Raises:
        CircuitError: If the request does not fit the design.
        ValueError: If no simulator is given.
    """
    with collecting() as defaults:
        result = _with_corners(
            grounded,
            request,
            simulators,
            store=store,
            profile=profile,
            registry=registry,
        )
    if not defaults or not result.limits:
        return result
    return dataclasses.replace(
        result,
        notes=(
            *result.notes,
            "Where the design states no tolerance, the limits were judged "
            "with Kairo's own default margins, which are wide: "
            + "; ".join(defaults)
            + ". A limit that passes on them is not shown against the parts' "
            "own figures; state those to tighten it.",
        ),
        on_defaults=tuple(defaults),
    )


def _with_corners(
    grounded: Grounded,
    request: SimRequest,
    simulators: list[Simulator],
    *,
    store: PartStore | None = None,
    profile: Profile = ACCURATE,
    registry: ModelRegistry | None = None,
) -> SimResult:
    """The worst case at 27 degC and at each temperature corner asked for."""
    corners = request.analysis.temperatures
    plain = request.model_copy(
        update={"analysis": request.analysis.model_copy(update={"temperatures": None})}
    )
    nominal = _at_one_temperature(
        grounded, plain, simulators, store=store, profile=profile, registry=registry
    )
    if not corners or nominal.status == "failed":
        return nominal
    verdicts: list[list[tuple[str, Judged]]] = [
        [(f"at {TEMPERATURE:g} degC", item)] for item in nominal.limits
    ]
    for corner in corners:
        at = plain.model_copy(
            update={
                "analysis": plain.analysis.model_copy(update={"temperature": corner})
            }
        )
        result = _at_one_temperature(
            grounded,
            at,
            simulators,
            store=store,
            profile=profile,
            registry=registry,
            away=abs(corner.value - TEMPERATURE),
        )
        for index, limit in enumerate(nominal.limits):
            found = (
                result.limits[index]
                if result.status != "failed"
                else Judged(limit.limit, "unverified", "the run failed")
            )
            verdicts[index].append((f"at {corner.value:g} degC", found))
    combined: list[Judged] = []
    for items in verdicts:
        kinds = {item.verdict for _, item in items}
        verdict = (
            "fail" if "fail" in kinds else "pass" if kinds == {"pass"} else "unverified"
        )
        # The detail leads with what decided it.
        ordered = sorted(items, key=lambda pair: pair[1].verdict != verdict)
        combined.append(
            Judged(
                items[0][1].limit,
                verdict,
                " | ".join(
                    f"{where}: {item.verdict}, {item.detail}" for where, item in ordered
                ),
            )
        )
    scope = (
        "Limits were judged at 27 degC and at "
        + ", ".join(f"{corner.value:g} degC" for corner in corners)
        + ". At each corner a value's tolerance was widened by its part's stated "
        "temperature coefficient, or a ceramic capacitor's dielectric code "
        "(EIA-198), over the difference from 27 degC. A model with no "
        "temperature data behaves there as at 27 degC, and its numbers are "
        "unverified."
    )
    return dataclasses.replace(
        nominal, limits=tuple(combined), notes=(*nominal.notes, scope)
    )


def _at_one_temperature(
    grounded: Grounded,
    request: SimRequest,
    simulators: list[Simulator],
    *,
    store: PartStore | None = None,
    profile: Profile = ACCURATE,
    registry: ModelRegistry | None = None,
    away: float = 0.0,
) -> SimResult:
    """Simulate at the request's one temperature and judge its limits there.

    Args:
        grounded: The design with its pins resolved.
        request: The analysis, sources and limits.
        simulators: The simulators to run.
        store: Datasheet facts, for capacitor DC-bias curves.
        profile: The numerical settings.
        registry: Registered model files.
        away: How far the run is from the nominal temperature, in degrees.

    Returns:
        The nominal result, with each limit that held at nominal replaced
        by its worst-case verdict.

    Raises:
        CircuitError: If the request does not fit the design.
        ValueError: If no simulator is given.
    """
    nominal = simulate(
        grounded, request, simulators, store=store, profile=profile, registry=registry
    )
    # At the nominal temperature a steady state is also compared with
    # the calculations at their own worst corner, rail by rail.
    rails: dict[str, float] = {}
    if nominal.status != "failed" and request.analysis.kind == "op" and not away:
        for net in nominal.circuit.sources:
            greatest = greatest_rail_current(grounded, net)
            if greatest is not None:
                rails[net] = greatest
    if nominal.status == "failed" or not (rails or any(j.held for j in nominal.limits)):
        return nominal
    varied = varied_values(grounded, nominal.circuit, away)
    if len(varied) > MAX_VARIED and not any(j.held for j in nominal.limits):
        return nominal
    if len(varied) > MAX_VARIED:
        return _replace_held(
            nominal,
            f"this circuit has {len(varied)} values with tolerances, more than "
            f"the {MAX_VARIED} one sweep varies; simulate a smaller part of it",
        )
    usable = [s for s in simulators if s.engine in nominal.circuit.engines]
    failures: list[str] = []
    one = evaluator(
        grounded,
        request,
        usable[0],
        store=store,
        profile=profile,
        registry=registry,
        failures=failures,
    )

    def end(item: Varied, sign: int) -> dict[str, float]:
        size = item.tolerance if item.tolerance is not None else PROBE
        return {item.element: 1.0 + sign * size / 100.0}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        highs = list(pool.map(one, [end(item, +1) for item in varied]))
        lows = list(pool.map(one, [end(item, -1) for item in varied]))

    def full(factors: dict[str, float]) -> SimResult:
        return simulate(
            grounded,
            request,
            simulators,
            store=store,
            profile=profile,
            registry=registry,
            factors=factors,
        )

    judged: list[Judged] = []
    for item in nominal.limits:
        if not item.held:
            judged.append(item)
            continue
        judged.append(
            _judge_swept(item, nominal, varied, highs, lows, one, full, failures)
        )
    compared = list(nominal.compared)
    for net, calculated in rails.items():
        at_corner = _compare_at_corner(
            net, calculated, nominal, varied, highs, lows, full
        )
        if at_corner is not None:
            compared.append(at_corner)
    notes = (
        (SCOPE, *nominal.notes[1:])
        if any(j.held for j in nominal.limits)
        else nominal.notes
    )
    return dataclasses.replace(
        nominal, limits=tuple(judged), notes=notes, compared=tuple(compared)
    )


def _compare_at_corner(
    net: str,
    calculated: float,
    nominal: SimResult,
    varied: list[Varied],
    highs: list[Measured | None],
    lows: list[Measured | None],
    full: Callable[[dict[str, float]], SimResult],
) -> Compared | None:
    """A rail's greatest current, calculated and simulated at one corner.

    The calculation takes each branch at its own worst case, which for
    resistors and diodes on one supply is one corner: the supply high,
    each resistance and forward voltage low. The simulation is sent to
    the corner its own runs say raises the current, so a calculation
    that took a value the wrong way shows as a disagreement.

    Returns:
        The comparison, or ``None`` if a value the current depends on
        has no tolerance, or a run failed.
    """
    key: Key = ("supplied_current", net)
    found = nominal.metric(*key)
    if found is None:
        return None
    corner: dict[str, float] = {}
    moved: list[str] = []
    for index, value in enumerate(varied):
        high, low = highs[index], lows[index]
        if high is None or low is None or key not in high or key not in low:
            return None
        up, down = high[key][0], low[key][0]
        noise = NOISE * max(abs(found.value), abs(up), abs(down))
        if value.tolerance is None:
            if abs(up - found.value) > noise or abs(down - found.value) > noise:
                return None
            continue
        if up == down:
            continue
        sign = 1 if up > down else -1
        corner[value.element] = 1.0 + sign * value.tolerance / 100.0
        moved.append(f"{value.label} {'+' if sign > 0 else '-'}{value.tolerance:g}%")
    result = full(corner)
    metric = result.metric(*key) if result.status != "failed" else None
    if metric is None:
        return None
    scale = max(abs(calculated), abs(metric.value))
    return Compared(
        f"greatest current supplied to {net}, at the worst corner "
        f"({', '.join(moved) or 'no value moves it'})",
        calculated,
        metric.value,
        "A",
        abs(calculated - metric.value) <= RECONCILE_CURRENT * scale,
        "The calculation's greatest current beside the simulation's at the "
        "corner the simulation itself finds worst. Both read the same "
        "tolerances, so agreement shows the formulas and their corner fit "
        "this circuit, not that the part data is right",
    )


def evaluator(
    grounded: Grounded,
    request: SimRequest,
    simulator: Simulator,
    *,
    store: PartStore | None = None,
    profile: Profile = ACCURATE,
    registry: ModelRegistry | None = None,
    failures: list[str] | None = None,
) -> Callable[[dict[str, float]], Measured | None]:
    """A function that runs the circuit once with some values multiplied.

    One simulator and one group of settings: quick enough to call many
    times, and not a checked result by itself.

    Args:
        grounded: The design with its pins resolved.
        request: The analysis and sources.
        simulator: The simulator to run.
        store: Datasheet facts, for capacitor DC-bias curves.
        profile: The numerical settings.
        registry: Registered model files.
        failures: If given, each failed run's reason, in the simulator's
            words, is added to it, so a verdict can say why.

    Returns:
        A function from each element's multiplier to the numbers
        measured, or ``None`` if the run failed.
    """
    base = build_circuit(grounded, request, store=store, registry=registry)
    band, start = step_reference(request)

    def one(factors: dict[str, float]) -> Measured | None:
        circuit = scaled(base, factors)
        text = netlist(
            circuit,
            simulator.engine,
            commands(
                simulator.engine,
                request.analysis,
                profile,
                sweep_source(circuit, request),
                request.stimuli,
                pspice=circuit.pspice,
            ),
        )
        run = whole_run(
            run_netlist(simulator, text, profile, pspice=circuit.pspice), request
        )
        if run.plot is None:
            if failures is not None:
                failures.append(run.error or "it gave no result")
            return None
        return measure_plot(circuit, request, run.plot, band, start)

    return one


def _why(failures: Sequence[str]) -> str:
    """The latest failed run's reason, to end a verdict with."""
    return f" ({failures[-1]})" if failures else ""


def _replace_held(nominal: SimResult, why: str) -> SimResult:
    """Mark every limit that held at nominal as unverified for one reason."""
    limits = tuple(
        Judged(j.limit, "unverified", f"{j.detail.split(';')[0]}; {why}")
        if j.held
        else j
        for j in nominal.limits
    )
    return dataclasses.replace(nominal, limits=limits)


def _judge_swept(
    item: Judged,
    nominal: SimResult,
    varied: list[Varied],
    highs: list[Measured | None],
    lows: list[Measured | None],
    one: Callable[[dict[str, float]], Measured | None],
    full: Callable[[dict[str, float]], SimResult],
    failures: Sequence[str] = (),
) -> Judged:
    """One limit's worst-case verdict."""
    limit = item.limit
    where = limit.net if limit.net is not None else limit.ref
    assert where is not None
    key: Key = (limit.metric, where)
    unit = METRICS[limit.metric]
    found = nominal.metric(limit.metric, where)
    assert found is not None
    at_nominal = found.value
    shown = f"{at_nominal:.6g} {unit} at nominal"

    signs: dict[str, int] = {}
    sizes: dict[str, float] = {}
    missing: list[Varied] = []
    interior: list[Varied] = []
    for index, value in enumerate(varied):
        high, low = highs[index], lows[index]
        if high is None or low is None or key not in high or key not in low:
            return Judged(
                limit,
                "unverified",
                f"{shown}; the run with {value.label} moved to an end of its "
                "tolerance failed or did not give this number",
            )
        up, down = high[key][0], low[key][0]
        noise = NOISE * max(abs(at_nominal), abs(up), abs(down))
        if value.tolerance is None:
            if abs(up - at_nominal) > noise or abs(down - at_nominal) > noise:
                missing.append(value)
            continue
        between = min(up, down) - noise <= at_nominal <= max(up, down) + noise
        if up == down and between:
            continue
        if not between:
            # Both ends lie to one side of the nominal number, so there is
            # a peak or a dip inside the tolerance.
            interior.append(value)
        # Every value with a tolerance goes to an end, however little it
        # moves the number: small effects add up.
        signs[value.element] = 1 if up > down else -1
        sizes[value.element] = value.tolerance
    if missing:
        return Judged(
            limit,
            "unverified",
            f"{shown}; it depends on "
            f"{', '.join(v.label for v in missing)}, which "
            f"{'has' if len(missing) == 1 else 'have'} {missing[0].lacks}. "
            + ". ".join(v.resolve for v in missing),
        )
    labels = {v.element: v.label for v in varied}
    if interior:
        return _judge_searched(
            limit, key, unit, shown, interior, signs, sizes, labels, one, full, failures
        )

    details: list[str] = []
    for name, text in (("max", limit.max), ("min", limit.min)):
        if text is None:
            continue
        bound = parse_quantity(text, unit).value
        toward = 1 if name == "max" else -1
        corner = {
            element: 1.0 + toward * sign * sizes[element] / 100.0
            for element, sign in signs.items()
        }
        described = (
            ", ".join(
                f"{labels[e]} {'+' if f > 1 else '-'}{sizes[e]:g}%"
                for e, f in corner.items()
            )
            or "no value moves it"
        )
        result = full(corner)
        metric = (
            result.metric(limit.metric, where) if result.status != "failed" else None
        )
        if metric is None:
            return Judged(
                limit,
                "unverified",
                f"{shown}; the worst-case run ({described}) failed or did not "
                "give this number",
            )
        worst = metric.value
        broken = worst > bound if name == "max" else worst < bound
        if metric.status != "checked":
            return Judged(
                limit,
                "unverified",
                f"{shown}, {worst:.6g} {unit} at the worst case ({described}), "
                f"which is not checked: {metric.reasons[0]}",
            )
        if broken:
            return Judged(
                limit,
                "fail",
                f"{worst:.6g} {unit} at the worst case ({described}) is "
                f"{'above' if name == 'max' else 'below'} the {name} of "
                f"{bound:g} {unit}; {shown}",
            )
        # Each value moved back to nominal must not push the number
        # further toward the limit than the corner did.
        for element in corner:
            backed = {e: f for e, f in corner.items() if e != element}
            again = one(backed)
            if again is None or key not in again:
                return Judged(
                    limit,
                    "unverified",
                    f"{shown}; a run confirming the worst case failed" + _why(failures),
                )
            value = again[key][0]
            further = value - worst if name == "max" else worst - value
            if further > NOISE * max(abs(value), abs(worst)):
                return Judged(
                    limit,
                    "unverified",
                    f"{shown}; with {labels[element]} back at nominal the number "
                    f"is {value:.6g} {unit}, further than the {worst:.6g} {unit} "
                    "of the corner, so the corner is not the worst case",
                )
        details.append(
            f"{worst:.6g} {unit} at the worst case for the {name} of "
            f"{bound:g} {unit} ({described})"
        )
    return Judged(limit, "pass", f"{'; '.join(details)}; {shown}")


def _judge_searched(
    limit: Limit,
    key: Key,
    unit: str,
    shown: str,
    interior: list[Varied],
    signs: dict[str, int],
    sizes: dict[str, float],
    labels: dict[str, str],
    one: Callable[[dict[str, float]], Measured | None],
    full: Callable[[dict[str, float]], SimResult],
    failures: Sequence[str] = (),
) -> Judged:
    """A limit's verdict when its worst case may lie between the ends.

    A point found that breaks the limit is a failure. Finding none is
    not a pass: the verdict is ``unverified`` with the worst found.
    """
    inside = {value.element for value in interior}
    names = ", ".join(value.label for value in interior)
    tried = 0
    found: list[str] = []
    for name, text in (("max", limit.max), ("min", limit.min)):
        if text is None:
            continue
        bound = parse_quantity(text, unit).value
        toward = 1 if name == "max" else -1
        point = {
            element: 1.0
            if element in inside
            else 1.0 + toward * sign * sizes[element] / 100.0
            for element, sign in signs.items()
        }
        best: tuple[float, dict[str, float]] | None = None

        def worse(number: float, than: float, toward: int = toward) -> bool:
            return toward * (number - than) > 0

        for _ in range(ROUNDS):
            for value in interior:
                span = sizes[value.element] / 100.0
                step = 2 * span / (SAMPLES - 1)
                grid = [1.0 - span + step * index for index in range(SAMPLES)]
                centre: float | None = None
                for factors in (grid, None):
                    if factors is None:
                        if centre is None:
                            break
                        # Once more, between the neighbours of the worst.
                        factors = [
                            centre - step + 2 * step * index / (SAMPLES - 1)
                            for index in range(SAMPLES)
                        ]
                        factors = [f for f in factors if 1.0 - span <= f <= 1.0 + span]
                    for factor in factors:
                        trial = {**point, value.element: factor}
                        measured = one(trial)
                        tried += 1
                        if measured is None or key not in measured:
                            continue
                        number = measured[key][0]
                        if best is None or worse(number, best[0]):
                            best = (number, trial)
                            centre = factor
                if best is not None:
                    point = dict(best[1])
        if best is None:
            return Judged(
                limit,
                "unverified",
                f"{shown}; it does not move one way with {names}, and the runs "
                "of a search between the ends failed" + _why(failures),
            )
        described = ", ".join(
            f"{labels[element]} {(factor - 1.0) * 100:+.3g}%"
            for element, factor in best[1].items()
        )
        result = full(best[1])
        metric = (
            result.metric(limit.metric, key[1]) if result.status != "failed" else None
        )
        if metric is None:
            return Judged(
                limit,
                "unverified",
                f"{shown}; it does not move one way with {names}, and the full "
                f"run at the worst point a search found ({described}) failed",
            )
        broken = metric.value > bound if name == "max" else metric.value < bound
        if broken and metric.status == "checked":
            return Judged(
                limit,
                "fail",
                f"{metric.value:.6g} {unit} is "
                f"{'above' if name == 'max' else 'below'} the {name} of {bound:g} "
                f"{unit} at a point between the ends of {names}, found by search "
                f"({described}); {shown}",
            )
        found.append(
            f"{metric.value:.6g} {unit} for the {name} of {bound:g} {unit} "
            f"({described})"
            + ("" if metric.status == "checked" else ", which is not checked")
        )
    return Judged(
        limit,
        "unverified",
        f"{shown}; it does not move one way with {names}, so its worst case "
        f"lies between the ends. A search of {tried} runs found at worst "
        f"{'; '.join(found)}. That is inside the limit, but a search does not "
        "prove that no point between those tried is worse",
    )
