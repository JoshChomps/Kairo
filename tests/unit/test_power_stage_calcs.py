"""Calculations for a battery's power stage, against hand-worked numbers.

Worked by hand for a 13-cell pack at 54.6 V: four FETs of 2.2 mohm sharing
100 A with the worst 15% above an even share carry 28.75 A each, so
28.75^2 * 0.0022 = 1.818 W. Precharging 2 mF through 47 ohm puts
0.002 * 54.6^2 / 2 = 2.981 J in the resistor, 54.6^2 / 47 = 63.43 W at the
start, and is done in 5 * 47 * 0.002 = 0.47 s.
"""

from __future__ import annotations

import pytest

from kairo.calc import Given, calculate
from kairo.units import Quantity


def given(name: str, value: float, unit: str) -> Given:
    return Given(name, Quantity(value, unit), tolerance=0.0, source="test data")


@pytest.mark.parametrize(
    ("name", "inputs", "expected"),
    [
        (
            "parallel_switch_loss",
            {
                "i": (100.0, "A"),
                "count": (4.0, ""),
                "r_on": (2.2e-3, "ohm"),
                "imbalance": (15.0, "%"),
            },
            28.75**2 * 0.0022,
        ),
        ("precharge_energy", {"c": (2e-3, "F"), "v": (54.6, "V")}, 2.98116),
        ("precharge_peak_power", {"v": (54.6, "V"), "r": (47.0, "ohm")}, 63.4289),
        ("precharge_time", {"r": (47.0, "ohm"), "c": (2e-3, "F")}, 0.47),
    ],
)
def test_a_power_stage_calculation_gives_the_hand_worked_figure(
    name: str, inputs: dict[str, tuple[float, str]], expected: float
) -> None:
    result = calculate(name, {k: given(k, v, u) for k, (v, u) in inputs.items()})
    assert result.value is not None
    assert result.value.value == pytest.approx(expected, rel=1e-5)
