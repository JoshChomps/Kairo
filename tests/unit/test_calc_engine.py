from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kairo.calc import (
    CATALOGUE,
    CalcResult,
    Given,
    Interval,
    calculate,
    check_limit,
    judge,
    worst_case,
)
from kairo.calc.catalogue import problem
from kairo.calc.interval import power, sqrt
from kairo.units import Quantity

# A realistic value for each input name, so every formula is tried where
# it is meant to be used (a supply above a forward voltage, and so on).
TYPICAL: dict[str, float] = {
    "vin": 5.0, "r_top": 10e3, "r_bottom": 4.7e3, "r_load": 100e3,
    "v_supply": 3.3, "v_f": 2.0, "r": 1e3, "i": 0.02,
    "vcc": 3.3, "vol_max": 0.4, "iol": 3e-3, "t_rise": 300e-9, "c_bus": 200e-12,
    "c": 100e-9, "f": 5e3, "l": 10e-6, "v_ref": 0.8,
    "applied": 3.3, "rating": 10.0,
    "v_out": 3.3, "v_in": 12.0, "efficiency": 0.9, "duty": 0.3, "f_sw": 500e3,
    "ripple": 0.6, "i_out": 2.0, "i_q": 0.005, "theta_ja": 90.0, "t_ambient": 40.0,
    "width": 0.5, "thickness": 0.035, "temp_rise": 20.0, "current": 2.0,
    "length": 50.0, "temperature": 60.0, "voltage": 48.0,
    "r1": 240.0, "r2": 715.0, "i_adj": 50e-6, "esr": 0.05, "v": 5.0,
    "v_start": 5.0, "v_floor": 2.0, "k": 890.0,
    "count": 4.0, "r_on": 2.2e-3, "imbalance": 15.0,
    "height": 0.3, "er": 4.3, "spacing": 0.6, "impedance": 50.0, "gap": 0.2,
}  # fmt: skip


def stated(name: str, value: float, unit: str, tolerance: float | None = None) -> Given:
    return Given(
        name,
        Quantity(value, unit),
        tolerance=tolerance,
        source="user-stated" if tolerance is not None else "missing",
    )


def inputs_for(name: str, tolerance: float | None) -> dict[str, Given]:
    calc = CATALOGUE[name]
    # A boost converter's output is above its input, unlike the others'.
    typical = {**TYPICAL, "v_out": 24.0} if name.startswith("boost") else TYPICAL
    return {
        key: stated(key, typical[key], unit, tolerance)
        for key, unit in calc.inputs.items()
    }


# Intervals

finite = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False)


@given(a=finite, b=finite, c=finite, d=finite, x=st.floats(0, 1), y=st.floats(0, 1))
@settings(max_examples=300, deadline=None)
def test_an_interval_result_always_holds_the_true_result(
    a: float, b: float, c: float, d: float, x: float, y: float
) -> None:
    first = Interval(min(a, b), max(a, b))
    second = Interval(min(c, d), max(c, d))
    # Rounding can carry a sampled point a hair past an end, so it is put
    # back inside: the claim is about points that are in the intervals.
    p = min(max(first.lo + (first.hi - first.lo) * x, first.lo), first.hi)
    q = min(max(second.lo + (second.hi - second.lo) * y, second.lo), second.hi)
    assert (first + second).contains(p + q)
    assert (first - second).contains(p - q)
    assert (first * second).contains(p * q)
    assert (3.0 - first).contains(3.0 - p)
    # A divisor within a hair of zero overflows, which an interval refuses
    # outright; that is tested on its own below.
    if not second.contains(0.0) and min(abs(second.lo), abs(second.hi)) > 1e-9:
        assert (first / second).contains(p / q)
        assert (1.0 / second).contains(1.0 / q)
    if first.lo >= 0:
        root = sqrt(first)
        assert isinstance(root, Interval) and root.contains(math.sqrt(p))


def test_an_interval_refuses_what_has_no_answer() -> None:
    with pytest.raises(ZeroDivisionError):
        _ = Interval(1, 2) / Interval(-1, 1)
    with pytest.raises(ValueError, match="wrong way round"):
        Interval(2, 1)
    with pytest.raises(ValueError, match="finite"):
        Interval(0, math.inf)
    with pytest.raises(ValueError, match="negative"):
        sqrt(Interval(-1, 4))
    with pytest.raises(ValueError, match="finite"):
        _ = Interval(1, 2) / Interval(1e-320, 1e-310)
    # Each operation moves the ends outward, never inward.
    total = Interval.point(0.1) + Interval.point(0.2)
    assert total.lo < 0.1 + 0.2 < total.hi


