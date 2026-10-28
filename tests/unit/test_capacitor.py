from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kairo.calc import Given, mlcc_effective_capacitance, regulator_stability
from kairo.parts import BiasCurve, PartsError, PartStore, Source
from kairo.units import Quantity

# Made up for these tests: it is not any manufacturer's data. A real curve
# comes from the manufacturer for one part number.
TEST_SOURCE = {
    "document": "test curve, not a manufacturer's",
    "revision": "none",
    "page": "none",
}
CURVE = BiasCurve(
    points=[(0.0, 1.0), (2.0, 0.8), (4.0, 0.5), (6.3, 0.3)],
    conditions="made up for a test",
    source=Source(**TEST_SOURCE),
    review="reviewed",
)


def farads(value: float, tolerance: float | None = 10.0) -> Given:
    return Given(
        "Cnom",
        Quantity(value, "F"),
        ref="C2",
        tolerance=tolerance,
        source="user-stated",
    )


def volts(value: float, tolerance: float | None = None) -> Given:
    return Given("Vdc", Quantity(value, "V"), tolerance=tolerance, source="user-stated")


def test_the_curve_is_read_between_its_points_and_never_beyond_them() -> None:
    assert CURVE.fraction_at(0) == 1.0
    assert CURVE.fraction_at(1.0) == pytest.approx(0.9)
    assert CURVE.fraction_at(3.0) == pytest.approx(0.65)
    assert CURVE.fraction_at(-3.0) == pytest.approx(0.65)
    assert CURVE.fraction_at(6.3) == pytest.approx(0.3)
    with pytest.raises(PartsError, match="beyond"):
        CURVE.fraction_at(10.0)


def test_the_minimum_takes_tolerance_bias_and_temperature_together() -> None:
    result = mlcc_effective_capacitance(
        farads(22e-6), volts(3.0, 5.0), dielectric="X5R", curve=CURVE, ref="C2"
    )
    # Bias is read at 3.15 V, the top of the rail's tolerance:
    # 0.8 - 0.3 * 1.15 / 2 = 0.6275. Then 22 uF * 0.9 * 0.6275 * 0.85.
    assert (result.status, result.bound) == ("bound", ">=")
    assert result.value is not None
    assert result.value.value == pytest.approx(22e-6 * 0.9 * 0.6275 * 0.85)
    assert result.accuracy.status == "verified"
    assert any("typical, not guaranteed" in w for w in result.warnings)
    assert "scope" in result.accuracy.worst_case
    # With no tolerance on the rail, the bias is read at nominal and the
    # result says the worst case is not complete.
    nominal_only = mlcc_effective_capacitance(
        farads(22e-6), volts(3.0), dielectric="X5R", curve=CURVE, ref="C2"
    )
    assert nominal_only.value is not None
    assert nominal_only.value.value == pytest.approx(22e-6 * 0.9 * 0.65 * 0.85)
    assert nominal_only.accuracy.status == "unverified"


def test_no_curve_is_unverified_and_names_the_part_and_what_to_supply() -> None:
    result = mlcc_effective_capacitance(
        farads(22e-6), volts(3.3), dielectric="X5R", curve=None, ref="C2"
    )
    assert (result.status, result.value) == ("unverified", None)
    (missing,) = result.missing
    assert (missing.name, missing.ref) == ("DC-bias curve", "C2")
    assert "no part number" in missing.effect and "mpn" in missing.resolve
    named = mlcc_effective_capacitance(
        farads(22e-6), volts(3.3), dielectric="X5R", curve=None, ref="C2", mpn="GRM21X"
    )
    assert "GRM21X" in named.missing[0].resolve


def test_a_class_one_dielectric_needs_no_curve() -> None:
    result = mlcc_effective_capacitance(
        farads(100e-12, 5.0), volts(50.0), dielectric="C0G", curve=None
    )
    assert result.value is not None
    assert result.value.value == pytest.approx(100e-12 * 0.95)
    assert any("class I" in w for w in result.warnings)


def test_missing_tolerance_dielectric_or_range_is_reported_not_filled_in() -> None:
    result = mlcc_effective_capacitance(
        farads(22e-6, None), volts(3.3), dielectric=None, curve=CURVE, ref="C2"
    )
    assert result.status == "unverified"
    assert [m.name for m in result.missing] == ["tolerance", "dielectric"]
    beyond = mlcc_effective_capacitance(
        farads(22e-6), volts(9.0), dielectric="X7R", curve=CURVE, ref="C2"
    )
    assert beyond.status == "unverified" and "beyond" in beyond.missing[0].effect
    odd = mlcc_effective_capacitance(
        farads(22e-6), volts(3.3), dielectric="Q9Z", curve=CURVE, ref="C2"
    )
    assert odd.status == "unverified" and "Q9Z" in odd.missing[0].effect


