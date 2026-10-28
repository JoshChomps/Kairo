"""Loop gain measured in the closed loop, and the margins it gives.

A feedback loop's gain cannot be measured by cutting the loop: the cut
changes the operating point and removes the loading each side puts on
the other. The loop is left closed and two small signals are injected at
one point in it, in two runs: a voltage in series with the wire, and a
current into the node on the downstream side. This is the method of
Tian, Visvanathan, Hantgan and Kundert ("Striving for small-signal
stability", IEEE Circuits and Devices Magazine, vol. 17 no. 1, 2001),
which extends Middlebrook's voltage and current injection (International
Journal of Electronics, vol. 38 no. 4, 1975) to a point where neither
side's impedance can be neglected.

The formula used is derived here, so it does not rest on a memory of
the paper. Call the upstream side of the break A and the downstream
side B. With no independent source inside, the circuit seen from the
two sides is a two-port, with currents into it::

    I_A = Yaa V_A + Yab V_B
    I_B = Yba V_A + Ybb V_B

``Yab`` is the way round the loop: how B's voltage sets what the loop
drives back at A. The closed loop (``V_A = V_B``, ``I_A + I_B = 0``) has
the determinant ``S = Yaa + Yab + Yba + Ybb``. The loop gain T is what
makes ``S`` differ from the same circuit with its one-way transmission
removed, ``Yab`` set equal to ``Yba``::

    1 + T = S / (Yaa + Ybb + 2 Yba)
    T = (Yab - Yba) / (Yaa + Ybb + 2 Yba)

For a loop that goes one way only, with output impedance Zo upstream and
input impedance Zi downstream, this is the open-loop gain times
``Zi / (Zi + Zo)``: the loaded loop gain.

A source ``Vi = V_B - V_A`` is put in the wire and a current ``Ii`` into
B. Writing I for the current through ``Vi`` from B to A:

- with ``Ii = 1`` and ``Vi = 0``: ``V_B = 1/S`` and ``I = (Yaa + Yab)/S``;
- with ``Vi = 1`` and ``Ii = 0``: ``V_B = (Yaa + Yba)/S``.

So ``X = I(first run) - V_B(second run) = (Yab - Yba)/S`` and
``T = X / (1 - X)``. A test holds this against a loop whose gain is
known exactly, in both simulators, to parts in a million million.

Which side is downstream matters: with the two sides exchanged the same
measurement gives another quantity, not T.
"""

from __future__ import annotations

import cmath
import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Literal

from kairo.library.grounding import Grounded
from kairo.parts.store import PartStore
from kairo.spice.circuit import (
    GROUND,
    Circuit,
    CircuitError,
    Element,
    build_circuit,
    netlist,
)
from kairo.spice.engines import (
    ACCURATE,
    Profile,
    Simulator,
    commands,
    run_netlist,
    tighter,
)
from kairo.spice.raw import Plot
from kairo.spice.registry import ModelRegistry
from kairo.spice.request import SimRequest

#: Pin types that take a signal in, so the pin is the downstream side.
_INPUTS = frozenset({"input"})
#: Pin types that drive, so the net is the downstream side.
_OUTPUTS = frozenset({"output", "power_out"})
#: How far a margin may move between runs and still agree: degrees for a
#: phase margin, decibels for a gain margin. Kairo's own figures; the
#: plan gives none for margins.
MARGIN_AGREEMENT = 0.5
#: How far the crossover frequency may move, as a fraction; the same 1%
#: the other frequency metrics use.
CROSSOVER_AGREEMENT = 0.01


@dataclass(frozen=True, slots=True)
class Margins:
    """A loop's stability margins.

    Attributes:
        crossover_hz: Where the gain's size falls through 1, or ``None``
            if it does not within the sweep.
        phase_margin_deg: 180 degrees plus the gain's phase there.
        phase_crossover_hz: Where the phase falls through -180 degrees,
            or ``None`` if it does not within the sweep.
        gain_margin_db: How far below 0 dB the gain is there.
    """

    crossover_hz: float | None
    phase_margin_deg: float | None
    phase_crossover_hz: float | None
    gain_margin_db: float | None