# Worst cases


def test_declared_directions_make_the_corners_exact() -> None:
    case = worst_case(
        CATALOGUE["divider_output"].formula,
        {"vin": 5.0, "r_top": 10e3, "r_bottom": 10e3},
        {"vin": (4.9, 5.1), "r_top": (9900, 10100), "r_bottom": (9900, 10100)},
        CATALOGUE["divider_output"].directions,
    )
    assert (case.method, case.exact, case.evaluations) == ("monotonic corners", True, 2)
    # Lowest: low supply, high top resistor, low bottom resistor.
    assert case.at_minimum == {"vin": 4.9, "r_top": 10100, "r_bottom": 9900}
    assert case.minimum == pytest.approx(4.9 * 9900 / 20000)
    assert case.maximum == pytest.approx(5.1 * 10100 / 20000)
    assert case.enclosure is not None
    assert case.enclosure.contains(case.minimum) and case.enclosure.contains(
        case.maximum
    )


def hump(v: Mapping[str, Any]) -> Any:
    return v["x"] * (1.0 - v["x"]) * v["k"]


def test_an_extreme_inside_the_range_is_found_by_search() -> None:
    # x * (1 - x) peaks at x = 0.5, inside the range; both ends give less.
    case = worst_case(
        hump, {"x": 0.3, "k": 2.0}, {"x": (0.1, 0.9), "k": (1.9, 2.1)}, {"k": "rising"}
    )
    assert (case.method, case.exact) == ("search", False)
    assert case.maximum == pytest.approx(0.25 * 2.1, rel=1e-9)
    assert case.at_maximum["x"] == pytest.approx(0.5, abs=1e-6)
    assert case.at_maximum["k"] == 2.1
    assert case.minimum == pytest.approx(0.09 * 1.9)
    # The same search gives the same answer and the same work every time.
    again = worst_case(
        hump, {"x": 0.3, "k": 2.0}, {"x": (0.1, 0.9), "k": (1.9, 2.1)}, {"k": "rising"}
    )
    assert again == case
    # Corners alone would have missed it.
    corners = max(hump({"x": x, "k": 2.1}) for x in (0.1, 0.9))
    assert corners < case.maximum


def test_a_limit_is_proven_failed_or_left_unverified() -> None:
    case = worst_case(
        hump, {"x": 0.3, "k": 2.0}, {"x": (0.1, 0.9), "k": (1.9, 2.1)}, {"k": "rising"}
    )
    assert case.enclosure is not None
    # A point that is reached breaks the limit: fail.
    assert judge(case, 0.5, "at most") == "fail"
    # The guaranteed enclosure meets the limit: pass, though it was a search.
    assert judge(case, case.enclosure.hi + 0.01, "at most") == "pass"
    # Between the worst found and the enclosure nothing is proven.
    between = (case.maximum + case.enclosure.hi) / 2
    assert case.maximum < between < case.enclosure.hi
    assert judge(case, between, "at most") == "unverified"
    assert judge(case, 0.0, "at least") == "pass"
    exact = worst_case(hump, {"x": 0.3, "k": 2.0}, {"k": (1.9, 2.1)}, {"k": "rising"})
    assert judge(exact, exact.maximum, "at most") == "pass"


def test_a_search_stays_within_its_budget_and_refuses_what_it_cannot_cover() -> None:
    def many(v: Mapping[str, Any]) -> Any:
        return sum(v[name] for name in v)

    nominal = {f"x{n}": 1.0 for n in range(4)}
    ranges = {name: (0.0, 2.0) for name in nominal}
    case = worst_case(many, nominal, ranges, {}, budget=500)
    assert case.evaluations <= 500
    assert (case.minimum, case.maximum) == (0.0, 8.0)
    with pytest.raises(ValueError, match="budget"):
        worst_case(many, nominal, ranges, {}, budget=10)
    with pytest.raises(ValueError, match="not an input"):
        worst_case(many, nominal, {"y": (0, 1)}, {})


# The catalogue


