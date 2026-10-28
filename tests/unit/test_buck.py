"""A step-down stage: its rail, its inductor's peak current, its ripple, its limit.

The numbers are those of a 12 V to 5 V stage built from the TPS54202
datasheet's values (TI SLVSD26C), worked by hand here:

- the rail: 0.596 V (0.581 to 0.611) times 1 + 100k/13.3k, each resistor
  1%, is 4.863 V to 5.298 V;
- the ripple current is greatest at 12.6 V in, 390 kHz, 12 uH and 5.298 V
  out: 5.298 * (1 - 5.298/12.6) / (390e3 * 12e-6) = 0.656 A;
- the peak current is 1.5 A (and 47 uA in the divider) plus half of
  that: 1.828 A;
- the output ripple into 37.6 uF (47 uF less 20%) and 40 mohm is
  0.656 * (0.040 + 1 / (8 * 390e3 * 37.6e-6)) = 31.8 mV.
"""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from minilib import buck, make_index

from kairo.calc import calculate
from kairo.calc.result import Given
from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.power import build_tree
from kairo.units import Quantity
from kairo.verify.requirements import RequirementResult, evaluate_requirements


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def run(index: LibraryIndex, change: Any = None) -> dict[str, RequirementResult]:
    data = buck()
    if change is not None:
        change(data)
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    return {result.type: result for result in results}


def exact(name: str, value: float, unit: str) -> Given:
    return Given(name, Quantity(value, unit), "test", None, 0.0, "test")


def test_the_formulas_give_the_datasheets_own_worked_numbers() -> None:
    # TI SLVSD26C, section 7.2.3.5: 28 V in, 5 V out, 15 uH, 500 kHz. Its
    # equation 13 gives a greatest ESR of 54.8 mohm for 30 mV, which is
    # 30 mV over a ripple current of 0.5474 A; its equation 12 gives
    # 4.56 uF for the same 30 mV.
    ripple = calculate(
        "buck_ripple_current",
        {
            "v_out": exact("Vout", 5.0, "V"),
            "v_in": exact("Vin", 28.0, "V"),
            "f_sw": exact("fsw", 500e3, "Hz"),
            "l": exact("L", 15e-6, "H"),
        },
    )
    assert ripple.value is not None
    assert ripple.value.value == pytest.approx(0.030 / 0.0548, rel=1e-3)
    by_esr = calculate(
        "buck_output_ripple",
        {
            "ripple": exact("dIL", ripple.value.value, "A"),
            "esr": exact("ESR", 0.0548, "ohm"),
            "f_sw": exact("fsw", 500e3, "Hz"),
            "c": exact("Cout", 1.0, "F"),
        },
    )
    by_capacitance = calculate(
        "buck_output_ripple",
        {
            "ripple": exact("dIL", ripple.value.value, "A"),
            "esr": exact("ESR", 1e-9, "ohm"),
            "f_sw": exact("fsw", 500e3, "Hz"),
            "c": exact("Cout", 4.56e-6, "F"),
        },
    )
    assert by_esr.value is not None and by_capacitance.value is not None
    assert by_esr.value.value == pytest.approx(0.030, rel=1e-3)
    assert by_capacitance.value.value == pytest.approx(0.030, rel=2e-3)
    # Ohm's law for two resistors in series.
    series = calculate(
        "series_resistors_current",
        {
            "v": exact("V", 5.0, "V"),
            "r1": exact("R1", 100e3, "ohm"),
            "r2": exact("R2", 13.3e3, "ohm"),
        },
    )
    assert series.value is not None
    assert series.value.value == pytest.approx(5.0 / 113.3e3)


def test_the_tree_finds_the_rail_on_the_far_side_of_the_inductor(
    index: LibraryIndex,
) -> None:
    tree = build_tree(ground(Design.model_validate(buck()), index))
    (regulator,) = tree.regulators
    assert (regulator.input_net, regulator.output_net) == ("VIN", "+5V")
    assert (regulator.inductor, regulator.switch_net) == ("L1", "SW")
    rail = tree.rails["+5V"]
    assert rail.source == "U1"
    # The inductor is the regulator's own path, not a draw; the divider
    # is, at 5 V over 113.3 kilohms.
    assert [(d.what, d.kind) for d in rail.draws] == [
        ("load: the load", "load"),
        ("R1 and R2 to ground", "divider"),
    ]
    assert rail.draws[1].current == pytest.approx(5.0 / 113.3e3)
    assert not rail.is_bound

    # Something else on the feedback net makes the divider a loaded one,
    # which the tree does not solve and says so.
    data = buck()
    data["components"]["R3"] = {"lib_id": "Device:R", "value": "1k"}
    data["nets"]["X"] = {"role": "signal"}
    data["connections"]["FB"].append("R3.1")
    data["connections"]["X"] = ["R3.2"]
    loaded = build_tree(ground(Design.model_validate(data), index))
    assert [u.ref for u in loaded.rails["+5V"].unknown] == ["R1"]


