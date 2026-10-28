from __future__ import annotations

import pytest

from kairo.calc import Given, ldo_thermal
from kairo.units import Quantity


def given(name: str, value: float, unit: str, **more: object) -> Given:
    return Given(name, Quantity(value, unit), **more)  # pyright: ignore[reportArgumentType]


VIN = given("Vin", 5.0, "V", origin="net VIN_5V")
VOUT = given("Vout", 3.3, "V", origin="net +3V3")
IOUT = given("Iout", 0.3013, "A", origin="power tree")
THETA = given("theta_JA", 90.0, "degC/W", ref="U1")
AMBIENT = given("TA", 40.0, "degC")


def test_the_plans_example_gives_lower_bounds_while_iq_is_missing() -> None:
    results = ldo_thermal(VIN, VOUT, IOUT, theta_ja=THETA, t_ambient=AMBIENT, ref="U1")
    power = results["ldo_dissipation"]
    # (5 - 3.3) V * 301.3 mA = 0.51221 W; the plan says "at least 0.51 W".
    assert (power.status, power.bound) == ("bound", ">=")
    assert power.value is not None and power.value.value == pytest.approx(0.51221)
    assert power.text() == ">= 0.5122 W"
    (missing,) = power.missing
    assert (missing.name, missing.ref) == ("Iq", "U1")
    assert "not negative" in missing.effect

    junction = results["ldo_junction_temperature"]
    # 40 + 0.51221 * 90 = 86.1 degC; the plan says "at least about 86".
    assert (junction.status, junction.bound) == ("bound", ">=")
    assert junction.value is not None
    assert junction.value.value == pytest.approx(86.0989)
    assert any("JEDEC test board" in warning for warning in junction.warnings)

    efficiency = results["ldo_efficiency"]
    assert (efficiency.status, efficiency.bound) == ("bound", "<=")
    assert efficiency.value is not None and efficiency.value.value == pytest.approx(
        66.0
    )


def test_with_iq_given_the_values_are_exact() -> None:
    iq = given("Iq", 0.005, "A", ref="U1")
    results = ldo_thermal(VIN, VOUT, IOUT, theta_ja=THETA, t_ambient=AMBIENT, i_q=iq)
    power = results["ldo_dissipation"]
    # 0.51221 + 5 V * 5 mA = 0.53721 W.
    assert (power.status, power.bound, power.missing) == ("ok", None, ())
    assert power.value is not None and power.value.value == pytest.approx(0.53721)
    junction = results["ldo_junction_temperature"]
    assert junction.value is not None
    assert junction.value.value == pytest.approx(40 + 0.53721 * 90)
    efficiency = results["ldo_efficiency"]
    assert efficiency.value is not None
    assert efficiency.value.value == pytest.approx(100 * 3.3 * 0.3013 / (5 * 0.3063))


def test_a_second_worked_example() -> None:
    # 12 V to 5 V at 100 mA with 6 mA quiescent, 50 degC/W, 25 degC ambient:
    # P = 7 * 0.1 + 12 * 0.006 = 0.772 W; TJ = 25 + 0.772 * 50 = 63.6 degC.
    results = ldo_thermal(
        given("Vin", 12.0, "V"),
        given("Vout", 5.0, "V"),
        given("Iout", 0.1, "A"),
        theta_ja=given("theta_JA", 50.0, "degC/W"),
        t_ambient=given("TA", 25.0, "degC"),
        i_q=given("Iq", 0.006, "A"),
        v_dropout=given("Vdropout", 2.0, "V"),
    )
    values = {name: r.value.value for name, r in results.items() if r.value}
    assert values["ldo_dissipation"] == pytest.approx(0.772)
    assert values["ldo_junction_temperature"] == pytest.approx(63.6)
    assert values["ldo_dropout_headroom"] == pytest.approx(5.0)


def test_what_cannot_be_computed_is_unverified_and_says_what_to_give() -> None:
    results = ldo_thermal(VIN, VOUT, IOUT, ref="U1")
    junction = results["ldo_junction_temperature"]
    assert (junction.status, junction.value, junction.text()) == (
        "unverified",
        None,
        "unverified",
    )
    assert [m.name for m in junction.missing] == ["Iq", "theta_JA", "TA"]
    headroom = results["ldo_dropout_headroom"]
    assert headroom.status == "unverified"
    assert "v_dropout" in headroom.missing[0].resolve


def test_every_result_carries_an_accuracy_block_that_is_never_a_pass_yet() -> None:
    stated = given("Iout", 0.3013, "A", tolerance=5.0, source="user-stated")
    results = ldo_thermal(
        VIN, VOUT, stated, theta_ja=THETA, t_ambient=AMBIENT, ref="U1"
    )
    for result in results.values():
        block = result.as_dict()["accuracy"]
        assert block["status"] == "unverified"
        assert (
            block["unverified_because"]
            and block["worst_case"]["status"] == "unverified"
        )
    block = results["ldo_dissipation"].as_dict()["accuracy"]
    by_name = {t["parameter"]: t for t in block["tolerances"]}
    assert by_name["Iout"] == {
        "ref": None,
        "parameter": "Iout",
        "tolerance": "5%",
        "source": "user-stated",
    }
    assert by_name["Vin"]["source"] == "missing"
    assert any("U1: no Iq" in line for line in block["unverified_because"])
    assert any("quiescent current" in line for line in block["to_resolve"])
    # Status and what to resolve come first, so a cut-off reply keeps them.
    assert list(results["ldo_dissipation"].as_dict())[:3] == ["name", "status", "value"]
    assert list(block)[:2] == ["status", "to_resolve"]


def test_values_no_regulator_can_have_are_refused() -> None:
    with pytest.raises(ValueError, match="above the input"):
        ldo_thermal(VOUT, VIN, IOUT)
    with pytest.raises(ValueError, match="negative"):
        ldo_thermal(VIN, VOUT, given("Iout", -0.1, "A"))
    with pytest.raises(ValueError, match="expected A"):
        ldo_thermal(VIN, VOUT, given("Iout", 0.3, "V"))
