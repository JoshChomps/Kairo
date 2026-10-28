"""Circuits with exact answers, to test the simulators and Kairo's reading of them.

Each case is a netlist body, the analysis to run, and a list of checks:
what to read from the results, the exact value, and the error allowed.
They test the engines, the raw-file reader and the measurement library.
They do not test part models.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Callable
from dataclasses import dataclass

from kairo.spice.measure import Waveform, gain_db, overshoot, phase_degrees
from kairo.spice.raw import Plot
from kairo.spice.request import Analysis


@dataclass(frozen=True, slots=True)
class Check:
    """One number to compare.

    Attributes:
        label: What it is.
        measure: Reads it from a run's results.
        exact: The exact value.
        budget: The largest error allowed, in the value's own unit.
    """

    label: str
    measure: Callable[[Plot], float]
    exact: float
    budget: float


@dataclass(frozen=True, slots=True)
class Case:
    """One circuit of the set.

    Attributes:
        name: A short name.
        body: The netlist's element lines, the same for every simulator.
        analysis: The analysis to run.
        checks: The numbers to compare.
        xyce_body: The element lines for Xyce, where its dialect differs.
    """

    name: str
    body: tuple[str, ...]
    analysis: Analysis
    checks: tuple[Check, ...]
    xyce_body: tuple[str, ...] | None = None

    def lines(self, engine: str) -> tuple[str, ...]:
        """The element lines for one simulator.

        Args:
            engine: ``ngspice`` or ``xyce``.

        Returns:
            The lines.
        """
        if engine == "xyce" and self.xyce_body is not None:
            return self.xyce_body
        return self.body


def _wave(plot: Plot, variable: str) -> Waveform:
    return Waveform(plot.real("time"), plot.real(variable))


def _at(variable: str, time: float) -> Callable[[Plot], float]:
    return lambda plot: _wave(plot, variable).at(time)


def _op(variable: str) -> Callable[[Plot], float]:
    return lambda plot: plot.real(variable)[-1]


def _ladder() -> Case:
    # 10 V across 1k, 2k, 3k and 4k in series: each node is an exact
    # fraction of 10 V.
    return Case(
        "resistor ladder",
        (
            "V1 a 0 DC 10",
            "R1 a b 1000",
            "R2 b c 2000",
            "R3 c d 3000",
            "R4 d 0 4000",
        ),
        Analysis(kind="op"),
        (
            Check("v(b)", _op("v(b)"), 9.0, 1e-9),
            Check("v(c)", _op("v(c)"), 7.0, 1e-9),
            Check("v(d)", _op("v(d)"), 4.0, 1e-9),
            Check("i(v1)", _op("i(v1)"), -1e-3, 1e-12),
        ),
    )


RC_TAU = 1e-3
RC_RISE = 1e-9


def _rc_exact(time: float) -> float:
    # The response of an RC to a ramp that reaches 1 V in RC_RISE and
    # holds: exact, so the source's finite edge costs no error budget.
    def ramp(t: float) -> float:
        return (t - RC_TAU * (1 - math.exp(-t / RC_TAU))) / RC_RISE if t > 0 else 0.0

    return ramp(time) - ramp(time - RC_RISE)


def _rc_step() -> Case:
    times = (0.5e-3, 1e-3, 2e-3, 5e-3)
    return Case(
        "RC step",
        (
            f"V1 in 0 PULSE(0 1 0 {RC_RISE!r} {RC_RISE!r} 1e6 2e6)",
            "R1 in out 1000",
            "C1 out 0 1e-6",
        ),
        Analysis(kind="tran", duration="5ms"),  # pyright: ignore[reportArgumentType]
        tuple(
            Check(f"v(out) at {t * 1e3:g} ms", _at("v(out)", t), _rc_exact(t), 1e-4)
            for t in times
        ),
    )


RLC_R, RLC_L, RLC_C = 10.0, 1e-3, 1e-6
_ALPHA = RLC_R / (2 * RLC_L)
_OMEGA_0 = 1 / math.sqrt(RLC_L * RLC_C)
_OMEGA_D = math.sqrt(_OMEGA_0**2 - _ALPHA**2)
_ZETA = _ALPHA / _OMEGA_0


def _rlc_exact(time: float) -> float:
    # The capacitor voltage of a series RLC after a unit step:
    # 1 - exp(-a t) (cos(wd t) + (a / wd) sin(wd t)).
    decay = math.exp(-_ALPHA * time)
    swing = math.cos(_OMEGA_D * time) + (_ALPHA / _OMEGA_D) * math.sin(_OMEGA_D * time)
    return 1 - decay * swing


def _rlc_step() -> Case:
    # Underdamped, so a method that adds or removes damping shows up as a
    # wrong overshoot. The edge is 1 ns against a 199 us period.
    times = (50e-6, 99.3e-6, 200e-6, 500e-6, 1e-3)
    exact_overshoot = math.exp(-math.pi * _ZETA / math.sqrt(1 - _ZETA**2))
    return Case(
        "underdamped RLC step",
        (
            "V1 in 0 PULSE(0 1 0 1e-9 1e-9 1e6 2e6)",
            f"R1 in a {RLC_R!r}",
            f"L1 a out {RLC_L!r}",
            f"C1 out 0 {RLC_C!r}",
        ),
        Analysis(kind="tran", duration="2ms"),  # pyright: ignore[reportArgumentType]
        (
            *(
                Check(
                    f"v(out) at {t * 1e6:g} us",
                    _at("v(out)", t),
                    _rlc_exact(t),
                    2e-4,
                )
                for t in times
            ),
            Check(
                "overshoot",
                lambda plot: overshoot(_wave(plot, "v(out)"), 0.0, 1.0),
                exact_overshoot,
                2e-4,
            ),
        ),
    )


def _ac_value(plot: Plot, variable: str, frequency: float) -> complex:
    # The sweep's points are not the frequencies asked for, so the two
    # either side are interpolated in log frequency.
    points = plot.real("frequency")
    values = plot.values[variable]
    for index in range(1, len(points)):
        if points[index] >= frequency:
            low, high = math.log(points[index - 1]), math.log(points[index])
            share = (math.log(frequency) - low) / (high - low)
            log_low, log_high = cmath.log(values[index - 1]), cmath.log(values[index])
            return cmath.exp(log_low + share * (log_high - log_low))
    raise ValueError(f"{frequency} Hz is outside the sweep")


def _rc_ac() -> Case:
    # A 1k, 100 nF low-pass: H = 1 / (1 + j w R C), cutoff 1591.5 Hz.
    def exact(frequency: float) -> complex:
        return 1 / (1 + 2j * math.pi * frequency * 1000 * 100e-9)

    checks: list[Check] = []
    for frequency in (100.0, 1591.5494309189535, 20000.0):
        checks.append(
            Check(
                f"gain at {frequency:g} Hz, dB",
                lambda plot, f=frequency: gain_db(_ac_value(plot, "v(out)", f), 1),
                gain_db(exact(frequency), 1),
                2e-3,
            )
        )
        checks.append(
            Check(
                f"phase at {frequency:g} Hz, degrees",
                lambda plot, f=frequency: phase_degrees(
                    _ac_value(plot, "v(out)", f), 1
                ),
                phase_degrees(exact(frequency), 1),
                2e-2,
            )
        )
    return Case(
        "RC low-pass",
        ("V1 in 0 DC 0 AC 1", "R1 in out 1000", "C1 out 0 1e-7"),
        Analysis(kind="ac", f_start="10Hz", f_stop="1MHz"),  # pyright: ignore[reportArgumentType]
        tuple(checks),
    )


def lambert_w(x: float) -> float:
    """The principal branch of the Lambert W function for large ``x``.

    Solves ``w * exp(w) = x`` by Newton's method on ``w + ln(w) = ln(x)``,
    which stays in range where ``exp(w)`` would overflow.

    Args:
        x: The argument, above 1.

    Returns:
        ``W(x)``.
    """
    target = math.log(x)
    w = target - math.log(target)
    for _ in range(50):
        step = (w + math.log(w) - target) / (1 + 1 / w)
        w -= step
        if abs(step) < 1e-15 * abs(w):
            break
    return w


#: The Boltzmann constant and the elementary charge, exact by the 2019
#: definition of the SI (BIPM, "The International System of Units", 9th
#: edition).
BOLTZMANN = 1.380649e-23
CHARGE = 1.602176634e-19


def _diode() -> Case:
    # A 5 V source, 1k and a diode with IS = 1e-14 A and N = 1 at 27 degC.
    # Kirchhoff's law with the Shockley equation solves in closed form:
    # I = (n Vt / R) W((Is R / n Vt) exp((V + Is R) / n Vt)) - Is.
    volts, ohms, saturation = 5.0, 1000.0, 1e-14
    thermal = BOLTZMANN * (27.0 + 273.15) / CHARGE
    argument = (saturation * ohms / thermal) * math.exp(
        (volts + saturation * ohms) / thermal
    )
    current = (thermal / ohms) * lambert_w(argument) - saturation
    return Case(
        "diode and resistor",
        (
            "V1 in 0 DC 5",
            "R1 in a 1000",
            "D1 a 0 DEXACT",
            ".model DEXACT D(IS=1e-14 N=1)",
        ),
        Analysis(kind="op"),
        (
            # The budget covers the simulators' own shunt conductance
            # across the junction (gmin, 1 pS) and their slightly older
            # values of the physical constants.
            Check("diode current", lambda plot: -_op("i(v1)")(plot), current, 2e-8),
            Check("diode voltage", _op("v(a)"), volts - current * ohms, 2e-5),
        ),
    )


BUCK_VIN, BUCK_L, BUCK_C, BUCK_R = 12.0, 1e-5, 1e-4, 1.0
BUCK_DURATION = 4e-3
BUCK_PERIOD, BUCK_ON, BUCK_EDGE = 1e-5, 4e-6, 1e-9


def _buck() -> Case:
    # A buck cell with a 1 mohm switch, an almost ideal diode and linear L
    # and C, run for 4 ms (its start-up rings at 5 kHz and dies away in
    # about 1 ms) and measured over its last ten periods. In
    # steady state the inductor current rises for the on-time with
    # (Vin - Vout) across L, so its ripple is (Vin - Vout) * t_on / L,
    # and for a triangular current the capacitor's ripple is
    # dIL * T / (8 C). Both use the output the run itself settles at, so
    # the switch's and diode's small drops are not part of the error.
    def tail(plot: Plot, variable: str) -> tuple[float, ...]:
        begin = BUCK_DURATION - 10 * BUCK_PERIOD
        return tuple(
            y
            for t, y in zip(plot.real("time"), plot.real(variable), strict=True)
            if t >= begin
        )

    def inductor_ripple(plot: Plot) -> float:
        current = tail(plot, "i(l1)")
        return max(current) - min(current)

    def expected_inductor_ripple(plot: Plot) -> float:
        out = tail(plot, "v(out)")
        mean = sum(out) / len(out)
        # The gate is above its threshold for the on-time plus one edge.
        return (BUCK_VIN - mean) * (BUCK_ON + BUCK_EDGE) / BUCK_L

    def output_ripple(plot: Plot) -> float:
        out = tail(plot, "v(out)")
        return max(out) - min(out)

    common = (
        f"Vin in 0 DC {BUCK_VIN!r}",
        f"Vg g 0 PULSE(0 1 0 {BUCK_EDGE!r} {BUCK_EDGE!r} {BUCK_ON!r} {BUCK_PERIOD!r})",
        "D1 0 sw DI",
        f"L1 sw out {BUCK_L!r}",
        f"C1 out 0 {BUCK_C!r}",
        f"R1 out 0 {BUCK_R!r}",
        ".model DI D(IS=1e-12 N=0.01)",
    )
    return Case(
        "buck cell",
        (*common, "S1 in sw g 0 SWI", ".model SWI SW(RON=1e-3 ROFF=1e6 VT=0.5 VH=0)"),
        Analysis(kind="tran", duration="4ms"),  # pyright: ignore[reportArgumentType]
        (
            Check(
                "inductor ripple against (Vin - Vout) t_on / L, relative",
                lambda plot: inductor_ripple(plot) / expected_inductor_ripple(plot),
                1.0,
                1e-2,
            ),
            Check(
                "output ripple against dIL T / (8 C), relative",
                lambda plot: (
                    output_ripple(plot)
                    / (inductor_ripple(plot) * BUCK_PERIOD / (8 * BUCK_C))
                ),
                1.0,
                # The plan's threshold for switching ripple. Measured: Xyce is
                # within 0.8% and, with tighter settings, within 0.003%;
                # ngspice is 2.0% low with these settings and 1.7% high with
                # tighter ones, so its error does not shrink as they tighten.
                2.5e-2,
            ),
            Check(
                "output against D Vin, V",
                lambda plot: sum(tail(plot, "v(out)")) / len(tail(plot, "v(out)")),
                BUCK_VIN * BUCK_ON / BUCK_PERIOD,
                0.02,
            ),
        ),
        (
            *common,
            "S1 in sw g 0 SWI",
            ".model SWI VSWITCH(RON=1e-3 ROFF=1e6 VON=0.6 VOFF=0.4)",
        ),
    )


def _tempco() -> Case:
    # A 1k resistor with a first-order temperature coefficient of 1000
    # ppm/degC, at 127 degC against a nominal of 27 degC: 1100 ohm.
    return Case(
        "resistor with a temperature coefficient",
        ("V1 a 0 DC 11", "R1 a 0 1000 TC1=0.001"),
        Analysis(kind="op", temperature="127degC"),  # pyright: ignore[reportArgumentType]
        (Check("current at 127 degC", lambda plot: -_op("i(v1)")(plot), 0.01, 1e-9),),
    )


def cases() -> tuple[Case, ...]:
    """Every circuit of the set that Kairo can run so far.

    Returns:
        The cases, in the plan's order.
    """
    return (
        _ladder(),
        _rc_step(),
        _rlc_step(),
        _rc_ac(),
        _diode(),
        _buck(),
        _tempco(),
    )