@dataclass(frozen=True, slots=True)
class LoopGain:
    """A loop's gain against frequency.

    Attributes:
        frequencies: The sweep's frequencies, in Hz, rising.
        gain: The loop gain at each, positive for negative feedback at
            low frequency.
    """

    frequencies: tuple[float, ...]
    gain: tuple[complex, ...]

    def margins(self) -> Margins:
        """Find the crossovers and the margins at them.

        The phase is followed from the lowest frequency without jumps, so
        a loop that turns through more than half a turn is read rightly.
        Between two points of the sweep the gain in decibels and the
        phase are taken to run straight against the logarithm of
        frequency.

        Returns:
            The margins; a crossover outside the sweep is ``None``.
        """
        if not self.gain:
            return Margins(None, None, None, None)
        logs = [math.log10(f) for f in self.frequencies]
        decibels = [20 * math.log10(abs(g)) if g else -math.inf for g in self.gain]
        phases = [math.degrees(cmath.phase(self.gain[0]))]
        for previous, current in zip(self.gain, self.gain[1:], strict=False):
            step = math.degrees(cmath.phase(current / previous)) if previous else 0.0
            phases.append(phases[-1] + step)

        def crossing(values: list[float], level: float) -> float | None:
            for index in range(1, len(values)):
                above, below = values[index - 1], values[index]
                if above > level >= below:
                    return (index - 1) + (above - level) / (above - below)
            return None

        def at(values: list[float], position: float) -> float:
            index = min(int(position), len(values) - 2)
            share = position - index
            return values[index] + share * (values[index + 1] - values[index])

        unity = crossing(decibels, 0.0)
        turn = crossing(phases, -180.0)
        return Margins(
            10 ** at(logs, unity) if unity is not None else None,
            180.0 + at(phases, unity) if unity is not None else None,
            10 ** at(logs, turn) if turn is not None else None,
            -at(decibels, turn) if turn is not None else None,
        )


def return_ratio(
    current_run: Plot, voltage_run: Plot, *, probe: str, node: str, sign: int
) -> LoopGain:
    """Work out the loop gain from the two injection runs.

    Args:
        current_run: The AC results with the current injected and the
            series source at zero.
        voltage_run: The AC results with the series source at 1 and no
            current injected.
        probe: The series source's name.
        node: The downstream node, where the current was injected.
        sign: +1 if the series source's positive end is the downstream
            node, -1 if its negative end is.

    Returns:
        The loop gain at each frequency of the sweep.

    Raises:
        KeyError: If a run lacks the probe's current or the node's voltage.
    """
    current = current_run.values[f"i({probe.lower()})"]
    voltage = voltage_run.values[f"v({node.lower()})"]
    gains: list[complex] = []
    for through, across in zip(current, voltage, strict=True):
        x = sign * (through - across)
        gains.append(x / (1 - x) if x != 1 else complex(math.inf))
    return LoopGain(current_run.real("frequency"), tuple(gains))


@dataclass(frozen=True, slots=True)
class LoopResult:
    """A loop-gain measurement and what stands behind it.

    Attributes:
        pin: The pin the loop was measured at, as ``REF.NUMBER``.
        downstream: Which side the signal flows toward: ``pin`` or ``net``.
        margins: The margins from the first simulator's accurate run.
        status: ``checked``, ``unverified``, or ``failed`` if no run gave
            a result.
        reasons: Why it is not checked.
        engines: The phase margin each simulator gave, in degrees.
        low_frequency_gain_db: The gain's size at the start of the sweep.
    """

    pin: str
    downstream: str
    margins: Margins
    status: Literal["checked", "unverified", "failed"]
    reasons: tuple[str, ...]
    engines: dict[str, float | None]
    low_frequency_gain_db: float | None

    def as_dict(self) -> dict[str, Any]:
        """The result as plain data, with the status and reasons first."""
        return {
            "status": self.status,
            "unverified_because": list(self.reasons),
            "pin": self.pin,
            "signal_flows_toward": self.downstream,
            "crossover_hz": self.margins.crossover_hz,
            "phase_margin_deg": self.margins.phase_margin_deg,
            "phase_crossover_hz": self.margins.phase_crossover_hz,
            "gain_margin_db": self.margins.gain_margin_db,
            "low_frequency_gain_db": self.low_frequency_gain_db,
            "phase_margin_by_simulator": dict(self.engines),
            "method": "voltage and current injection with the loop closed "
            "(Tian et al., 2001)",
        }


