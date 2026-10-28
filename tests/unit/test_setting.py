"""A chip's programmed settings: only whole steps within its range are held.

Worked by hand with a 50.6 mV step and a range of 1.012 V to 5.566 V (20
to 110 steps): 4.25 V is step 84 (4.2504 V, the same to four figures);
4.22 V lies between step 83 (4.1998 V) and step 84.
"""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from minilib import make_index
from pydantic import ValidationError

from kairo.calc import settable
from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.verify.requirements import RequirementResult, evaluate_requirements


def test_a_value_on_a_step_is_held() -> None:
    found = settable(4.25, 0.0506, 0.0, 1.012, 5.566)
    assert found.exact and found.in_range
    assert found.code == 84


def test_a_value_between_steps_names_its_neighbours() -> None:
    found = settable(4.22, 0.0506, 0.0, 1.012, 5.566)
    assert not found.exact
    assert found.below == pytest.approx(83 * 0.0506)
    assert found.above == pytest.approx(84 * 0.0506)


def test_an_offset_moves_every_step() -> None:
    # A delay of 3.3 ms per step after a fixed 10 ms: 16.6 ms is step 2.
    found = settable(0.0166, 0.0033, 0.010, 0.010, 0.426)
    assert found.exact and found.code == 2


def test_a_value_outside_the_range_is_not_held() -> None:
    found = settable(2.0, 0.426, 0.0, 0.010, 0.426)
    assert not found.in_range


def test_a_step_must_be_above_zero() -> None:
    with pytest.raises(ValueError, match="above zero"):
        settable(1.0, 0.0, 0.0, None, 2.0)


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def chip(**setting: str) -> dict[str, Any]:
    return {
        "design": "setting",
        "nets": {
            "VCC": {"role": "power", "voltage": "3.3V"},
            "GND": {"role": "ground"},
        },
        "components": {"U1": {"lib_id": "Mon:MON"}},
        "connections": {"VCC": ["U1.BAT"], "GND": ["U1.VSS"]},
        "no_connect": ["U1.PACK", "U1.CHG", "U1.TS"],
        "requirements": [
            {
                "type": "setting",
                "component": "U1",
                "name": "COV",
                "step": "50.6mV",
                "min": "1.012V",
                "max": "5.566V",
                "source": "test data",
                **setting,
            }
        ],
    }


def judge(index: LibraryIndex, data: dict[str, Any]) -> RequirementResult:
    _, found = evaluate_requirements(ground(Design.model_validate(data), index))
    return next(r for r in found if r.type == "setting")


def test_a_design_relying_on_a_value_the_chip_holds_passes(index: LibraryIndex) -> None:
    result = judge(index, chip(value="4.25V"))
    assert result.status == "pass"
    assert "step 84" in result.summary


def test_a_design_relying_on_a_value_between_steps_fails(index: LibraryIndex) -> None:
    result = judge(index, chip(value="4.22V"))
    assert result.status == "fail"
    assert "4.1998 V and 4.2504 V" in result.summary


def test_a_design_relying_on_a_value_out_of_range_fails(index: LibraryIndex) -> None:
    result = judge(index, chip(value="6.072V"))
    assert result.status == "fail"
    assert "outside the range" in result.summary


def test_a_setting_in_two_units_is_refused() -> None:
    with pytest.raises(ValidationError, match="share one unit"):
        Design.model_validate(chip(value="1s"))