@pytest.mark.parametrize("name", sorted(CATALOGUE))
def test_every_declared_direction_holds_over_a_grid(name: str) -> None:
    calc = CATALOGUE[name]
    spans = {key: (TYPICAL[key] * 0.7, TYPICAL[key] * 1.3) for key in calc.inputs}
    if name.startswith("buck"):
        spans["v_out"] = (2.0, 5.0)
    if name.startswith("boost"):
        # A boost converter's output is above its input.
        spans["v_out"] = (20.0, 30.0)
    steps = 5
    grid = {
        key: [low + (high - low) * n / (steps - 1) for n in range(steps)]
        for key, (low, high) in spans.items()
    }
    checked = 0
    for key, direction in calc.directions.items():
        others = [k for k in calc.inputs if k != key]
        for combination in itertools.product(*(grid[k] for k in others)):
            point = dict(zip(others, combination, strict=True))
            # A direction is declared for the values the formula is for.
            if any(
                problem(calc, {**point, key: x}, {**point, key: x}) for x in grid[key]
            ):
                continue
            checked += 1
            values = [float(calc.formula({**point, key: x})) for x in grid[key]]
            steps_up = [b - a for a, b in itertools.pairwise(values)]
            if direction == "rising":
                assert all(step >= -1e-12 * abs(values[0]) for step in steps_up), (
                    name,
                    key,
                )
            else:
                assert all(step <= 1e-12 * abs(values[0]) for step in steps_up), (
                    name,
                    key,
                )
    assert set(calc.directions) <= set(calc.inputs)
    assert checked >= len(calc.directions)


def test_a_formula_is_refused_outside_the_values_it_is_for() -> None:
    # An LED fed from less than its forward voltage: no current flows, and
    # the formula would give a negative one.
    with pytest.raises(ValueError, match="v_supply must be above v_f"):
        calculate(
            "led_current",
            {
                "v_supply": stated("Vsupply", 1.8, "V"),
                "v_f": stated("Vf", 2.0, "V"),
                "r": stated("R", 1e3, "ohm"),
            },
        )
    with pytest.raises(ValueError, match="r must be above zero"):
        calculate(
            "resistor_power", {"i": stated("I", 0.1, "A"), "r": stated("R", 0, "ohm")}
        )
    # Fine at nominal, but the tolerances let the supply fall below the
    # forward voltage: the worst case is not computed, and says why.
    result = calculate(
        "led_current",
        {
            "v_supply": stated("Vsupply", 2.1, "V", 10.0),
            "v_f": stated("Vf", 2.0, "V", 10.0),
            "r": stated("R", 1e3, "ohm", 1.0),
        },
    )
    assert result.status == "ok" and result.corners is not None
    assert result.corners.status == "unverified"
    assert "v_supply must be above v_f" in result.corners.missing[0]
    assert result.accuracy.status == "unverified"


@pytest.mark.parametrize("name", sorted(CATALOGUE))
def test_every_worst_case_lies_inside_its_enclosure(name: str) -> None:
    result = calculate(name, inputs_for(name, 5.0))
    assert result.status == "ok" and result.case is not None
    case = result.case
    assert case.enclosure is not None, "the formula does not run on intervals"
    assert case.enclosure.contains(case.minimum)
    assert case.enclosure.contains(case.maximum)
    assert result.value is not None
    assert case.minimum <= result.value.value <= case.maximum
    assert result.accuracy.status == "verified"
    assert result.accuracy.worst_case["scope"] == "initial"


def test_ti_worked_example_for_i2c_pullups() -> None:
    # SLVA689, section 4: fast mode, Cb = 200 pF, VCC = 3.3 V, with
    # VOL(max) = 0.4 V at IOL = 3 mA and tr = 300 ns from its table 1.
    # It gives Rp(min) = 966.667 ohm and Rp(max) = 1.77 kohm.
    low = calculate(
        "i2c_pullup_min",
        {
            "vcc": stated("VCC", 3.3, "V"),
            "vol_max": stated("VOL(max)", 0.4, "V"),
            "iol": stated("IOL", 3e-3, "A"),
        },
    )
    high = calculate(
        "i2c_pullup_max",
        {"t_rise": stated("tr", 300e-9, "s"), "c_bus": stated("Cb", 200e-12, "F")},
    )
    assert low.value is not None and high.value is not None
    assert round(low.value.value, 3) == 966.667
    assert round(high.value.value / 1000, 2) == 1.77
    assert "SLVA689" in low.source and "equation 6" in high.source


