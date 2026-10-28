"""A current a chip sets from one resistor to ground, I = K / R.

Worked by hand: K = 1000 V (A*ohm) within 10% and R = 2 kohm within 1%
give 500 mA at nominal, at least 900 / 2020 = 445.5 mA and at most
1100 / 1980 = 555.6 mA.
"""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from minilib import make_index
from pydantic import ValidationError

from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.verify.requirements import RequirementResult, evaluate_requirements


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def charger(low: str | None = "400mA", high: str | None = "600mA") -> dict[str, Any]:
    requirement: dict[str, Any] = {
        "type": "programmed_current",
        "component": "U1",
        "pin": "ISET",
        "factor": "k_charge",
    }
    if low is not None:
        requirement["min"] = low
    if high is not None:
        requirement["max"] = high
    return {
        "design": "charger",
        "policy": {"margins": "stated"},
        "nets": {
            "VIN": {"role": "power", "voltage": "5V", "tolerance": "5%"},
            "BAT": {"role": "power", "voltage": "3.7V", "tolerance": "15%"},
            "ISET": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        "components": {
            "U1": {
                "lib_id": "Reg:CHG",
                "params": {
                    "k_charge": "1000V",
                    "k_charge_tolerance": "10%",
                    "source": "test data",
                },
            },
            "R1": {
                "lib_id": "Device:R",
                "value": "2k",
                "params": {"tolerance": "1%"},
            },
        },
        "connections": {
            "VIN": ["U1.IN"],
            "BAT": ["U1.BAT"],
            "ISET": ["U1.ISET", "R1.1"],
            "GND": ["U1.GND", "R1.2"],
        },
        "no_connect": ["U1.ILIM"],
        "requirements": [requirement],
    }


def judge(
    index: LibraryIndex, data: dict[str, Any], change: Callable[..., None] | None = None
) -> RequirementResult:
    if change is not None:
        change(data)
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    return next(r for r in results if r.type == "programmed_current")


def test_the_current_over_the_tolerances_is_k_over_r_at_its_corners(
    index: LibraryIndex,
) -> None:
    result = judge(index, charger())
    assert result.status == "pass"
    calc = result.results[0]
    assert calc.value is not None and calc.case is not None
    assert calc.value.value == pytest.approx(0.5)
    assert calc.case.minimum == pytest.approx(900 / 2020)
    assert calc.case.maximum == pytest.approx(1100 / 1980)
    assert "445.5 mA to 555.6 mA" in result.summary


def test_a_bound_the_worst_case_crosses_fails(index: LibraryIndex) -> None:
    # 550 mA is above the nominal 500 mA but below the most it can be.
    assert judge(index, charger(None, "550mA")).status == "fail"
    assert judge(index, charger("450mA", None)).status == "fail"
    assert judge(index, charger("445mA", "556mA")).status == "pass"


def test_without_the_datasheets_factor_or_its_tolerance_nothing_is_passed(
    index: LibraryIndex,
) -> None:
    def no_factor(data: dict[str, Any]) -> None:
        del data["components"]["U1"]["params"]["k_charge"]

    result = judge(index, charger(), no_factor)
    assert result.status == "unverified"
    assert result.missing == ("U1: no k_charge, the datasheet's factor for ISET",)

    # Kairo has no default spread for a chip's factor, with or without its
    # default margins: the datasheet's minimum and maximum are needed.
    def no_spread(data: dict[str, Any]) -> None:
        del data["components"]["U1"]["params"]["k_charge_tolerance"]
        data["policy"] = {"margins": "default"}

    result = judge(index, charger(), no_spread)
    assert result.status == "unverified"
    assert any("U1" in line for line in result.missing)


def test_a_second_path_from_the_pin_leaves_it_unverified(index: LibraryIndex) -> None:
    # A capacitor beside the resistor carries none of the pin's direct
    # current, but anything else on the net is outside the formula, so
    # the check refuses rather than guessing which parts matter.
    def with_capacitor(data: dict[str, Any]) -> None:
        data["components"]["C1"] = {"lib_id": "Device:C", "value": "1nF"}
        data["connections"]["ISET"].append("C1.1")
        data["connections"]["GND"].append("C1.2")

    result = judge(index, charger(), with_capacitor)
    assert result.status == "unverified"
    assert result.missing == ("ISET: C1, R1 besides U1.ISET",)

    # A resistor to a rail, not to ground, is not the datasheet's circuit.
    def to_a_rail(data: dict[str, Any]) -> None:
        data["connections"]["GND"].remove("R1.2")
        data["connections"]["VIN"].append("R1.2")

    assert judge(index, charger(), to_a_rail).status == "unverified"


def test_a_requirement_with_no_bound_is_refused() -> None:
    data = charger(None, None)
    with pytest.raises(ValidationError, match="min, its max, or both"):
        Design.model_validate(data)
