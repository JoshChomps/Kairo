"""How long a rail's capacitors hold it above a floor once its supply is gone.

Worked by hand: two 100 uF polymer capacitors of 20% give 160 uF at
their least; a 5 V supply of 5% starts at 4.75 V at its lowest; with a
100 mA load the rail takes 160 uF * (4.75 V - 4 V) / 0.1 A = 1.2 ms to
reach 4 V. With every part at its marked value it would take 2 ms.
"""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from minilib import buck, make_index

from kairo.calc import calculate
from kairo.calc.result import Given
from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.units import Quantity
from kairo.verify.requirements import RequirementResult, evaluate_requirements


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def reservoir(time: str = "1ms") -> dict[str, Any]:
    polymer = {"tolerance": "20%", "dielectric": "polymer"}
    return {
        "design": "holdup",
        "policy": {"margins": "stated"},
        "nets": {
            "VBUS": {"role": "power", "voltage": "5V", "tolerance": "5%"},
            "GND": {"role": "ground"},
        },
        "components": {
            "C1": {"lib_id": "Device:C", "value": "100uF", "params": dict(polymer)},
            "C2": {"lib_id": "Device:C", "value": "100uF", "params": dict(polymer)},
        },
        "connections": {"VBUS": ["C1.1", "C2.1"], "GND": ["C1.2", "C2.2"]},
        "loads": [{"net": "VBUS", "current": "100mA", "note": "the logic"}],
        "requirements": [
            {"type": "min_holdup_time", "net": "VBUS", "floor": "4V", "time": time}
        ],
    }


def judge(
    index: LibraryIndex, data: dict[str, Any], change: Callable[..., None] | None = None
) -> RequirementResult:
    if change is not None:
        change(data)
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    return next(r for r in results if r.type == "min_holdup_time")


def test_the_formula_is_the_capacitor_law_at_a_constant_current() -> None:
    def exact(name: str, value: float, unit: str) -> Given:
        return Given(name, Quantity(value, unit), "test", None, 0.0, "test")

    result = calculate(
        "holdup_time",
        {
            "c": exact("C", 160e-6, "F"),
            "v_start": exact("Vstart", 4.75, "V"),
            "v_floor": exact("Vfloor", 4.0, "V"),
            "i": exact("I", 0.1, "A"),
        },
    )
    assert result.value is not None
    assert result.value.value == pytest.approx(1.2e-3)
    assert result.value.unit == "s"


def test_the_time_is_the_least_over_the_tolerances(index: LibraryIndex) -> None:
    held = judge(index, reservoir("1ms"))
    assert held.status == "pass"
    assert "C1, C2 (160 uF at their least)" in held.summary
    assert "for at least 1.2 ms at 100 mA" in held.summary
    assert "ESR is not counted" in held.summary
    # 1.5 ms is met at the marked values (2 ms) and not at the worst.
    assert judge(index, reservoir("1.5ms")).status == "fail"


def test_a_rail_that_cannot_hold_fails_with_the_reason(index: LibraryIndex) -> None:
    def no_capacitors(data: dict[str, Any]) -> None:
        data["nets"]["X"] = {"role": "signal"}
        data["connections"] = {"X": ["C1.1", "C2.1"], "GND": ["C1.2", "C2.2"]}

    bare = judge(index, reservoir(), no_capacitors)
    assert bare.status == "fail" and "no capacitor joins VBUS" in bare.summary

    def high_floor(data: dict[str, Any]) -> None:
        data["requirements"][0]["floor"] = "4.8V"

    low = judge(index, reservoir(), high_floor)
    assert low.status == "fail"
    assert "as low as 4.75 V before its supply goes" in low.summary


def test_what_is_not_known_leaves_it_unverified_and_is_named(
    index: LibraryIndex,
) -> None:
    def ceramic(data: dict[str, Any]) -> None:
        data["components"]["C1"]["params"]["dielectric"] = "X5R"

    # A class II ceramic with no DC-bias curve: its capacitance at 5 V is
    # not known, and is not guessed.
    biased = judge(index, reservoir(), ceramic)
    assert biased.status == "unverified"
    assert any("C1" in line for line in biased.missing)

    def no_load(data: dict[str, Any]) -> None:
        data["loads"] = []

    idle = judge(index, reservoir(), no_load)
    assert idle.status == "unverified"
    assert idle.missing == ("VBUS: nothing is stated to draw from it",)

    def no_tolerance(data: dict[str, Any]) -> None:
        del data["nets"]["VBUS"]["tolerance"]

    loose = judge(index, reservoir(), no_tolerance)
    assert loose.status == "unverified"

    def not_a_rail(data: dict[str, Any]) -> None:
        data["nets"]["VBUS"] = {"role": "signal"}
        data["loads"] = []

    assert judge(index, reservoir(), not_a_rail).status == "unverified"