@pytest.mark.parametrize(
    ("name", "values", "want"),
    [
        # Worked by hand from each equation; the sources give no numbers.
        ("divider_output", {"vin": 12, "r_top": 30e3, "r_bottom": 10e3}, 3.0),
        ("divider_output", {"vin": 5, "r_top": 10e3, "r_bottom": 10e3}, 2.5),
        (
            "divider_output_loaded",
            {"vin": 5, "r_top": 10e3, "r_bottom": 10e3, "r_load": 10e3},
            5 * 5e3 / 15e3,
        ),
        ("led_current", {"v_supply": 3.3, "v_f": 2.0, "r": 1e3}, 1.3e-3),
        ("led_current", {"v_supply": 5.0, "v_f": 2.0, "r": 150}, 0.02),
        ("led_resistor_power", {"v_supply": 5.0, "v_f": 2.0, "r": 150}, 0.06),
        ("diode_output", {"v_in": 5.0, "v_f": 0.5}, 4.5),
        ("resistor_power", {"i": 0.02, "r": 150}, 0.06),
        # 1000 ns into 400 pF, the standard-mode limits of SLVA689 table 1.
        (
            "i2c_pullup_max",
            {"t_rise": 1000e-9, "c_bus": 400e-12},
            1e-6 / (0.8473 * 4e-10),
        ),
        ("rc_cutoff", {"r": 1e3, "c": 1e-6}, 159.1549),
        ("rc_cutoff", {"r": 10e3, "c": 100e-9}, 159.1549),
        ("rc_lowpass_gain", {"f": 159.1549431, "r": 1e3, "c": 1e-6}, 1 / math.sqrt(2)),
        ("rc_lowpass_gain", {"f": 0.0, "r": 1e3, "c": 1e-6}, 1.0),
        # At the cutoff the loss is 10 log10(2); at ten times it, 10 log10(101).
        (
            "rc_lowpass_attenuation",
            {"f": 159.1549431, "r": 1e3, "c": 1e-6},
            3.0103,
        ),
        (
            "rc_lowpass_attenuation",
            {"f": 1591.549431, "r": 1e3, "c": 1e-6},
            20.0432,
        ),
        ("lc_resonance", {"l": 10e-6, "c": 100e-9}, 159154.94),
        (
            "feedback_divider_output",
            {"v_ref": 0.8, "r_top": 31.6e3, "r_bottom": 10e3},
            3.328,
        ),
        (
            "feedback_divider_output",
            {"v_ref": 1.25, "r_top": 240, "r_bottom": 240},
            2.5,
        ),
        ("stress_ratio", {"applied": 3.3, "rating": 10.0}, 0.33),
        ("buck_duty_cycle", {"v_out": 3.3, "v_in": 12.0, "efficiency": 0.9}, 0.305556),
        ("buck_duty_cycle", {"v_out": 5.0, "v_in": 24.0, "efficiency": 0.85}, 0.245098),
        (
            "buck_inductor_ripple",
            {"v_in": 12, "v_out": 3.3, "duty": 0.305556, "f_sw": 500e3, "l": 10e-6},
            0.531667,
        ),
        (
            "buck_inductor",
            {"v_out": 3.3, "v_in": 12, "ripple": 0.6, "f_sw": 500e3},
            3.3 * 8.7 / (0.6 * 500e3 * 12),
        ),
        ("buck_peak_switch_current", {"ripple": 0.6, "i_out": 2.0}, 2.3),
        # The same two cases as the LDO tests.
        (
            "ldo_dissipation",
            {"v_in": 5, "v_out": 3.3, "i_out": 0.3013, "i_q": 0.005},
            0.53721,
        ),
        (
            "ldo_dissipation",
            {"v_in": 12, "v_out": 5, "i_out": 0.1, "i_q": 0.006},
            0.772,
        ),
        (
            "ldo_junction_temperature",
            {
                "v_in": 12,
                "v_out": 5,
                "i_out": 0.1,
                "i_q": 0.006,
                "theta_ja": 50,
                "t_ambient": 25,
            },
            63.6,
        ),
        # 3.3 V to 5 V at 80%: 1 - 2.64 / 5. 5 V to 12 V at 85%: 1 - 4.25 / 12.
        ("boost_duty_cycle", {"v_in": 3.3, "efficiency": 0.8, "v_out": 5.0}, 0.472),
        (
            "boost_duty_cycle",
            {"v_in": 5.0, "efficiency": 0.85, "v_out": 12.0},
            0.645833,
        ),
        # 3.3 * 0.472 / (1 MHz * 4.7 uH) = 1.5576 / 4.7.
        (
            "boost_inductor_ripple",
            {"v_in": 3.3, "duty": 0.472, "f_sw": 1e6, "l": 4.7e-6},
            0.331404,
        ),
        # 3.3 * 1.7 / (0.3 * 1 MHz * 5) = 5.61 / 1.5e6.
        (
            "boost_inductor",
            {"v_in": 3.3, "v_out": 5.0, "ripple": 0.3, "f_sw": 1e6},
            3.74e-6,
        ),
        # 0.331404 / 2 + 0.5 / 0.528 = 0.165702 + 0.946970.
        (
            "boost_peak_switch_current",
            {"ripple": 0.331404, "i_out": 0.5, "duty": 0.472},
            1.112672,
        ),
        # 1/58 ohm mm^2/m * 0.1 m / (0.25 mm * 0.035 mm) = 0.0017241 / 0.00875,
        # and 19.65% more at 70 degC (0.00393 * 50).
        (
            "trace_resistance",
            {"length": 100, "width": 0.25, "thickness": 0.035, "temperature": 20},
            0.197044,
        ),
        (
            "trace_resistance",
            {"length": 100, "width": 0.25, "thickness": 0.035, "temperature": 70},
            0.235763,
        ),
        # 2 A through the 0.197044 ohm run above.
        (
            "trace_voltage_drop",
            {
                "current": 2.0,
                "length": 100,
                "width": 0.25,
                "thickness": 0.035,
                "temperature": 20,
            },
            0.394088,
        ),
        # The LM317's equation: 1.25 * (1 + 715 / 240) = 4.97396, plus
        # 50 uA * 715 ohm = 0.03575.
        (
            "adjustable_regulator_output",
            {"v_ref": 1.25, "r1": 240, "r2": 715, "i_adj": 50e-6},
            5.009708,
        ),
        (
            "adjustable_regulator_output",
            {"v_ref": 1.25, "r1": 240, "r2": 240, "i_adj": 0},
            2.5,
        ),
        # 3.3 V at 1 A from 12 V at 90%: 3.3 W out, 3.667 W in, 0.3056 A.
        (
            "switcher_input_current",
            {"v_out": 3.3, "i_out": 1.0, "efficiency": 0.9, "v_in": 12},
            0.305556,
        ),
    ],
)
def test_each_formula_gives_the_value_worked_by_hand(
    name: str, values: dict[str, float], want: float
) -> None:
    calc = CATALOGUE[name]
    result = calculate(
        name,
        {key: stated(key, value, calc.inputs[key]) for key, value in values.items()},
    )
    assert result.value is not None
    assert result.value.value == pytest.approx(want, rel=1e-5)
    assert result.value.unit == calc.unit