def region(name: str, value: float, unit: str) -> Given:
    return Given(name, Quantity(value, unit), origin="datasheet p.9, figure 12")


def test_a_capacitor_outside_the_stable_region_is_flagged() -> None:
    stable = {
        "c_min": region("Cmin", 10e-6, "F"),
        "esr_min": region("ESRmin", 0.1, "ohm"),
        "esr_max": region("ESRmax", 5.0, "ohm"),
    }
    good = Given("ESR", Quantity(0.5, "ohm"), tolerance=20.0, source="datasheet")
    assert regulator_stability(12e-6, 24e-6, good, ref="U1", **stable) == ("pass", ())
    # The seeded fault: a ceramic capacitor's few milliohms on a regulator
    # that needs at least 100.
    ceramic = Given("ESR", Quantity(0.005, "ohm"), tolerance=20.0, source="datasheet")
    verdict, reasons = regulator_stability(12e-6, 24e-6, ceramic, ref="U1", **stable)
    assert verdict == "fail"
    assert "below the 0.1 ohm" in reasons[0] and "datasheet p.9" in reasons[0]
    verdict, reasons = regulator_stability(6e-6, 24e-6, good, ref="U1", **stable)
    assert verdict == "fail" and "6e-06 F, below" in reasons[0]
    # Nothing is assumed: no region, no ESR, or no tolerance on it.
    assert (
        regulator_stability(
            12e-6, 24e-6, good, c_min=None, esr_min=None, esr_max=None, ref="U1"
        )[0]
        == "unverified"
    )
    assert (
        regulator_stability(12e-6, 24e-6, None, ref="U1", **stable)[0] == "unverified"
    )
    bare = Given("ESR", Quantity(0.5, "ohm"))
    assert (
        regulator_stability(12e-6, 24e-6, bare, ref="U1", **stable)[0] == "unverified"
    )


def part_file(folder: Path, **changes: Any) -> None:
    data: dict[str, Any] = {
        "mpn": "TEST-22U",
        "manufacturer": "Test",
        "facts": {
            "v_rating": {
                "value": "10V",
                "source": dict(TEST_SOURCE),
                "review": "reviewed",
            }
        },
        "dc_bias": {
            "points": [[0, 1.0], [5, 0.5], [10, 0.2]],
            "conditions": "made up for a test",
            "source": dict(TEST_SOURCE),
        },
    }
    data.update(changes)
    (folder / "TEST-22U.json").write_text(json.dumps(data), encoding="utf-8")


def test_a_part_is_read_with_its_sources(tmp_path: Path) -> None:
    store = PartStore(tmp_path)
    assert store.get("TEST-22U") is None
    part_file(tmp_path)
    part = store.get("TEST-22U")
    assert part is not None and part.dc_bias is not None
    assert part.dc_bias.fraction_at(2.5) == pytest.approx(0.75)
    assert part.dc_bias.review == "unreviewed"
    assert part.facts["v_rating"].source.cite().startswith("test curve")


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"facts": {"v_rating": {"value": "10V"}}}, "source"),
        (
            {"dc_bias": {"points": [[0, 1.0], [5, 0.5]], "conditions": "made up"}},
            "source",
        ),
        (
            {
                "dc_bias": {
                    "points": [[1, 1.0], [5, 0.5]],
                    "conditions": "x y z",
                    "source": TEST_SOURCE,
                }
            },
            "start at 0 V",
        ),
        (
            {
                "dc_bias": {
                    "points": [[0, 1.0], [5, 0.5], [4, 0.4]],
                    "conditions": "x y z",
                    "source": TEST_SOURCE,
                }
            },
            "rise in voltage",
        ),
        ({"mpn": "OTHER"}, "not 'TEST-22U'"),
        ({"colour": "blue"}, "colour"),
    ],
)
def test_a_value_with_no_source_or_a_bad_curve_is_refused(
    tmp_path: Path, changes: dict[str, Any], says: str
) -> None:
    part_file(tmp_path, **changes)
    with pytest.raises(PartsError, match=says):
        PartStore(tmp_path).get("TEST-22U")