def measure_loop(
    grounded: Grounded,
    request: SimRequest,
    simulators: list[Simulator],
    *,
    store: PartStore | None = None,
    profile: Profile = ACCURATE,
    registry: ModelRegistry | None = None,
) -> LoopResult:
    """Measure the gain of the loop that passes through one pin.

    Args:
        grounded: The design with its pins resolved.
        request: An ``ac`` analysis with ``loop`` set.
        simulators: The simulators to run.
        store: Datasheet facts, for capacitor DC-bias curves.
        profile: The numerical settings.
        registry: Registered model files.

    Returns:
        The margins, checked only if the part's model represents its
        loop, the tightened run agrees and the simulators agree.

    Raises:
        CircuitError: If the pin is not in the circuit, or its direction
            is not known and the request does not give it.
        ValueError: If the request has no ``loop``.
    """
    asked = request.loop
    if asked is None:
        raise ValueError("the request has no loop")
    circuit = build_circuit(grounded, request, store=store, registry=registry)
    ref, _, written = asked.pin.rpartition(".")
    matches = [
        pin
        for pin in grounded.pins
        if pin.ref == ref and written in (pin.pin.number, pin.pin.name)
    ]
    probe = next(
        (
            name
            for name, (owner, number) in circuit.pins.items()
            if matches and owner == ref and number == matches[0].pin.number
        ),
        None,
    )
    if len(matches) != 1 or probe is None:
        raise CircuitError(
            f"loop.pin {asked.pin!r} is not one connected pin of the design; "
            "write it as REF.PIN"
        )
    kind = matches[0].pin.electrical_type
    downstream = asked.into or (
        "pin" if kind in _INPUTS else "net" if kind in _OUTPUTS else None
    )
    if downstream is None:
        raise CircuitError(
            f"{asked.pin} is a {kind} pin, which does not say which way the loop's "
            "signal flows through it: give loop.into as 'pin' if it flows into "
            "the part there, 'net' if out of it"
        )
    element = next(e for e in circuit.elements if e.name == probe)
    net_node, pin_node = element.nodes
    node = pin_node if downstream == "pin" else net_node
    # The pin's 0 V source runs from the net to the pin, so its positive
    # end is the net.
    sign = -1 if downstream == "pin" else 1

    def variant(volts: int, amps: int) -> Circuit:
        elements = [
            dataclasses.replace(e, value=f"DC 0 AC {volts}") if e.name == probe else e
            for e in circuit.elements
        ]
        elements.append(Element("I_kairo_loop", (GROUND, node), f"DC 0 AC {amps}"))
        return dataclasses.replace(circuit, elements=tuple(elements))

    def gain(simulator: Simulator, settings: Profile) -> LoopGain | None:
        plots: list[Plot] = []
        for volts, amps in ((0, 1), (1, 0)):
            run = run_netlist(
                simulator,
                netlist(
                    variant(volts, amps),
                    simulator.engine,
                    commands(
                        simulator.engine,
                        request.analysis,
                        settings,
                        pspice=circuit.pspice,
                    ),
                ),
                settings,
                pspice=circuit.pspice,
            )
            if run.plot is None or not run.plot.is_complex:
                return None
            plots.append(run.plot)
        try:
            return return_ratio(plots[0], plots[1], probe=probe, node=node, sign=sign)
        except KeyError:
            return None

    usable = [s for s in simulators if s.engine in circuit.engines]
    if not usable:
        raise CircuitError("no installed simulator can run this circuit")
    first = gain(usable[0], profile)
    if first is None:
        return LoopResult(
            asked.pin,
            downstream,
            Margins(None, None, None, None),
            "failed",
            (f"{usable[0].engine} gave no AC result for the two injection runs",),
            {},
            None,
        )
    margins = first.margins()
    reasons = [f"{u.why}; it is left open" for u in circuit.unmodelled]
    reasons += [note.unproven for note in circuit.models if note.unproven]
    note = next((m for m in circuit.models if m.ref == ref), None)
    if note is None or note.fidelity != "vendor":
        reasons.append(
            f"{ref}'s model is "
            f"{note.fidelity if note is not None else 'absent'}, which does not "
            "represent the part's loop; only a maker's model does"
        )
    if margins.crossover_hz is None:
        reasons.append(
            "the loop gain does not fall through 1 within the sweep, so there "
            "is no phase margin to read: widen f_start and f_stop"
        )
    engines: dict[str, float | None] = {usable[0].engine: margins.phase_margin_deg}
    others: list[tuple[str, LoopGain | None]] = [
        (
            f"{usable[0].engine} with tightened settings",
            gain(usable[0], tighter(profile)),
        )
    ]
    for other in usable[1:]:
        found = gain(other, profile)
        engines[other.engine] = (
            found.margins().phase_margin_deg if found is not None else None
        )
        others.append((other.engine, found))
    if len(usable) == 1:
        reasons.append(
            f"only {usable[0].engine} ran this circuit, so no second simulator "
            "checks it"
        )
    for label, found in others:
        if found is None:
            reasons.append(f"{label} gave no result")
            continue
        theirs = found.margins()
        reasons += _disagreements(label, margins, theirs)
    return LoopResult(
        asked.pin,
        downstream,
        margins,
        "unverified" if reasons else "checked",
        tuple(reasons),
        engines,
        20 * math.log10(abs(first.gain[0])) if first.gain and first.gain[0] else None,
    )


def _disagreements(label: str, ours: Margins, theirs: Margins) -> list[str]:
    """How another run's margins differ from the first's, beyond agreement."""
    found: list[str] = []
    pairs = (
        ("phase margin", "deg", ours.phase_margin_deg, theirs.phase_margin_deg),
        ("gain margin", "dB", ours.gain_margin_db, theirs.gain_margin_db),
    )
    for name, unit, mine, other in pairs:
        if (mine is None) != (other is None):
            found.append(f"{label} disagrees on whether there is a {name}")
        elif (
            mine is not None
            and other is not None
            and abs(mine - other) > MARGIN_AGREEMENT
        ):
            found.append(
                f"{label} gives a {name} of {other:.4g} {unit} against "
                f"{mine:.4g} {unit}"
            )
    mine, other = ours.crossover_hz, theirs.crossover_hz
    if (
        mine is not None
        and other is not None
        and abs(mine - other) > CROSSOVER_AGREEMENT * max(mine, other)
    ):
        found.append(
            f"{label} gives a crossover of {other:.6g} Hz against {mine:.6g} Hz"
        )
    return found