@pytest.mark.parametrize(
    ("values", "want"),
    [
        # Pozar, Microwave Engineering, 3rd ed., example 3.7: a 50 ohm line
        # on 1.27 mm of er 2.20 is w/d = 3.081 wide (his closed form).
        ({"width": 3.913, "height": 1.27, "er": 2.2}, 50.0),
        # The 50 ohm line usually shown for 1.6 mm FR-4 at er 4.4: 3.06 mm.
        ({"width": 3.06, "height": 1.6, "er": 4.4}, 50.0),
    ],
)
def test_microstrip_impedance_agrees_with_published_designs(
    values: dict[str, float], want: float
) -> None:
    # The published widths come from simpler formulas, good to about 1%;
    # Hammerstad and Jensen's are finer, so they agree within that.
    calc = CATALOGUE["microstrip_impedance"]
    assert float(calc.formula(values)) == pytest.approx(want, rel=0.01)


def test_stripline_impedance_agrees_with_a_published_design_and_its_exact_laws() -> (
    None
):
    calc = CATALOGUE["stripline_impedance"]
    # Pozar, example 3.5: 50 ohm between planes 3.2 mm apart in er 2.20 is
    # 2.66 mm wide (his fit to the exact solution, good to about 1%).
    z = float(calc.formula({"width": 2.66, "spacing": 3.2, "er": 2.2}))
    assert z == pytest.approx(50.0, rel=0.01)
    # Exact laws of the solution: only w/b matters, and Z0 goes as 1/sqrt(er).
    assert float(
        calc.formula({"width": 5.32, "spacing": 6.4, "er": 2.2})
    ) == pytest.approx(z, rel=1e-12)
    assert float(
        calc.formula({"width": 2.66, "spacing": 3.2, "er": 8.8})
    ) == pytest.approx(z / 2.0, rel=1e-12)