def test_a_switch_pin_typed_output_is_found_as_well(index: LibraryIndex) -> None:
    # KiCad's TPS54360 types its SW pin output, not power output; the stage
    # is still the one inductor from the switch node to a rail. A pull-up on
    # the power-good flag is no second stage.
    data = buck()
    data["components"]["U1"]["lib_id"] = "Sw:BUCK_OUT"
    data["components"]["R3"] = {"lib_id": "Device:R", "value": "100k"}
    data["nets"]["PG"] = {"role": "signal"}
    data["connections"]["PG"] = ["U1.PG", "R3.1"]
    data["connections"]["+5V"].append("R3.2")
    tree = build_tree(ground(Design.model_validate(data), index))
    (regulator,) = tree.regulators
    assert (regulator.input_net, regulator.output_net) == ("VIN", "+5V")
    assert (regulator.inductor, regulator.switch_net) == ("L1", "SW")
    assert tree.rails["+5V"].source == "U1"


def test_every_check_of_the_stage_passes_with_the_hand_worked_numbers(
    index: LibraryIndex,
) -> None:
    results = run(index)
    assert results["rail_voltage"].status == "pass"
    assert "+5V is 4.863 V to 5.298 V" in results["rail_voltage"].summary
    assert "feedback pin's own current taken as nothing" in (
        results["rail_voltage"].summary
    )
    peak = results["max_inductor_current"]
    assert peak.status == "pass"
    assert "at most 1.828 A" in peak.summary and "0.656 A of ripple" in peak.summary
    ripple = results["max_ripple"]
    assert ripple.status == "pass" and "at most 31.8 mV" in ripple.summary
    assert results["regulator_load"].status == "pass"
    limit = results["switch_current_limit"]
    assert limit.status == "pass" and "at least 2.5 A" in limit.summary
    # A switching regulator's stability is its loop's, which no capacitor
    # range settles.
    stability = results["regulator_stability"]
    assert stability.status == "unverified"
    assert "phase margin is not established" in stability.missing[0]


def test_each_check_fails_when_its_limit_is_broken(index: LibraryIndex) -> None:
    def set_param(ref: str, name: str, value: str) -> Any:
        def change(data: dict[str, Any]) -> None:
            data["components"][ref]["params"][name] = value

        return change

    def set_requirement(position: int, name: str, value: str) -> Any:
        def change(data: dict[str, Any]) -> None:
            data["requirements"][position][name] = value

        return change

    # The inductor saturates below the 1.828 A it must carry.
    saturating = run(index, set_param("L1", "i_sat", "1.8A"))["max_inductor_current"]
    assert saturating.status == "fail" and "it saturates at 1.8 A" in saturating.summary
    # 31.8 mV of ripple against 30 mV allowed.
    rippling = run(index, set_requirement(2, "value", "30mV"))["max_ripple"]
    assert rippling.status == "fail" and "30 mV is allowed" in rippling.summary
    # The datasheet's own divider reaches 5.298 V, which is outside 5 V
    # within 5%: a spec that asks for that is refused.
    off = run(index, set_requirement(0, "tolerance", "5%"))["rail_voltage"]
    assert off.status == "fail" and "allowed is 4.75 V to 5.25 V" in off.summary
    # A current limit that may be as low as 1.8 A cannot carry 1.828 A.
    limited = run(index, set_param("U1", "i_limit", "1.8A"))["switch_current_limit"]
    assert limited.status == "fail"
    # More load than the regulator is rated for.
    overloaded = run(index, lambda data: data["loads"][0].update(current="2.5A"))[
        "regulator_load"
    ]
    assert overloaded.status == "fail"