def test_a_default_margin_stands_in_for_a_tolerance_and_is_named(
    index: LibraryIndex,
) -> None:
    def defaults(data: dict[str, Any]) -> None:
        del data["policy"]
        del data["nets"]["VBUS"]["tolerance"]

    # The supply's default is 10%: 160 uF * (4.5 V - 4 V) / 0.1 A = 0.8 ms.
    held = judge(index, reservoir("0.5ms"), defaults)
    assert held.status == "pass" and "at least 0.8 ms" in held.summary
    assert held.on_defaults == ("VBUS's tolerance: 10%",)


def test_a_switching_regulator_is_taken_at_the_floor_where_it_draws_most(
    index: LibraryIndex,
) -> None:
    # A converter draws more as its input falls, so over the hold-up it
    # draws most at the floor: 5.298 V (its output at the highest) times
    # 1.5 A and the divider's 47 uA, over 85% times 8 V, is 1.169 A. The
    # 10 uF C0G input capacitor, 10% low, holds 11.4 V (12 V less 5%)
    # above 8 V for 9 uF * 3.4 V / 1.169 A = 26.2 us.
    def with_input(time: str, **u1: Any) -> RequirementResult:
        data = buck()
        data["components"]["C1"]["params"] = {"tolerance": "10%", "dielectric": "C0G"}
        data["components"]["U1"]["params"].update(u1)
        if u1:
            data["components"]["U1"]["limits"]["efficiency"] = "min"
        data["requirements"] = [
            {"type": "min_holdup_time", "net": "VIN", "floor": "8V", "time": time}
        ]
        return judge(index, data)

    top = (
        0.596
        * (1 + 100e3 / 13.3e3)
        * (
            1
            + (
                0.596 * 1.0252 * (1 + 101e3 / (13.3e3 * 0.99))
                - 0.596 * (1 + 100e3 / 13.3e3)
            )
            / (0.596 * (1 + 100e3 / 13.3e3))
        )
    )
    current = top * (1.5 + top / (99e3 + 13.3e3 * 0.99)) / (0.85 * 8)
    held = 9e-6 * 3.4 / current
    assert held == pytest.approx(26.2e-6, abs=0.1e-6)

    unstated = with_input("20us")
    assert unstated.status == "unverified"
    assert any("efficiency" in line for line in unstated.missing)
    enough = with_input("20us", efficiency="85%")
    assert enough.status == "pass", enough.summary
    assert with_input("30us", efficiency="85%").status == "fail"


def test_a_makers_curve_decides_what_marked_values_would_pass(
    index: LibraryIndex,
) -> None:
    # Murata's measured curve for GRM21BR61A226ME44, kept with the
    # reference designs: 44% of its 0 V value at 5.25 V. Four of them
    # hold 50 mA from 4.75 V to 4 V for 26.38 uF * 0.75 V / 50 mA at least.
    from kairo.parts.store import PartStore

    parts = PartStore(Path(__file__).resolve().parents[2] / "evals" / "parts")
    mlcc = {
        "mpn": "GRM21BR61A226ME44",
        "tolerance": "20%",
        "dielectric": "X5R",
    }
    data = reservoir("300us")
    data["components"] = {
        f"C{n}": {"lib_id": "Device:C", "value": "22uF", "params": dict(mlcc)}
        for n in range(1, 5)
    }
    data["connections"] = {
        "VBUS": [f"C{n}.1" for n in range(1, 5)],
        "GND": [f"C{n}.2" for n in range(1, 5)],
    }
    data["loads"] = [{"net": "VBUS", "current": "50mA", "note": "the logic"}]

    def holds(time: str, store: PartStore | None) -> RequirementResult:
        data["requirements"][0]["time"] = time
        _, results = evaluate_requirements(
            ground(Design.model_validate(data), index), store
        )
        return results[0]

    passed = holds("300us", parts)
    assert passed.status == "pass"
    assert "26.38 uF at their least" in passed.summary
    assert "0.3957 ms" in passed.summary
    # Marked values would give 1.32 ms; the curve says otherwise.
    assert holds("500us", parts).status == "fail"
    # Without the curve nothing about an X5R part under bias is known.
    assert holds("300us", None).status == "unverified"