def test_microstrip_refuses_a_permittivity_below_air() -> None:
    with pytest.raises(ValueError, match="er must be at least 1"):
        CATALOGUE["microstrip_impedance"].formula(
            {"width": 1.0, "height": 1.0, "er": 0.5}
        )


@pytest.mark.parametrize(
    ("layer", "k", "known_mils"),
    # 1 A with 10 degC of rise in 1 oz copper is the case trace-width
    # calculators built on IPC-2221 are usually shown with: about 11.8 mil
    # on an outer layer and 30.8 mil on an inner one.
    [("external", 0.048, 11.8), ("internal", 0.024, 30.8)],
)
def test_ipc_2221_trace_width_and_current_are_each_others_inverse(
    layer: str, k: float, known_mils: float
) -> None:
    thickness = 0.035
    # The same formula by another route: logarithms instead of powers.
    area = math.exp(math.log(1.0 / k) / 0.725 - 0.44 / 0.725 * math.log(10.0))
    want = area * 0.0254 * 0.0254 / thickness
    width = calculate(
        f"trace_width_{layer}",
        {
            "current": stated("current", 1.0, "A"),
            "temp_rise": stated("temp_rise", 10.0, "degC"),
            "thickness": stated("thickness", thickness, "mm"),
        },
    )
    assert width.value is not None and width.value.unit == "mm"
    assert width.value.value == pytest.approx(want, rel=1e-9)
    assert width.value.value / 0.0254 == pytest.approx(known_mils, abs=0.06)
    assert "IPC-2221" in width.source
    current = calculate(
        f"trace_current_{layer}",
        {
            "width": stated("width", width.value.value, "mm"),
            "thickness": stated("thickness", thickness, "mm"),
            "temp_rise": stated("temp_rise", 10.0, "degC"),
        },
    )
    assert current.value is not None
    assert current.value.value == pytest.approx(1.0, rel=1e-9)
    # Twice the copper carries 2^0.725 times the current, not twice.
    doubled = calculate(
        f"trace_current_{layer}",
        {
            "width": stated("width", 2 * width.value.value, "mm"),
            "thickness": stated("thickness", thickness, "mm"),
            "temp_rise": stated("temp_rise", 10.0, "degC"),
        },
    )
    assert doubled.value is not None
    assert doubled.value.value == pytest.approx(2**0.725, rel=1e-9)


@given(
    lo=st.floats(1e-6, 1e6),
    spread=st.floats(1.0, 10.0),
    share=st.floats(0, 1),
    exponent=st.floats(-3, 3),
)
@settings(max_examples=300, deadline=None)
def test_a_power_of_an_interval_holds_every_power(
    lo: float, spread: float, share: float, exponent: float
) -> None:
    box = Interval(lo, lo * spread)
    point = min(max(lo + (box.hi - lo) * share, box.lo), box.hi)
    result = power(box, exponent)
    assert isinstance(result, Interval) and result.contains(math.pow(point, exponent))
    with pytest.raises(ValueError, match="not above zero"):
        power(Interval(0.0, 1.0), 0.5)
    with pytest.raises(ValueError, match="not above zero"):
        power(-1.0, 0.5)
    assert power(8.0, 1 / 3) == pytest.approx(2.0)


def test_every_calculation_names_its_source_and_inputs() -> None:
    for calc in CATALOGUE.values():
        assert len(calc.source) > 10 and calc.equation and calc.inputs
        assert set(calc.inputs) <= set(TYPICAL)


# Running one


def test_a_missing_input_makes_the_result_unverified_and_says_which() -> None:
    result = calculate("led_current", {"v_supply": stated("Vsupply", 3.3, "V")})
    assert (result.status, result.value, result.text()) == (
        "unverified",
        None,
        "unverified",
    )
    assert [m.name for m in result.missing] == ["v_f", "r"]
    assert result.accuracy.status == "unverified"
    assert any("v_f" in line for line in result.accuracy.to_resolve)


