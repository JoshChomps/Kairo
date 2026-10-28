"""The calculations Kairo offers, each with its formula, source and directions.

Every entry is a `Calculation`: a formula written once, so that the same
code gives the nominal value, the worst case over tolerances, and the
interval enclosure. Each names where its equation comes from and declares,
for each input, which way the result moves as that input rises, where
that is provable; `tests/unit/test_calc_engine.py` checks every
declaration over a grid of points.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kairo.calc.interval import Interval, exp, log, log10, power, sqrt
from kairo.calc.spacing import spacing_formula
from kairo.calc.worst_case import Direction, Formula


@dataclass(frozen=True, slots=True)
class Calculation:
    """One calculation.

    Attributes:
        name: Its name, such as ``divider_output``.
        equation: The formula in the inputs' names, for a reader.
        inputs: Each input's name and unit.
        unit: The result's unit.
        formula: The formula, working on numbers and on intervals.
        directions: Which way the result moves as each input rises, for
            the inputs where that holds over every allowed value.
        source: Where the equation comes from.
        notes: Limits a reader of the result should know.
        above: Pairs of inputs of which the first must stay above the
            second for the formula, and its directions, to hold.
        below_one: Inputs that must stay below 1, such as a duty cycle
            the formula divides ``1 - D`` by.
    """

    name: str
    equation: str
    inputs: Mapping[str, str]
    unit: str
    formula: Formula
    directions: Mapping[str, Direction]
    source: str
    notes: tuple[str, ...] = ()
    above: tuple[tuple[str, str], ...] = ()
    below_one: tuple[str, ...] = ()


def problem(
    calc: Calculation, low: Mapping[str, float], high: Mapping[str, float]
) -> str | None:
    """Say why a box of input values is outside what a formula is for.

    Every input must be above zero, and each pair in the calculation's
    ``above`` must keep its order, at every point of the box. A formula's
    declared directions are only true there.

    Args:
        calc: The calculation.
        low: Each input's lowest value.
        high: Each input's highest value.

    Returns:
        The first reason found, or ``None`` if the whole box is in range.
    """
    for name in calc.inputs:
        if name in low and low[name] <= 0 and name not in _MAY_BE_ZERO:
            return f"{name} must be above zero"
    for greater, smaller in calc.above:
        if greater in low and smaller in high and low[greater] <= high[smaller]:
            return f"{greater} must be above {smaller}"
    for name in calc.below_one:
        if name in high and high[name] >= 1:
            return f"{name} must be below 1"
    return None


# Inputs that need not be above zero: a gain at zero frequency, a
# regulator with no quiescent current counted, an ambient below freezing.
_MAY_BE_ZERO = frozenset({"f", "i_q", "t_ambient", "temperature", "i_adj", "voltage"})


def _divider(v: Mapping[str, Any]) -> Any:
    # Written with one appearance of each resistor where possible, so the
    # interval result is as narrow as the arithmetic allows.
    return v["vin"] / (1.0 + v["r_top"] / v["r_bottom"])


def _divider_loaded(v: Mapping[str, Any]) -> Any:
    return v["vin"] / (1.0 + v["r_top"] * (1.0 / v["r_bottom"] + 1.0 / v["r_load"]))


def _led_current(v: Mapping[str, Any]) -> Any:
    return (v["v_supply"] - v["v_f"]) / v["r"]


def _diode_output(v: Mapping[str, Any]) -> Any:
    return v["v_in"] - v["v_f"]


def _resistor_power(v: Mapping[str, Any]) -> Any:
    return v["i"] * v["i"] * v["r"]


def _parallel_switch_loss(v: Mapping[str, Any]) -> Any:
    share = v["i"] * (1.0 + v["imbalance"] / 100.0) / v["count"]
    return share * share * v["r_on"]


def _precharge_energy(v: Mapping[str, Any]) -> Any:
    return 0.5 * v["c"] * v["v"] * v["v"]


def _precharge_peak_power(v: Mapping[str, Any]) -> Any:
    return v["v"] * v["v"] / v["r"]


def _precharge_time(v: Mapping[str, Any]) -> Any:
    return 5.0 * v["r"] * v["c"]


def _led_resistor_power(v: Mapping[str, Any]) -> Any:
    drop = v["v_supply"] - v["v_f"]
    return drop * drop / v["r"]


def _i2c_rp_min(v: Mapping[str, Any]) -> Any:
    return (v["vcc"] - v["vol_max"]) / v["iol"]


def _i2c_rp_max(v: Mapping[str, Any]) -> Any:
    return v["t_rise"] / (0.8473 * v["c_bus"])


def _rc_cutoff(v: Mapping[str, Any]) -> Any:
    return 1.0 / (2.0 * math.pi * v["r"] * v["c"])


def _rc_gain(v: Mapping[str, Any]) -> Any:
    ratio = 2.0 * math.pi * v["f"] * v["r"] * v["c"]
    return 1.0 / sqrt(1.0 + ratio * ratio)


def _rc_attenuation(v: Mapping[str, Any]) -> Any:
    ratio = 2.0 * math.pi * v["f"] * v["r"] * v["c"]
    return 10.0 * log10(1.0 + ratio * ratio)


def _lc_resonance(v: Mapping[str, Any]) -> Any:
    return 1.0 / (2.0 * math.pi * sqrt(v["l"] * v["c"]))


def _feedback_vout(v: Mapping[str, Any]) -> Any:
    return v["v_ref"] * (1.0 + v["r_top"] / v["r_bottom"])


def _adjustable_vout(v: Mapping[str, Any]) -> Any:
    return v["v_ref"] * (1.0 + v["r2"] / v["r1"]) + v["i_adj"] * v["r2"]


def _stress_ratio(v: Mapping[str, Any]) -> Any:
    return v["applied"] / v["rating"]


def _ldo_power(v: Mapping[str, Any]) -> Any:
    return (v["v_in"] - v["v_out"]) * v["i_out"] + v["v_in"] * v["i_q"]


def _ldo_junction(v: Mapping[str, Any]) -> Any:
    power = (v["v_in"] - v["v_out"]) * v["i_out"] + v["v_in"] * v["i_q"]
    return v["t_ambient"] + power * v["theta_ja"]


def _switcher_input(v: Mapping[str, Any]) -> Any:
    return v["v_out"] * v["i_out"] / (v["efficiency"] * v["v_in"])


def _buck_duty(v: Mapping[str, Any]) -> Any:
    return v["v_out"] / (v["v_in"] * v["efficiency"])


def _buck_ripple(v: Mapping[str, Any]) -> Any:
    return (v["v_in"] - v["v_out"]) * v["duty"] / (v["f_sw"] * v["l"])


def _buck_inductor(v: Mapping[str, Any]) -> Any:
    return v["v_out"] * (v["v_in"] - v["v_out"]) / (v["ripple"] * v["f_sw"] * v["v_in"])


def _buck_ripple_current(v: Mapping[str, Any]) -> Any:
    # Vout * (Vin - Vout) / Vin with Vin once, so an interval is not
    # widened more than it must be.
    return v["v_out"] * (1.0 - v["v_out"] / v["v_in"]) / (v["f_sw"] * v["l"])


def _buck_output_ripple(v: Mapping[str, Any]) -> Any:
    return v["ripple"] * (v["esr"] + 1.0 / (8.0 * v["f_sw"] * v["c"]))


def _holdup_time(v: Mapping[str, Any]) -> Any:
    return v["c"] * (v["v_start"] - v["v_floor"]) / v["i"]


def _series_current(v: Mapping[str, Any]) -> Any:
    return v["v"] / (v["r1"] + v["r2"])


def _programmed_current(v: Mapping[str, Any]) -> Any:
    return v["k"] / v["r"]


def _buck_peak(v: Mapping[str, Any]) -> Any:
    return v["ripple"] / 2.0 + v["i_out"]


_OHM = "Ohm's law and Kirchhoff's voltage law"
_I2C = (
    "Texas Instruments application note SLVA689A, 'I2C Bus Pullup Resistor "
    "Calculation' (February 2015, revised October 2026)"
)


def _boost_duty(v: Mapping[str, Any]) -> Any:
    return 1.0 - v["v_in"] * v["efficiency"] / v["v_out"]


def _boost_ripple(v: Mapping[str, Any]) -> Any:
    return v["v_in"] * v["duty"] / (v["f_sw"] * v["l"])


def _boost_inductor(v: Mapping[str, Any]) -> Any:
    # Vin * (Vout - Vin) / Vout written with Vout once.
    return v["v_in"] * (1.0 - v["v_in"] / v["v_out"]) / (v["ripple"] * v["f_sw"])


def _boost_peak(v: Mapping[str, Any]) -> Any:
    return v["ripple"] / 2.0 + v["i_out"] / (1.0 - v["duty"])


#: IPC-2221's constants for a trace's current: I = k * dT^0.44 * A^0.725,
#: with I in amps, dT in degrees Celsius and A in square mils; k is 0.048
#: for an external layer and 0.024 for an internal one.
IPC2221_K_EXTERNAL = 0.048
IPC2221_K_INTERNAL = 0.024
IPC2221_RISE_EXPONENT = 0.44
IPC2221_AREA_EXPONENT = 0.725
#: Millimetres in a mil (a thousandth of an inch, 25.4 mm exactly).
MM_PER_MIL = 0.0254
#: Annealed copper at 20 degC: resistivity 1/58 ohm mm^2/m and temperature
#: coefficient 0.00393 per degC (IEC 60028, "International standard of
#: resistance for copper").
COPPER_RESISTIVITY = 1.0 / 58.0
COPPER_TEMPERATURE_COEFFICIENT = 0.00393
COPPER_REFERENCE_TEMPERATURE = 20.0


def _trace_current(k: float) -> Formula:
    def formula(v: Mapping[str, Any]) -> Any:
        area = (v["width"] / MM_PER_MIL) * (v["thickness"] / MM_PER_MIL)
        return (
            k
            * power(v["temp_rise"], IPC2221_RISE_EXPONENT)
            * power(area, IPC2221_AREA_EXPONENT)
        )

    return formula


def _trace_width(k: float) -> Formula:
    def formula(v: Mapping[str, Any]) -> Any:
        area = power(
            v["current"] / (k * power(v["temp_rise"], IPC2221_RISE_EXPONENT)),
            1.0 / IPC2221_AREA_EXPONENT,
        )
        return area * MM_PER_MIL * MM_PER_MIL / v["thickness"]

    return formula


def _trace_resistance(v: Mapping[str, Any]) -> Any:
    warm = 1.0 + COPPER_TEMPERATURE_COEFFICIENT * (
        v["temperature"] - COPPER_REFERENCE_TEMPERATURE
    )
    # Resistivity is in ohm mm^2 per metre, so the length goes in metres.
    return (
        COPPER_RESISTIVITY
        * warm
        * (v["length"] / 1000.0)
        / (v["width"] * v["thickness"])
    )


def _trace_drop(v: Mapping[str, Any]) -> Any:
    return v["current"] * _trace_resistance(v)


#: The characteristic impedance of vacuum, mu0 * c, in ohms (CODATA 2018
#: recommended value).
VACUUM_IMPEDANCE = 376.730313668
#: Floating-point steps an iterated mean's ends are widened by: each of its
#: few dozen steps rounds at most once.
_AGM_STEPS = 64


def _mean(y: float) -> float:
    """The arithmetic-geometric mean of 1 and ``y``, for ``y`` above zero."""
    a, b = 1.0, y
    for _ in range(64):
        if abs(a - b) <= 1e-15 * a:
            break
        a, b = (a + b) / 2.0, math.sqrt(a * b)
    return a


def _agm_one(y: Any) -> Any:
    """The arithmetic-geometric mean of 1 and ``y``, a number or an interval.

    The mean rises with ``y``, so an interval's ends are the means of its
    ends, widened past rounding.
    """
    if not isinstance(y, Interval):
        return _mean(y)
    lo, hi = _mean(y.lo), _mean(y.hi)
    for _ in range(_AGM_STEPS):
        lo, hi = math.nextafter(lo, 0.0), math.nextafter(hi, math.inf)
    return Interval(lo, hi)


def _microstrip(v: Mapping[str, Any]) -> Any:
    er = v["er"]
    if not isinstance(er, Interval) and er < 1.0:
        raise ValueError("er must be at least 1")
    u = v["width"] / v["height"]
    f = 6.0 + (2.0 * math.pi - 6.0) * exp(-power(30.666 / u, 0.7528))
    z_air = (
        VACUUM_IMPEDANCE
        / (2.0 * math.pi)
        * log(f / u + sqrt(1.0 + power(2.0 / u, 2.0)))
    )
    a = (
        1.0
        + log((power(u, 4.0) + power(u / 52.0, 2.0)) / (power(u, 4.0) + 0.432)) / 49.0
        + log(1.0 + power(u / 18.1, 3.0)) / 18.7
    )
    b = 0.564 * power((er - 0.9) / (er + 3.0), 0.053)
    e_eff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * exp(-(a * b) * log(1.0 + 10.0 / u))
    return z_air / sqrt(e_eff)


def _stripline(v: Mapping[str, Any]) -> Any:
    # With k = sech(x) and k' = tanh(x), k' is the complement of k, so
    # K(k) / K(k') is the mean of 1 and k' over the mean of 1 and k
    # (K(k) = pi / (2 AGM(1, sqrt(1 - k^2)))), which stays exact for wide strips.
    x = math.pi * v["width"] / (2.0 * v["spacing"])
    # sech(x) = 2 e^-x / (1 + e^-2x), which cannot overflow for wide strips.
    m = exp(-2.0 * x)
    k = 2.0 * exp(-x) / (1.0 + m)
    k_prime = (1.0 - m) / (1.0 + m)
    return VACUUM_IMPEDANCE / (4.0 * sqrt(v["er"])) * _agm_one(k) / _agm_one(k_prime)


def _width_for(impedance_of: Formula, plane: str) -> Formula:
    """The width that gives a target impedance, found by bisection.

    A line's impedance falls as it widens, so one width gives each
    impedance; bisection in w/plane finds it to a part in 10^12. On
    intervals each end of the result is the width for an end of the
    inputs, widened past the search's tolerance.
    """

    def solve(target: float, plane_mm: float, er: float) -> float:
        def z(ratio: float) -> float:
            return float(
                impedance_of({"width": ratio * plane_mm, plane: plane_mm, "er": er})
            )

        # w/h from 1/10,000 to 100: the narrowest line to one wider than any
        # board's impedance needs (a few ohms).
        lo, hi = 1e-4, 100.0
        if not z(hi) <= target <= z(lo):
            raise ValueError(
                f"no width gives {target:g} ohm: the range is "
                f"{z(hi):.3g} to {z(lo):.3g}"
            )
        for _ in range(200):
            mid = (lo * hi) ** 0.5
            if z(mid) > target:
                lo = mid
            else:
                hi = mid
            if hi - lo <= 1e-12 * hi:
                break
        return (lo + hi) / 2.0 * plane_mm

    def formula(v: Mapping[str, Any]) -> Any:
        target, plane_mm, er = v["impedance"], v[plane], v["er"]
        if not any(isinstance(x, Interval) for x in (target, plane_mm, er)):
            return solve(target, plane_mm, er)
        point = {
            name: x if isinstance(x, Interval) else Interval.point(float(x))
            for name, x in (("impedance", target), ("plane", plane_mm), ("er", er))
        }
        # Wider for a lower target, a thicker dielectric and a lower er.
        narrowest = solve(point["impedance"].hi, point["plane"].lo, point["er"].hi)
        widest = solve(point["impedance"].lo, point["plane"].hi, point["er"].lo)
        return Interval(narrowest * (1.0 - 1e-9), widest * (1.0 + 1e-9))

    return formula


def _tanh(x: Any) -> Any:
    """tanh(x) for x at or above zero, as (1 - e^-2x) / (1 + e^-2x)."""
    m = exp(-2.0 * x)
    return (1.0 - m) / (1.0 + m)


def _stripline_pair(v: Mapping[str, Any]) -> Any:
    # The odd mode: k = tanh(pi w / 2b) / tanh(pi (w + s) / 2b), and
    # Z0o = eta0 / (4 sqrt(er)) K(k') / K(k), which is the mean of 1 and k'
    # over the mean of 1 and k. A pair driven differentially sees 2 Z0o.
    width, gap, spacing = v["width"], v["gap"], v["spacing"]
    k = _tanh(math.pi * width / (2.0 * spacing)) / _tanh(
        math.pi * (width + gap) / (2.0 * spacing)
    )
    k_prime = sqrt(1.0 - k * k)
    return (
        2.0 * VACUUM_IMPEDANCE / (4.0 * sqrt(v["er"])) * _agm_one(k_prime) / _agm_one(k)
    )


_MICROSTRIP = (
    "E. Hammerstad and O. Jensen, 'Accurate Models for Microstrip Computer-Aided "
    "Design', IEEE MTT-S International Microwave Symposium Digest, 1980, "
    "pp. 407-409: the impedance in air and the effective permittivity"
)
_MICROSTRIP_NOTES = (
    "for a trace of no thickness on a dielectric over a ground plane, with air "
    "above; copper thickness and solder mask lower it, commonly by a few percent",
    "its authors give the impedance in air within 0.03 % for w/h up to 1000 and "
    "the effective permittivity within 0.2 % for w/h from 0.01 to 100",
    "er is the laminate's at the signal's frequency, from its maker's data; it "
    "differs between makers and with frequency",
)
_STRIPLINE = (
    "S. B. Cohn, 'Characteristic Impedance of the Shielded-Strip Transmission "
    "Line', IRE Transactions on Microwave Theory and Techniques, MTT-2, July 1954, "
    "pp. 52-57: the exact solution, Z0 = eta0 / (4 sqrt(er)) K(k) / K(k'), "
    "k = sech(pi w / 2b)"
)
_STRIPLINE_PAIR = (
    "S. B. Cohn, 'Shielded Coupled-Strip Transmission Line', IRE Transactions "
    "on Microwave Theory and Techniques, MTT-3, October 1955, pp. 29-38: the "
    "exact odd-mode impedance, Z0o = eta0 / (4 sqrt(er)) K(k') / K(k), "
    "k = tanh(pi w / 2b) / tanh(pi (w + s) / 2b); differential is 2 Z0o"
)
_STRIPLINE_NOTES = (
    "exact for a trace of no thickness centred between two ground planes "
    "spacing apart, in one dielectric; copper thickness lowers it",
    "er is the laminate's at the signal's frequency, from its maker's data",
)
_BOOST = (
    "Texas Instruments application note SLVA372D, 'Basic Calculation of a Boost "
    "Converter's Power Stage' (November 2009, revised November 2022)"
)
_BOOST_NOTE = (
    "for a boost converter with an integrated switch in continuous conduction; "
    "the efficiency is an estimate unless taken from the converter's datasheet"
)
_IPC2221 = "IPC-2221, Generic Standard on Printed Board Design, conductor current chart"
_IPC2221_NOTE = (
    "IPC-2221's curve fit, from charts drawn for 10 to 100 degC of rise, up to "
    "35 A and 0.5 to 3 oz copper; approximate, and the newer IPC-2152, which is "
    "chart-based, generally allows more current"
)
_SPACING = (
    "IPC-2221 Table 6-1, Electrical Conductor Spacing: the larger of revision "
    "B (2012) and revision C (December 2023), read from published copies of "
    "the table (see kairo.calc.spacing)"
)
_SPACING_NOTES = (
    "the voltage is DC or AC peak; a voltage between two of the table's bands "
    "is put in the higher one",
    "the table presumes woven glass coated with resin, and a product safety "
    "standard (IEC 62368-1, UL 61010-1) may ask for more",
)
_PRECHARGE = (
    "Texas Instruments design guide TIDUF73 (September 2024), section 2.2.1, "
    "equations 1 to 4"
)
_PRECHARGE_NOTE = (
    "for a capacitor charged from 0 V through a resistor from a fixed source; "
    "a load drawing current during the charge adds to it"
)
_COPPER = (
    "R = rho * L / A with annealed copper's resistivity and temperature "
    "coefficient from IEC 60028"
)
_BUCK = (
    "Texas Instruments application note SLVA477C, 'Basic Calculation of a Buck "
    "Converter's Power Stage' (December 2011, revised September 2026)"
)
_BUCK_NOTE = (
    "for a buck converter with an integrated switch in continuous conduction; "
    "the efficiency is an estimate unless taken from the converter's datasheet"
)

CATALOGUE: dict[str, Calculation] = {
    c.name: c
    for c in (
        Calculation(
            "divider_output",
            "Vout = Vin * Rbottom / (Rtop + Rbottom)",
            {"vin": "V", "r_top": "ohm", "r_bottom": "ohm"},
            "V",
            _divider,
            {"vin": "rising", "r_top": "falling", "r_bottom": "rising"},
            _OHM,
            ("with no load on the output",),
        ),
        Calculation(
            "divider_output_loaded",
            "Vout = Vin * (Rbottom || Rload) / (Rtop + Rbottom || Rload)",
            {"vin": "V", "r_top": "ohm", "r_bottom": "ohm", "r_load": "ohm"},
            "V",
            _divider_loaded,
            {
                "vin": "rising",
                "r_top": "falling",
                "r_bottom": "rising",
                "r_load": "rising",
            },
            _OHM,
        ),
        Calculation(
            "led_current",
            "I = (Vsupply - Vf) / R",
            {"v_supply": "V", "v_f": "V", "r": "ohm"},
            "A",
            _led_current,
            {"v_supply": "rising", "v_f": "falling", "r": "falling"},
            _OHM,
            (
                "Vf is taken as given; a diode's forward voltage itself "
                "changes with current and temperature",
            ),
            above=(("v_supply", "v_f"),),
        ),
        Calculation(
            "diode_output",
            "Vout = Vin - Vf",
            {"v_in": "V", "v_f": "V"},
            "V",
            _diode_output,
            {"v_in": "rising", "v_f": "falling"},
            "Kirchhoff's voltage law across a diode in series",
            (
                "Vf is taken as given; give the datasheet's maximum at the "
                "rail's current for the low end, and its least for the high end",
            ),
            above=(("v_in", "v_f"),),
        ),
        Calculation(
            "led_resistor_power",
            "P = (Vsupply - Vf)^2 / R",
            {"v_supply": "V", "v_f": "V", "r": "ohm"},
            "W",
            _led_resistor_power,
            # True while the supply is above the forward voltage, which
            # the formula's use requires.
            {"v_supply": "rising", "v_f": "falling", "r": "falling"},
            "Joule's law with " + _OHM,
            above=(("v_supply", "v_f"),),
        ),
        Calculation(
            "resistor_power",
            "P = I^2 * R",
            {"i": "A", "r": "ohm"},
            "W",
            _resistor_power,
            {"r": "rising"},
            "Joule's law",
        ),
        Calculation(
            "i2c_pullup_min",
            "Rp(min) = (VCC - VOL(max)) / IOL",
            {"vcc": "V", "vol_max": "V", "iol": "A"},
            "ohm",
            _i2c_rp_min,
            {"vcc": "rising", "vol_max": "falling", "iol": "falling"},
            _I2C + ", equation 1",
            above=(("vcc", "vol_max"),),
        ),
        Calculation(
            "i2c_pullup_max",
            "Rp(max) = tr / (0.8473 * Cb)",
            {"t_rise": "s", "c_bus": "F"},
            "ohm",
            _i2c_rp_max,
            {"t_rise": "rising", "c_bus": "falling"},
            _I2C + ", equation 6",
            (
                "0.8473 is ln(0.7/0.3): the rise time is measured from 30% "
                "to 70% of VCC",
            ),
        ),
        Calculation(
            "rc_cutoff",
            "fc = 1 / (2 * pi * R * C)",
            {"r": "ohm", "c": "F"},
            "Hz",
            _rc_cutoff,
            {"r": "falling", "c": "falling"},
            "the -3 dB frequency of a first-order RC network",
        ),
        Calculation(
            "rc_lowpass_gain",
            "|H| = 1 / sqrt(1 + (2 * pi * f * R * C)^2)",
            {"f": "Hz", "r": "ohm", "c": "F"},
            "",
            _rc_gain,
            {"f": "falling", "r": "falling", "c": "falling"},
            "the magnitude response of a first-order RC low-pass network",
            ("the gain is a ratio of amplitudes, with no load on the output",),
        ),
        Calculation(
            "rc_lowpass_attenuation",
            "A = 10 * log10(1 + (2 * pi * f * R * C)^2)",
            {"f": "Hz", "r": "ohm", "c": "F"},
            "dB",
            _rc_attenuation,
            {"f": "rising", "r": "rising", "c": "rising"},
            "the magnitude response of a first-order RC low-pass network, "
            "|H| = 1 / sqrt(1 + (2 pi f R C)^2), as a loss in decibels, "
            "-20 log10 |H|",
            (
                "with no load on the output and a source of no resistance; a "
                "resistance or capacitance loading the output adds to the loss",
            ),
        ),
        Calculation(
            "lc_resonance",
            "f0 = 1 / (2 * pi * sqrt(L * C))",
            {"l": "H", "c": "F"},
            "Hz",
            _lc_resonance,
            {"l": "falling", "c": "falling"},
            "the resonant frequency of an ideal LC network (Thomson's formula)",
        ),
        Calculation(
            "feedback_divider_output",
            "Vout = Vref * (1 + Rtop / Rbottom)",
            {"v_ref": "V", "r_top": "ohm", "r_bottom": "ohm"},
            "V",
            _feedback_vout,
            {"v_ref": "rising", "r_top": "rising", "r_bottom": "falling"},
            "the feedback equation of a regulator that holds its feedback pin "
            "at Vref; " + _OHM,
            (
                "the feedback pin's own current is left out; a regulator's "
                "datasheet says when that matters",
            ),
        ),
        Calculation(
            "stress_ratio",
            "ratio = applied / rating",
            {"applied": "", "rating": ""},
            "",
            _stress_ratio,
            {"applied": "rising", "rating": "falling"},
            "definition of a stress ratio",
            (
                "a raw ratio; whether it is acceptable depends on a derating "
                "policy, and with none set the check is unverified",
            ),
        ),
        Calculation(
            "ldo_dissipation",
            "P = (Vin - Vout) * Iout + Vin * Iq",
            {"v_in": "V", "v_out": "V", "i_out": "A", "i_q": "A"},
            "W",
            _ldo_power,
            {"v_in": "rising", "v_out": "falling", "i_out": "rising", "i_q": "rising"},
            "conservation of power in a linear regulator",
            above=(("v_in", "v_out"),),
        ),
        Calculation(
            "ldo_junction_temperature",
            "TJ = TA + ((Vin - Vout) * Iout + Vin * Iq) * theta_JA",
            {
                "v_in": "V",
                "v_out": "V",
                "i_out": "A",
                "i_q": "A",
                "theta_ja": "degC/W",
                "t_ambient": "degC",
            },
            "degC",
            _ldo_junction,
            {
                "v_in": "rising",
                "v_out": "falling",
                "i_out": "rising",
                "i_q": "rising",
                "theta_ja": "rising",
                "t_ambient": "rising",
            },
            "conservation of power, and the definition of theta_JA (JEDEC JESD51-2A)",
            (
                "theta_JA is measured on the JEDEC test board in still air; "
                "the real board's copper changes it",
            ),
            above=(("v_in", "v_out"),),
        ),
        Calculation(
            "switcher_input_current",
            "Iin = Vout * Iout / (efficiency * Vin)",
            {"v_out": "V", "i_out": "A", "efficiency": "", "v_in": "V"},
            "A",
            _switcher_input,
            {
                "v_out": "rising",
                "i_out": "rising",
                "efficiency": "falling",
                "v_in": "falling",
            },
            "conservation of power in a switching regulator",
        ),
        Calculation(
            "buck_duty_cycle",
            "D = Vout / (Vin * efficiency)",
            {"v_out": "V", "v_in": "V", "efficiency": ""},
            "",
            _buck_duty,
            {"v_out": "rising", "v_in": "falling", "efficiency": "falling"},
            _BUCK + ", equation 1",
            (_BUCK_NOTE,),
        ),
        Calculation(
            "buck_inductor_ripple",
            "dIL = (Vin - Vout) * D / (fsw * L)",
            {"v_in": "V", "v_out": "V", "duty": "", "f_sw": "Hz", "l": "H"},
            "A",
            _buck_ripple,
            {
                "v_in": "rising",
                "v_out": "falling",
                "duty": "rising",
                "f_sw": "falling",
                "l": "falling",
            },
            _BUCK + ", equation 2",
            (_BUCK_NOTE,),
            above=(("v_in", "v_out"),),
        ),
        Calculation(
            "buck_inductor",
            "L = Vout * (Vin - Vout) / (dIL * fsw * Vin)",
            {"v_out": "V", "v_in": "V", "ripple": "A", "f_sw": "Hz"},
            "H",
            _buck_inductor,
            {"ripple": "falling", "f_sw": "falling", "v_in": "rising"},
            _BUCK + ", equation 5",
            (_BUCK_NOTE,),
            above=(("v_in", "v_out"),),
        ),
        Calculation(
            "buck_peak_switch_current",
            "ISW(max) = dIL / 2 + Iout(max)",
            {"ripple": "A", "i_out": "A"},
            "A",
            _buck_peak,
            {"ripple": "rising", "i_out": "rising"},
            _BUCK + ", equation 4",
            (_BUCK_NOTE,),
        ),
        Calculation(
            "holdup_time",
            "t = C * (Vstart - Vfloor) / I",
            {"c": "F", "v_start": "V", "v_floor": "V", "i": "A"},
            "s",
            _holdup_time,
            {"c": "rising", "v_start": "rising", "v_floor": "falling", "i": "falling"},
            "the defining relation of capacitance, i = C dv/dt, integrated for a "
            "constant current",
            (
                "the current drawn stays the same as the voltage falls: true of a "
                "stated load and of what a linear regulator passes on, on the "
                "safe side for a resistor, and not true of a switching "
                "regulator, which draws more as its input falls",
                "the capacitance is the same at every voltage on the way down; "
                "taking a class II ceramic's at the starting voltage is on the "
                "safe side where its capacitance falls with bias",
                "the step across the capacitors' series resistance when the "
                "supply goes, I times ESR, is left out",
            ),
            above=(("v_start", "v_floor"),),
        ),
        Calculation(
            "programmed_current",
            "I = K / R",
            {"k": "V", "r": "ohm"},
            "A",
            _programmed_current,
            {"k": "rising", "r": "falling"},
            "Texas Instruments BQ2407x datasheet SLUS810N (revised October "
            "2021): equation 1, page 21, IIN-MAX = KILIM / RILIM, and equation "
            "2, page 25, ICHG = KISET / RISET",
            (
                "K is the datasheet's factor in A*ohm, written in volts",
                "the chip holds this current only while nothing else limits "
                "it: an input current limit below it, its thermal regulation, "
                "or the state of the battery",
            ),
        ),
        Calculation(
            "series_resistors_current",
            "I = V / (R1 + R2)",
            {"v": "V", "r1": "ohm", "r2": "ohm"},
            "A",
            _series_current,
            {"v": "rising", "r1": "falling", "r2": "falling"},
            _OHM,
            ("nothing else may draw current from the point between them",),
        ),
        Calculation(
            "buck_ripple_current",
            "dIL = Vout * (Vin - Vout) / (Vin * fsw * L)",
            {"v_out": "V", "v_in": "V", "f_sw": "Hz", "l": "H"},
            "A",
            _buck_ripple_current,
            {"v_in": "rising", "f_sw": "falling", "l": "falling"},
            _BUCK + ", equation 2 with the duty cycle of equation 1 at an "
            "efficiency of 1",
            (
                "for a buck converter in continuous conduction; the voltage "
                "drops across the switches and the inductor are left out, as "
                "the note's own formula leaves them out",
            ),
            above=(("v_in", "v_out"),),
        ),
        Calculation(
            "buck_output_ripple",
            "dVout = dIL * (ESR + 1 / (8 * fsw * Cout))",
            {"ripple": "A", "esr": "ohm", "f_sw": "Hz", "c": "F"},
            "V",
            _buck_output_ripple,
            {
                "ripple": "rising",
                "esr": "rising",
                "f_sw": "falling",
                "c": "falling",
            },
            _BUCK + ", equations 12 and 13 added",
            (
                "the capacitance's part and the ESR's part reach their peaks at "
                "different moments of the cycle, so their sum is no less than "
                "the ripple the two cause together; the capacitor's inductance "
                "and the layout's are left out, and add spikes this does not "
                "show",
            ),
        ),
        Calculation(
            "boost_duty_cycle",
            "D = 1 - Vin(min) * efficiency / Vout",
            {"v_in": "V", "efficiency": "", "v_out": "V"},
            "",
            _boost_duty,
            {"v_in": "falling", "efficiency": "falling", "v_out": "rising"},
            _BOOST + ", equation 1",
            (_BOOST_NOTE,),
            above=(("v_out", "v_in"),),
        ),
        Calculation(
            "boost_inductor_ripple",
            "dIL = Vin(min) * D / (fsw * L)",
            {"v_in": "V", "duty": "", "f_sw": "Hz", "l": "H"},
            "A",
            _boost_ripple,
            {"v_in": "rising", "duty": "rising", "f_sw": "falling", "l": "falling"},
            _BOOST + ", equation 2",
            (_BOOST_NOTE,),
        ),
        Calculation(
            "boost_inductor",
            "L = Vin * (Vout - Vin) / (dIL * fsw * Vout)",
            {"v_in": "V", "v_out": "V", "ripple": "A", "f_sw": "Hz"},
            "H",
            _boost_inductor,
            {"v_out": "rising", "ripple": "falling", "f_sw": "falling"},
            _BOOST + ", equation 5",
            (_BOOST_NOTE,),
            above=(("v_out", "v_in"),),
        ),
        Calculation(
            "boost_peak_switch_current",
            "ISW(max) = dIL / 2 + Iout(max) / (1 - D)",
            {"ripple": "A", "i_out": "A", "duty": ""},
            "A",
            _boost_peak,
            {"ripple": "rising", "i_out": "rising", "duty": "rising"},
            _BOOST + ", equation 4",
            (_BOOST_NOTE,),
            below_one=("duty",),
        ),
        Calculation(
            "trace_current_external",
            "I = 0.048 * dT^0.44 * (W * T)^0.725, W and T in mils",
            {"width": "mm", "thickness": "mm", "temp_rise": "degC"},
            "A",
            _trace_current(IPC2221_K_EXTERNAL),
            {"width": "rising", "thickness": "rising", "temp_rise": "rising"},
            _IPC2221,
            (_IPC2221_NOTE,),
        ),
        Calculation(
            "trace_current_internal",
            "I = 0.024 * dT^0.44 * (W * T)^0.725, W and T in mils",
            {"width": "mm", "thickness": "mm", "temp_rise": "degC"},
            "A",
            _trace_current(IPC2221_K_INTERNAL),
            {"width": "rising", "thickness": "rising", "temp_rise": "rising"},
            _IPC2221,
            (_IPC2221_NOTE,),
        ),
        Calculation(
            "trace_width_external",
            "W = (I / (0.048 * dT^0.44))^(1 / 0.725) / T, W and T in mils",
            {"current": "A", "temp_rise": "degC", "thickness": "mm"},
            "mm",
            _trace_width(IPC2221_K_EXTERNAL),
            {"current": "rising", "temp_rise": "falling", "thickness": "falling"},
            _IPC2221,
            (_IPC2221_NOTE,),
        ),
        Calculation(
            "trace_width_internal",
            "W = (I / (0.024 * dT^0.44))^(1 / 0.725) / T, W and T in mils",
            {"current": "A", "temp_rise": "degC", "thickness": "mm"},
            "mm",
            _trace_width(IPC2221_K_INTERNAL),
            {"current": "rising", "temp_rise": "falling", "thickness": "falling"},
            _IPC2221,
            (_IPC2221_NOTE,),
        ),
        Calculation(
            "adjustable_regulator_output",
            "Vout = Vref * (1 + R2 / R1) + Iadj * R2",
            {"v_ref": "V", "r1": "ohm", "r2": "ohm", "i_adj": "A"},
            "V",
            _adjustable_vout,
            {"v_ref": "rising", "r1": "falling", "r2": "rising", "i_adj": "rising"},
            "Texas Instruments LM317 datasheet SLVS044Z (April 2025), equation 2",
            (
                "R1 joins the output to the adjust pin and R2 the adjust pin to "
                "ground; the adjust-pin current flows through R2",
            ),
        ),
        Calculation(
            "conductor_spacing_internal",
            "IPC-2221 Table 6-1, column B1 (internal conductors)",
            {"voltage": "V"},
            "mm",
            spacing_formula("B1"),
            {"voltage": "rising"},
            _SPACING,
            _SPACING_NOTES,
        ),
        Calculation(
            "conductor_spacing_external_uncoated",
            "IPC-2221 Table 6-1, column B2 (external conductors, uncoated, "
            "sea level to 3050 m)",
            {"voltage": "V"},
            "mm",
            spacing_formula("B2"),
            {"voltage": "rising"},
            _SPACING,
            _SPACING_NOTES,
        ),
        Calculation(
            "parallel_switch_loss",
            "P = (I * (1 + imbalance) / N)^2 * Ron",
            {"i": "A", "count": "", "r_on": "ohm", "imbalance": "%"},
            "W",
            _parallel_switch_loss,
            {
                "i": "rising",
                "count": "falling",
                "r_on": "rising",
                "imbalance": "rising",
            },
            "Joule's law for the switch carrying the most: an even share of the "
            "current, raised by the imbalance",
            (
                "Ron is the switch's at its working temperature, from its "
                "datasheet's curve; the imbalance is how far above an even share "
                "the worst one carries, a judgement unless measured",
            ),
        ),
        Calculation(
            "precharge_energy",
            "E = C * V^2 / 2, dissipated in the resistor",
            {"c": "F", "v": "V"},
            "J",
            _precharge_energy,
            {"c": "rising", "v": "rising"},
            _PRECHARGE,
            (_PRECHARGE_NOTE, "the same whatever the resistance (TIDUF73 eq. 4)"),
        ),
        Calculation(
            "precharge_peak_power",
            "P = V^2 / R, at the start of the charge",
            {"v": "V", "r": "ohm"},
            "W",
            _precharge_peak_power,
            {"v": "rising", "r": "falling"},
            _PRECHARGE,
            (_PRECHARGE_NOTE, "it decays with time constant R * C"),
        ),
        Calculation(
            "precharge_time",
            "t = 5 * R * C, to within 1% of the source",
            {"r": "ohm", "c": "F"},
            "s",
            _precharge_time,
            {"r": "rising", "c": "rising"},
            _PRECHARGE,
            (_PRECHARGE_NOTE,),
        ),
        Calculation(
            "trace_resistance",
            "R = rho20 * (1 + alpha * (T - 20)) * L / (W * t)",
            {"length": "mm", "width": "mm", "thickness": "mm", "temperature": "degC"},
            "ohm",
            _trace_resistance,
            {
                "length": "rising",
                "width": "falling",
                "thickness": "falling",
                "temperature": "rising",
            },
            _COPPER,
            (
                "for a straight run of uniform width; the temperature is the "
                "copper's own, ambient plus its rise",
                "trace_voltage_drop gives the drop at a current, and the "
                "dissipation is resistor_power",
            ),
        ),
        Calculation(
            "trace_voltage_drop",
            "V = I * rho20 * (1 + alpha * (T - 20)) * L / (W * t)",
            {
                "current": "A",
                "length": "mm",
                "width": "mm",
                "thickness": "mm",
                "temperature": "degC",
            },
            "V",
            _trace_drop,
            {
                "current": "rising",
                "length": "rising",
                "width": "falling",
                "thickness": "falling",
                "temperature": "rising",
            },
            f"Ohm's law, V = I R, with {_COPPER}",
            (
                "for a straight run of uniform width carrying a steady current; "
                "the temperature is the copper's own, ambient plus its rise",
            ),
        ),
        Calculation(
            "microstrip_impedance",
            "Z0 = Z_air(w/h) / sqrt(e_eff(w/h, er)) (Hammerstad and Jensen)",
            {"width": "mm", "height": "mm", "er": ""},
            "ohm",
            _microstrip,
            {"width": "falling", "height": "rising", "er": "falling"},
            _MICROSTRIP,
            _MICROSTRIP_NOTES,
        ),
        Calculation(
            "microstrip_width",
            "w such that microstrip_impedance(w, h, er) = Z0",
            {"impedance": "ohm", "height": "mm", "er": ""},
            "mm",
            _width_for(_microstrip, "height"),
            {"impedance": "falling", "height": "rising", "er": "falling"},
            _MICROSTRIP,
            (
                *_MICROSTRIP_NOTES,
                "solved from microstrip_impedance by bisection, to a part in 10^12",
            ),
        ),
        Calculation(
            "stripline_width",
            "w such that stripline_impedance(w, b, er) = Z0",
            {"impedance": "ohm", "spacing": "mm", "er": ""},
            "mm",
            _width_for(_stripline, "spacing"),
            {"impedance": "falling", "spacing": "rising", "er": "falling"},
            _STRIPLINE,
            (
                *_STRIPLINE_NOTES,
                "solved from stripline_impedance by bisection, to a part in 10^12",
            ),
        ),
        Calculation(
            "stripline_differential_impedance",
            "Zdiff = 2 Z0o, Z0o = eta0 / (4 sqrt(er)) K(k') / K(k) (Cohn 1955)",
            {"width": "mm", "gap": "mm", "spacing": "mm", "er": ""},
            "ohm",
            _stripline_pair,
            {"width": "falling", "gap": "rising", "spacing": "rising", "er": "falling"},
            _STRIPLINE_PAIR,
            (
                "exact for two traces of no thickness, each width wide and gap "
                "apart, centred between two ground planes spacing apart in one "
                "dielectric; copper thickness lowers it",
                "er is the laminate's at the signal's frequency, from its maker's data",
            ),
        ),
        Calculation(
            "stripline_impedance",
            "Z0 = eta0 / (4 sqrt(er)) * K(sech x) / K(tanh x), x = pi w / (2 b)",
            {"width": "mm", "spacing": "mm", "er": ""},
            "ohm",
            _stripline,
            {"width": "falling", "spacing": "rising", "er": "falling"},
            _STRIPLINE,
            _STRIPLINE_NOTES,
        ),
    )
}