def test_what_is_not_stated_leaves_a_check_unverified_and_is_named(
    index: LibraryIndex,
) -> None:
    def without(ref: str, name: str) -> Any:
        def change(data: dict[str, Any]) -> None:
            del data["components"][ref]["params"][name]

        return change

    # The plan's acceptance for simulated limits, met here by calculation:
    # a ripple requirement on a capacitor with no ESR names the ESR.
    no_esr = run(index, without("C3", "esr_max"))["max_ripple"]
    assert no_esr.status == "unverified"
    assert no_esr.missing == ("C3: no greatest ESR",)
    assert "esr_max" in no_esr.to_resolve[0]

    no_rating = run(index, without("L1", "i_sat"))["max_inductor_current"]
    assert no_rating.status == "unverified" and "i_sat" in no_rating.to_resolve[0]

    loose = run(index, without("L1", "tolerance"))
    assert loose["max_inductor_current"].status == "unverified"
    assert loose["max_ripple"].status == "unverified"
    assert any("L1" in line for line in loose["max_ripple"].missing)

    # A typical switching frequency is no bound: the ripple is greatest
    # at the least.
    def typical(data: dict[str, Any]) -> None:
        data["components"]["U1"]["limits"] = {"i_limit": "min"}

    unmarked = run(index, typical)
    for name in ("max_inductor_current", "max_ripple", "switch_current_limit"):
        assert unmarked[name].status == "unverified", name
        assert any("not marked as a minimum" in line for line in unmarked[name].missing)

    def unmarked_limit(data: dict[str, Any]) -> None:
        data["components"]["U1"]["limits"] = {"f_sw": "min"}

    assert run(index, unmarked_limit)["switch_current_limit"].status == "unverified"

    # A ceramic output capacitor with no DC-bias curve: its capacitance
    # in use is not known, so neither is the ripple.
    def ceramic(data: dict[str, Any]) -> None:
        data["components"]["C3"]["params"]["dielectric"] = "X7R"

    assert run(index, ceramic)["max_ripple"].status == "unverified"

    # Two output capacitors: the formula is for one.
    def second(data: dict[str, Any]) -> None:
        data["components"]["C4"] = dict(data["components"]["C3"])
        data["connections"]["+5V"].append("C4.1")
        data["connections"]["GND"].append("C4.2")

    doubled = run(index, second)["max_ripple"]
    assert doubled.status == "unverified" and "it has 2" in doubled.summary

    # An inductor that is in no stage, and a net no stage makes.
    def elsewhere(data: dict[str, Any]) -> None:
        data["requirements"][1]["component"] = "R1"
        data["requirements"][2]["net"] = "VIN"

    wrong = run(index, elsewhere)
    assert wrong["max_inductor_current"].status == "unverified"
    assert wrong["max_ripple"].status == "unverified"


def budget(index: LibraryIndex, limit: str, **u1: Any) -> RequirementResult:
    def change(data: dict[str, Any]) -> None:
        data["components"]["U1"]["params"].update(u1.get("params", {}))
        data["components"]["U1"]["limits"].update(u1.get("limits", {}))
        data["requirements"] = [
            {"type": "max_rail_current", "net": "VIN", "value": limit}
        ]

    return run(index, change)["max_rail_current"]


def test_a_supply_budget_follows_the_current_through_the_converter(
    index: LibraryIndex,
) -> None:
    # The input draws most with the output at its highest (5.298 V, the
    # reference and divider at their ends), carrying the load and the
    # divider at their most, from the input at its lowest (11.4 V) with
    # the efficiency at its least: Iin = Vout * Iout / (efficiency * Vin).
    vout_high = 0.596 * 1.0252 * (1 + 101e3 / (13.3e3 * 0.99))
    nominal = 0.596 * (1 + 100e3 / 13.3e3)
    vout_low = 0.596 * 0.9748 * (1 + 99e3 / (13.3e3 * 1.01))
    # The rail's range is given as the wider of its two sides about nominal.
    top = nominal * (1 + max(vout_high - nominal, nominal - vout_low) / nominal)
    i_out = 1.5 + top / (99e3 + 13.3e3 * 0.99)
    most = top * i_out / (0.85 * 11.4)
    assert most == pytest.approx(0.8202, abs=1e-4)

    # A typical efficiency is not a bound, and none is not either.
    unstated = budget(index, "1A")
    assert unstated.status == "unverified"
    assert any("efficiency is not given" in line for line in unstated.missing)
    typical = budget(index, "1A", params={"efficiency": "85%"})
    assert typical.status == "unverified"
    assert any("not marked as a least" in line for line in typical.missing)

    least = {"params": {"efficiency": "85%"}, "limits": {"efficiency": "min"}}
    held = budget(index, "1A", **least)
    assert held.status == "pass", held.summary
    assert f"at most {most * 1000:.4g} mA" in held.summary
    # Over the budget only at the worst case: still a failure.
    assert budget(index, "810mA", **least).status == "fail"
    # Over it at nominal, where the rail is its stated 5 V:
    # 5 V * 1.5 A / (0.85 * 12 V) = 735.3 mA.
    over = budget(index, "700mA", **least)
    assert over.status == "fail" and "735.3 mA at nominal" in over.summary