def test_an_input_with_no_tolerance_is_never_given_one() -> None:
    inputs = inputs_for("led_current", 5.0)
    inputs["v_f"] = stated("v_f", 2.0, "V")
    result = calculate("led_current", inputs)
    assert result.status == "ok" and result.corners is not None
    assert result.corners.status == "unverified"
    assert result.corners.missing == ("no tolerance on v_f",)
    assert result.case is None and result.accuracy.status == "unverified"
    assert result.accuracy.worst_case["status"] == "unverified"
    assert check_limit(result, 1.0, "at most") == "unverified"
    # A nominal value that already breaks the limit is a failure all the same.
    assert check_limit(result, 0.001, "at most") == "fail"
    assert result.as_dict()["corners"] == {
        "status": "unverified",
        "missing": ["no tolerance on v_f"],
    }


def test_a_limit_is_judged_at_the_worst_case_not_at_nominal() -> None:
    # 1.3 mA nominal; with 5% on everything the worst case is higher.
    result = calculate("led_current", inputs_for("led_current", 5.0))
    assert result.value is not None and result.case is not None
    assert result.value.value == pytest.approx(1.3e-3)
    worst = (3.3 * 1.05 - 2.0 * 0.95) / (1e3 * 0.95)
    assert result.case.maximum == pytest.approx(worst)
    assert check_limit(result, 1.4e-3, "at most") == "fail"
    assert check_limit(result, worst * 1.001, "at most") == "pass"
    report = result.as_dict()
    assert report["corners"]["method"] == "monotonic corners"
    assert report["corners"]["scope"] == "initial tolerance only"
    assert [t["source"] for t in report["accuracy"]["tolerances"]] == [
        "user-stated"
    ] * 3


def test_an_input_with_no_declared_direction_is_searched() -> None:
    # The inductor for a ripple peaks where the output is half the input,
    # so the output voltage has no one-way direction.
    inputs = inputs_for("buck_inductor", 5.0)
    inputs["v_out"] = stated("v_out", 6.0, "V", 20.0)
    result = calculate("buck_inductor", inputs)
    assert result.case is not None and result.case.method == "search"
    assert result.case.at_maximum["v_out"] == pytest.approx(
        result.case.at_maximum["v_in"] / 2, rel=1e-3
    )
    assert result.accuracy.worst_case["status"] == "searched, not proven"


def test_wrong_inputs_are_refused() -> None:
    with pytest.raises(KeyError):
        calculate("perpetual_motion", {})
    with pytest.raises(ValueError, match="expected V"):
        calculate("led_current", {"v_supply": stated("Vsupply", 3.3, "A")})
    with pytest.raises(ValueError, match="also given"):
        calculate("resistor_power", {"volts": stated("V", 1, "V")})


def test_a_boost_duty_cycle_of_one_or_more_is_refused() -> None:
    def peak(duty: float, tolerance: float | None = None) -> CalcResult:
        return calculate(
            "boost_peak_switch_current",
            {
                "ripple": stated("ripple", 0.3, "A", tolerance),
                "i_out": stated("i_out", 0.5, "A", tolerance),
                "duty": stated("duty", duty, "", tolerance),
            },
        )

    assert peak(0.9).value is not None
    for duty in (1.0, 1.2):
        with pytest.raises(ValueError, match="duty must be below 1"):
            peak(duty)
    # A range that reaches 1 has no worst case, and is not divided by.
    wide = peak(0.95, 10.0)
    assert wide.case is None
    assert "duty must be below 1" in " ".join(wide.accuracy.unverified_because)


