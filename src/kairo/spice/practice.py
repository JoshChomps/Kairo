"""Compare a simulated response with published design practice.

A design can meet every steady-state number and still be a poor one: a
supply that rings to twice its voltage when plugged in, a rail that
overshoots on the way up, a capacitor that sees more than its rating for
a millisecond. Each check here reads the measured response, states what
it found, and cites the practice it is compared with. They are advice,
not verdicts: only a stated rating that is exceeded is an error.

Sources:

- Analog Devices (Linear Technology) Application Note 88, "Ceramic Input
  Capacitors Can Cause Overvoltage Transients": plugging a live supply
  into a ceramic input capacitor through lead inductance can ring to
  about twice the supply voltage; the cure is damping, by an electrolytic
  capacitor or a small series resistance.
- Texas Instruments SLYT332, "Taming linear-regulator inrush currents":
  the current to charge an output capacitance is ``C * dV/dt``, and
  without soft-start it is limited only by the regulator's current limit.
- Texas Instruments SLVA381B, "Simplifying Stability Checks": "a PM of
  about 45 degrees is considered generally acceptable", and its Table 1
  relates phase margin to ringing in a load step: no ringing at 54
  degrees and above, 1.5 bumps at 45.7, 3 at 34.7, 6 at 19.4.
- Franklin, Powell and Emami-Naeini, "Feedback Control of Dynamic
  Systems": overshoot against damping ratio and phase margin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from kairo.calc.second_order import damping_from_overshoot, phase_margin_from_damping
from kairo.ir.models import RailVoltage
from kairo.library.grounding import Grounded
from kairo.spice.circuit import PASSIVES
from kairo.spice.request import SimRequest
from kairo.spice.simulate import SimResult

#: The phase margin TI SLVA381B calls generally acceptable, and the one at
#: and above which its Table 1 shows no ringing, in degrees.
MARGIN_FLOOR = 45.0
MARGIN_QUIET = 54.0
#: Overshoot below this share of the step is not worth a remark, in %.
QUIET = 0.5

AN88 = "Analog Devices AN88, Ceramic Input Capacitors Can Cause Overvoltage Transients"
SLYT332 = "TI SLYT332, Taming linear-regulator inrush currents"
SLVA381 = "TI SLVA381B, Simplifying Stability Checks"


@dataclass(frozen=True, slots=True)
class Advice:
    """One remark on a response.

    Attributes:
        code: A stable code, for example ``A_RINGING``.
        severity: ``error`` for a stated rating exceeded, ``warning`` for
            something practice advises against, ``note`` otherwise.
        where: The net or part.
        message: What was found, with the numbers.
        source: The practice it is compared with.
        checked: False if the numbers come from an unverified metric.
    """

    code: str
    severity: Literal["error", "warning", "note"]
    where: str
    message: str
    source: str
    checked: bool

    def as_dict(self) -> dict[str, Any]:
        """The remark as plain data."""
        return {
            "code": self.code,
            "severity": self.severity,
            "where": self.where,
            "message": self.message,
            "source": self.source,
            "from_checked_numbers": self.checked,
        }


def review(grounded: Grounded, request: SimRequest, result: SimResult) -> list[Advice]:
    """Read a transient result against design practice.

    Args:
        grounded: The design.
        request: What was simulated.
        result: The simulation's result.

    Returns:
        The remarks, most serious first. Empty for other analyses.
    """
    if request.analysis.kind != "tran" or result.status == "failed":
        return []
    design = grounded.design
    advice: list[Advice] = []
    nets_of: dict[str, list[str]] = {}
    for pin in grounded.pins:
        nets_of.setdefault(pin.ref, []).append(pin.net)
    grounds = {name for name, net in design.nets.items() if net.role == "ground"}
    stepped = {s.net: s for s in request.stimuli if s.kind == "step"}

    for net in design.nets:
        over = result.metric("overshoot", net)
        if over is None or over.value < QUIET or net in stepped:
            continue
        zeta = damping_from_overshoot(min(over.value / 100.0, 1.0))
        margin = phase_margin_from_damping(zeta)
        if margin < MARGIN_FLOOR:
            severity, verdict = (
                "warning",
                f"below the {MARGIN_FLOOR:g} degrees that is generally "
                "acceptable for a power supply",
            )
        elif margin < MARGIN_QUIET:
            severity, verdict = (
                "note",
                f"above the {MARGIN_FLOOR:g} degrees that is generally "
                f"acceptable, and below the {MARGIN_QUIET:g} at which ringing stops",
            )
        else:
            severity, verdict = (
                "note",
                f"above the {MARGIN_QUIET:g} degrees at which ringing stops",
            )
        advice.append(
            Advice(
                "A_RINGING",
                severity,
                net,
                f"{net} overshoots by {over.value:.3g}% of its step. A "
                f"second-order response with that overshoot has a damping "
                f"ratio of {zeta:.2f}, which is what a loop with about "
                f"{margin:.0f} degrees of phase margin gives: {verdict}",
                f"{SLVA381}; Franklin, Powell and Emami-Naeini, Feedback "
                "Control of Dynamic Systems",
                over.status == "checked",
            )
        )

    for net, stimulus in stepped.items():
        peak = result.metric("peak", net)
        final = result.metric("final", net)
        start = result.metric("start", net)
        across = [
            ref
            for ref, part in design.components.items()
            if PASSIVES.get(part.lib_id, ("", ""))[0] == "C"
            and net in nets_of.get(ref, [])
        ]
        bare = [r for r in across if design.components[r].quantity("esr") is None]
        if stimulus.inductance is None:
            if bare:
                advice.append(
                    Advice(
                        "A_HOT_PLUG_NOT_ASSESSED",
                        "note",
                        net,
                        f"{net} is stepped through no inductance, so this run "
                        f"cannot show the ringing a cable causes when a live "
                        f"supply is plugged into {', '.join(bare)}. Such a "
                        "spike can reach about twice the supply voltage. Give "
                        "the source's inductance and resistance to simulate it",
                        AN88,
                        True,
                    )
                )
            continue
        if peak is None or final is None or start is None:
            continue
        step = final.value - start.value
        if step <= 0 or peak.value <= final.value * (1 + QUIET / 100.0):
            continue
        advice.append(
            Advice(
                "A_HOT_PLUG_RINGING",
                "warning",
                net,
                f"{net} rings to {peak.value:.4g} V on a step to "
                f"{final.value:.4g} V, {100 * (peak.value - final.value) / step:.3g}% "
                "over. The source's inductance and the capacitance on the "
                "net form a resonant circuit with little to damp it. Damp it "
                "with an electrolytic capacitor in parallel or a small "
                "resistance in series with the ceramic one, and check the "
                "peak against the absolute maximum of everything on the net",
                AN88,
                peak.status == "checked",
            )
        )

    for ref, part in design.components.items():
        rating = part.quantity("v_rating")
        nets = nets_of.get(ref, [])
        if rating is None or len(nets) != 2:
            continue
        live = [n for n in nets if n not in grounds]
        if len(live) != 1:
            # Neither end is ground, so the net peaks do not give the
            # voltage across the part.
            continue
        peak = result.metric("peak", live[0])
        if peak is not None and peak.value > rating.value:
            advice.append(
                Advice(
                    "A_OVER_VOLTAGE_RATING",
                    "error",
                    ref,
                    f"{ref} sees {peak.value:.4g} V at its peak and is rated "
                    f"for {rating.value:g} V",
                    f"{ref}'s stated voltage rating",
                    peak.status == "checked",
                )
            )

    for requirement in design.requirements:
        if not isinstance(requirement, RailVoltage):
            continue
        peak = result.metric("peak", requirement.net)
        ceiling = requirement.nominal.value * (1 + requirement.tolerance.value / 100.0)
        if peak is not None and peak.value > ceiling:
            advice.append(
                Advice(
                    "A_RAIL_ABOVE_BAND",
                    "warning",
                    requirement.net,
                    f"{requirement.net} reaches {peak.value:.4g} V during the "
                    f"run, above the {ceiling:.4g} V top of its required band "
                    f"({requirement.nominal.value:g} V + "
                    f"{requirement.tolerance.value:g}%). The band is met in the "
                    "steady state and broken on the way there",
                    "the design's rail_voltage requirement",
                    peak.status == "checked",
                )
            )

    for net in stepped:
        inrush = result.metric("peak_current", f"source {net}")
        # A signal's source charging a filter is the filter working, not
        # an inrush; only a supply's is worth a remark.
        if inrush is None or design.nets[net].role != "power":
            continue
        steady = result.waves.get(f"current from the source on {net}")
        settled = abs(steady[1][-1]) if steady is not None else 0.0
        if inrush.value <= 2 * settled or inrush.value <= 0:
            continue
        advice.append(
            Advice(
                "A_INRUSH",
                "note",
                net,
                f"The source on {net} supplies a peak of {inrush.value:.4g} A "
                f"while the capacitors charge, against {settled:.4g} A once "
                "settled. Charging current is C * dV/dt, so a slower edge or "
                "soft-start lowers it. Check the peak against what the "
                "supply, connector and any fuse allow",
                SLYT332,
                inrush.status == "checked",
            )
        )
    order = {"error": 0, "warning": 1, "note": 2}
    return sorted(advice, key=lambda a: order[a.severity])