def test_conductor_spacing_follows_both_revisions_of_the_table() -> None:
    from kairo.calc.spacing import BANDS, MEANINGS_B, TABLES, conductor_spacing

    # Revision B, as its table prints it.
    assert conductor_spacing(12, "B1", "B") == 0.05
    assert conductor_spacing(48, "B2", "B") == 0.6
    assert conductor_spacing(100, "B3", "B") == 1.5
    assert conductor_spacing(101, "B3", "B") == 3.2
    assert conductor_spacing(300, "A7", "B") == 0.8
    assert conductor_spacing(250, "A7", "B") == 0.4
    assert conductor_spacing(24, "A6", "B") == 0.25
    # Revision C changed the uncoated external value and added a column.
    assert conductor_spacing(48, "B2", "C") == 0.64
    assert conductor_spacing(48, "B4", "C") == 0.3
    assert conductor_spacing(500, "A8", "C") == 3.0
    # Above 500 V: the 500 V value plus so much a volt. Siemens' worked
    # example for B1 at 600 V is 0.25 mm + 100 V * 0.0025 mm = 0.50 mm.
    assert conductor_spacing(600, "B1", "C") == pytest.approx(0.50)
    assert conductor_spacing(1000, "B2", "B") == pytest.approx(2.5 + 500 * 0.005)
    assert conductor_spacing(700, "A8", "C") == pytest.approx(3.0 + 200 * 0.0061)
    # Between two bands, the higher one.
    assert conductor_spacing(15.5, "A6", "B") == 0.25
    assert conductor_spacing(0, "B2", "B") == 0.1
    with pytest.raises(ValueError, match="no column A8"):
        conductor_spacing(10, "A8", "B")
    with pytest.raises(ValueError, match="magnitude"):
        conductor_spacing(-5, "B1", "B")
    # Every column has a value for every band and never falls with voltage.
    for revision, table in TABLES.items():
        assert len(table) == (7 if revision == "B" else 8)
        for values, per_volt in table.values():
            assert len(values) == len(BANDS) and per_volt > 0
            assert list(values) == sorted(values)
    assert set(MEANINGS_B) == set(TABLES["B"])


def test_the_spacing_calculations_take_the_larger_revision() -> None:
    def spacing(name: str, volts: float, tolerance: float | None = None) -> CalcResult:
        return calculate(name, {"voltage": stated("V", volts, "V", tolerance)})

    outer = spacing("conductor_spacing_external_uncoated", 48)
    assert outer.value is not None and outer.value.unit == "mm"
    # 0.6 mm in revision B, 0.64 mm in revision C.
    assert outer.value.value == 0.64
    assert "revision C" in outer.source and "Table 6-1" in outer.source
    inner = spacing("conductor_spacing_internal", 48)
    assert inner.value is not None and inner.value.value == 0.1
    # 160 V give or take 10% runs from 144 V to 176 V, across two of the
    # table's bands: 0.64 mm at the bottom and 1.25 mm at the top.
    wide = spacing("conductor_spacing_external_uncoated", 160, 10.0)
    assert wide.case is not None
    assert (wide.case.minimum, wide.case.maximum) == (0.64, 1.25)


@pytest.mark.parametrize(
    ("line", "plane", "values"),
    [
        ("microstrip", "height", (50.0, 1.6, 4.4)),
        ("microstrip", "height", (90.0, 0.2, 4.2)),
        ("stripline", "spacing", (50.0, 3.2, 2.2)),
        ("stripline", "spacing", (100.0, 0.4, 4.2)),
    ],
)
def test_the_width_for_an_impedance_gives_that_impedance_back(
    line: str, plane: str, values: tuple[float, float, float]
) -> None:
    impedance, thickness, er = values
    width = float(
        CATALOGUE[f"{line}_width"].formula(
            {"impedance": impedance, plane: thickness, "er": er}
        )
    )
    back = CATALOGUE[f"{line}_impedance"].formula(
        {"width": width, plane: thickness, "er": er}
    )
    assert float(back) == pytest.approx(impedance, rel=1e-9)


def test_an_impedance_no_width_can_give_is_refused() -> None:
    with pytest.raises(ValueError, match="no width gives"):
        CATALOGUE["microstrip_width"].formula(
            {"impedance": 1000.0, "height": 1.6, "er": 4.4}
        )


def test_a_stripline_pair_tends_to_two_single_lines_as_it_parts() -> None:
    pair = CATALOGUE["stripline_differential_impedance"].formula
    single = CATALOGUE["stripline_impedance"].formula
    line = {"width": 0.2, "spacing": 0.6, "er": 4.2}
    alone = 2.0 * float(single(line))
    # Coupling lowers the odd mode: a close pair is below two lines alone.
    assert float(pair({**line, "gap": 0.2})) < alone
    # Far apart, the exact solution is the single line's, twice.
    assert float(pair({**line, "gap": 5.0})) == pytest.approx(alone, rel=1e-6)
