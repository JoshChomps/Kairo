# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from minilib import ldo, make_index

from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.parts import PartStore
from kairo.power import build_tree
from kairo.verify.requirements import RequirementResult, evaluate_requirements

REQUIREMENTS = [
    {"type": "min_capacitance", "between": ["+3V3", "GND"], "value": "10uF"},
    {"type": "rail_voltage", "net": "+3V3", "nominal": "3.3V", "tolerance": "3%"},
    {
        "type": "max_junction_temp",
        "component": "U1",
        "ambient": "40degC",
        "limit": "125degC",
    },
    {"type": "max_component_power", "component": "R1", "derating": "50%"},
]
# Made up for these tests; not any manufacturer's data.
SOURCE = {
    "document": "test curve, not a manufacturer's",
    "revision": "none",
    "page": "none",
}


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def example() -> dict[str, Any]:
    data = ldo()
    data["requirements"] = [dict(r) for r in REQUIREMENTS]
    data["components"]["C1"]["params"] = {"tolerance": "10%", "dielectric": "X5R"}
    data["components"]["C2"]["params"] = {"tolerance": "10%", "dielectric": "X5R"}
    return data


def run(
    index: LibraryIndex, change: Any = None, store: PartStore | None = None
) -> dict[str, RequirementResult]:
    data = example()
    if change is not None:
        change(data)
    _, results = evaluate_requirements(
        ground(Design.model_validate(data), index), store
    )
    return {result.type: result for result in results}


def curve_store(folder: Path, points: list[list[float]]) -> PartStore:
    parts = folder / "parts"
    parts.mkdir(exist_ok=True)
    (parts / "TEST-22U.json").write_text(
        json.dumps(
            {
                "mpn": "TEST-22U",
                "manufacturer": "Test",
                "dc_bias": {
                    "points": points,
                    "conditions": "made up for a test",
                    "source": SOURCE,
                    "review": "reviewed",
                },
            }
        ),
        encoding="utf-8",
    )
    return PartStore(parts)


def test_the_example_rail_carries_about_301_milliamps(index: LibraryIndex) -> None:
    tree = build_tree(ground(Design.model_validate(example()), index))
    rail = tree.rails["+3V3"]
    assert rail.current == pytest.approx(0.3013)
    assert [(d.what, round(d.current, 6)) for d in rail.draws] == [
        ("load: MCU budget", 0.3),
        ("R1 and D1", 0.0013),
    ]
    assert rail.source == "U1" and not rail.is_bound
    feed = tree.rails["VIN_5V"]
    # The regulator's input is at least its output; its own current is unknown.
    assert feed.current == pytest.approx(0.3013) and feed.is_bound
    # The small library's connector is in no connector library, so what
    # it carries is not taken to be nothing.
    assert [u.ref for u in feed.unknown] == ["J1", "U1"]
    assert tree.regulators[0].kind == "linear"


def test_the_junction_is_a_lower_bound_and_the_requirement_unverified(
    index: LibraryIndex,
) -> None:
    result = run(index)["max_junction_temp"]
    assert result.status == "unverified"
    # 40 + (5 - 3.3) * 0.3013 * 90 = 86.1 degC, the plan's "at least about 86".
    assert "at least 86.1 degC" in result.summary
    assert "lower bound cannot show" in result.summary
    assert result.missing[0].startswith("U1: no Iq")
    assert "quiescent current (i_q)" in result.to_resolve[0]
    (calculation,) = result.results
    assert calculation.value is not None
    assert calculation.value.value == pytest.approx(86.0989)
    finding = result.finding()
    assert finding is not None
    assert (finding.layer, finding.severity, finding.code) == (
        "L5",
        "warning",
        "W_REQUIREMENT_UNVERIFIED",
    )
    assert finding.where.ref == "U1"


def test_capacitance_is_unverified_until_a_curve_is_supplied(
    index: LibraryIndex, tmp_path: Path
) -> None:
    result = run(index)["min_capacitance"]
    assert result.status == "unverified"
    assert any(line.startswith("C2: no DC-bias curve") for line in result.missing)
    assert any("part number (mpn)" in line for line in result.to_resolve)

    def with_part(d: dict[str, Any]) -> None:
        d["components"]["C2"]["params"]["mpn"] = "TEST-22U"
        d["components"]["U1"]["params"].update(v_out="3.3V", v_out_tolerance="2%")

    # A part number alone is not a curve.
    assert run(index, with_part)["min_capacitance"].status == "unverified"
    # 22 uF * 0.9 * 0.8 * 0.85 = 13.5 uF at the top of the rail: enough.
    good = curve_store(tmp_path, [[0, 1.0], [3.4, 0.8], [6.3, 0.5]])
    passed = run(index, with_part, good)["min_capacitance"]
    assert passed.status == "pass"
    assert "13.5 uF effective" in passed.summary and "typical" in passed.summary
    # The seeded DC-bias fault: the same part keeps only 40.6% at 3.366 V,
    # so 22 uF * 0.9 * 0.406 * 0.85 = 6.83 uF.
    poor = curve_store(tmp_path, [[0, 1.0], [3.4, 0.4], [6.3, 0.2]])
    failed = run(index, with_part, poor)["min_capacitance"]
    assert failed.status == "fail" and "6.83 uF effective" in failed.summary


def test_a_nominal_basis_is_counted_as_marked_and_says_so(index: LibraryIndex) -> None:
    result = run(index, lambda d: d["requirements"][0].update(basis="nominal"))
    assert result["min_capacitance"].status == "pass"
    assert (
        "22 uF nominal (marked values, as asked)" in result["min_capacitance"].summary
    )


def all_given(d: dict[str, Any]) -> None:
    d["components"]["U1"]["params"].update(v_out="3.3V", v_out_tolerance="2%")
    d["components"]["D1"]["params"]["v_f_tolerance"] = "10%"


def test_removing_a_tolerance_turns_a_pass_into_unverified_never_a_pass(
    index: LibraryIndex,
) -> None:
    # (3.3 - 2.0)^2 / 1k = 1.69 mW against 62.5 mW: far inside, and with
    # every tolerance given it is proven.
    proven = run(index, all_given)["max_component_power"]
    assert proven.status == "pass", proven.missing
    assert "1.69 mW" in proven.summary

    def no_resistor_tolerance(d: dict[str, Any]) -> None:
        all_given(d)
        del d["components"]["R1"]["params"]["tolerance"]

    result = run(index, no_resistor_tolerance)["max_component_power"]
    assert result.status == "unverified"
    assert result.missing == ("R1: no tolerance on R",)
    assert "R1" in result.to_resolve[0]


def test_the_rail_voltage_needs_the_regulators_tolerance(index: LibraryIndex) -> None:
    result = run(index)["rail_voltage"]
    assert result.status == "unverified"
    assert result.missing == ("U1: no Vout", "U1: no tolerance on Vout")
    assert run(index, all_given)["rail_voltage"].status == "pass"
    wide = run(
        index,
        lambda d: d["components"]["U1"]["params"].update(
            v_out="3.3V", v_out_tolerance="4%"
        ),
    )["rail_voltage"]
    assert wide.status == "fail" and "3.168 V to 3.432 V" in wide.summary


def test_seeded_overloads_fail(index: LibraryIndex) -> None:
    # 2 A asked of a 1 A regulator.
    overloaded = run(index, lambda d: d["loads"][0].update(current="2A"))
    assert overloaded["regulator_load"].status == "fail"
    assert "2001 mA" in overloaded["regulator_load"].summary
    # Even as a lower bound the junction is far past its limit, so it is a
    # failure and not merely unverified: the missing term can only add.
    assert overloaded["max_junction_temp"].status == "fail"
    assert "at least" in overloaded["max_junction_temp"].summary
    assert run(index)["regulator_load"].status == "pass"
    finding = overloaded["regulator_load"].finding()
    assert finding is not None and finding.code == "E_REQUIREMENT_FAILED"


def test_a_seeded_corner_fault_fails_though_nominal_passes(index: LibraryIndex) -> None:
    # 1.69 mW nominal against a 1.75 mW limit passes at nominal; with the
    # rail 2% high, the LED 10% low and the resistor 1% low it is
    # (3.366 - 1.8)^2 / 990 = 2.48 mW.
    def tight(d: dict[str, Any]) -> None:
        all_given(d)
        d["components"]["R1"]["params"]["p_rating"] = "3.5mW"

    result = run(index, tight)["max_component_power"]
    assert result.status == "fail"
    assert "1.69 mW at nominal" in result.summary
    assert "worst corner" in result.summary and "2.48 mW" in result.summary


def test_results_put_status_and_what_to_resolve_first(index: LibraryIndex) -> None:
    report = run(index)["max_junction_temp"].as_dict()
    assert list(report)[:4] == ["requirement", "type", "status", "to_resolve"]
    assert report["requirement"] == 2
    assert run(index, all_given)["rail_voltage"].finding() is None


def test_a_resistor_the_tree_cannot_solve_is_reported_not_left_out(
    index: LibraryIndex,
) -> None:
    # A divider from the 3.3 V rail with a part on its middle net: R2 to
    # a signal net, R3 from there to ground, and a third resistor leading
    # off. Its current is real, and the rail's total must not look
    # complete without it.
    data = example()
    data["nets"]["MID"] = {"role": "signal"}
    data["nets"]["OFF"] = {"role": "signal"}
    data["components"]["R2"] = {"lib_id": "Device:R", "value": "10k"}
    data["components"]["R3"] = {"lib_id": "Device:R", "value": "10k"}
    data["components"]["R5"] = {"lib_id": "Device:R", "value": "10k"}
    data["connections"]["+3V3"].append("R2.1")
    data["connections"]["MID"] = ["R2.2", "R3.1", "R5.1"]
    data["connections"]["OFF"] = ["R5.2"]
    data["connections"]["GND"].append("R3.2")
    # And a resistor between the two rails.
    data["components"]["R4"] = {"lib_id": "Device:R", "value": "100k"}
    data["connections"]["VIN_5V"].append("R4.1")
    data["connections"]["+3V3"].append("R4.2")
    tree = build_tree(ground(Design.model_validate(data), index))
    rail = tree.rails["+3V3"]
    assert rail.is_bound
    unknown = {u.ref: u for u in rail.unknown}
    assert sorted(unknown) == ["R2", "R4"]
    assert "joins +3V3 to MID" in unknown["R2"].why
    assert "simulate" in unknown["R2"].resolve
    # What is known is still counted, and the LED branch is still solved.
    assert rail.current == pytest.approx(0.3013)
    # The input rail inherits what its regulator's output does not know.
    assert {u.ref for u in tree.rails["VIN_5V"].unknown} == {"J1", "R2", "R4", "U1"}

    # With nothing else on the middle net the pair is a plain divider,
    # and its current is Ohm's law: 3.3 V over 20 kilohms.
    del data["components"]["R5"], data["nets"]["OFF"], data["connections"]["OFF"]
    data["connections"]["MID"] = ["R2.2", "R3.1"]
    plain = build_tree(ground(Design.model_validate(data), index)).rails["+3V3"]
    assert sorted(u.ref for u in plain.unknown) == ["R4"]
    divider = next(d for d in plain.draws if d.kind == "divider")
    assert divider.what == "R2 and R3 to ground"
    assert divider.current == pytest.approx(3.3 / 20e3)


def test_an_led_branch_shared_with_a_chip_pin_is_not_solved(
    index: LibraryIndex,
) -> None:
    # A chip's pin on the net between R1 and D1 can carry current of its
    # own, so the branch is no longer a resistor and a diode in series.
    data = example()
    data["components"]["U2"] = {"lib_id": "Device:QUAD"}
    data["connections"]["LED_A"].append("U2.EN")
    data["connections"]["GND"].append("U2.GND*")
    tree = build_tree(ground(Design.model_validate(data), index))
    rail = tree.rails["+3V3"]
    assert [u.ref for u in rail.unknown] == ["R1"]
    assert rail.current == pytest.approx(0.3)


def test_a_capacitor_marked_without_a_unit_is_not_read_as_a_resistor(
    index: LibraryIndex,
) -> None:
    # Found on a real board: "100n" across a rail was read as 100 nanoohms
    # and the rail's current came out in millions of amps.
    data = example()
    data["components"]["C2"]["value"] = "100n"
    tree = build_tree(ground(Design.model_validate(data), index))
    assert tree.rails["+3V3"].current == pytest.approx(0.3013)
    assert not tree.rails["+3V3"].unknown


def test_a_two_pin_part_of_no_known_kind_on_a_rail_is_named(
    index: LibraryIndex,
) -> None:
    # A bead, a fuse, a diode: current may pass through it, so the rail's
    # total is not complete until its current is stated.
    data = example()
    data["components"]["D2"] = {"lib_id": "Device:LED"}
    data["connections"]["+3V3"].append("D2.A")
    data["connections"]["GND"].append("D2.K")
    rail = build_tree(ground(Design.model_validate(data), index)).rails["+3V3"]
    assert [u.ref for u in rail.unknown] == ["D2"]
    assert "i_supply" in rail.unknown[0].resolve
    data["components"]["D2"]["params"] = {"i_supply": "2mA"}
    rail = build_tree(ground(Design.model_validate(data), index)).rails["+3V3"]
    assert not rail.unknown
    assert rail.current == pytest.approx(0.3033)


def stability(index: LibraryIndex, change: Any) -> RequirementResult:
    data = example()
    data["components"]["U1"]["params"].update(
        {"c_out_min": "10uF", "c_out_esr_min": "0.3ohm", "c_out_esr_max": "22ohm"}
    )
    data["components"]["C2"]["params"] = {
        "tolerance": "10%",
        "dielectric": "tantalum",
        "esr_min": "0.5ohm",
        "esr_max": "2ohm",
    }
    change(data)
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    return next(r for r in results if r.type == "regulator_stability")


def test_an_output_capacitor_is_held_to_the_regulators_stable_region(
    index: LibraryIndex,
) -> None:
    # The LM1117's region: at least 10 uF, ESR from 0.3 ohm to 22 ohm.
    good = stability(index, lambda d: None)
    assert good.status == "pass"
    assert "at least 19.8 uF with an ESR from 0.5 to 2 ohm" in good.summary

    def ceramic(d: Any) -> None:
        d["components"]["C2"]["params"].update(esr_min="2mohm", esr_max="10mohm")

    low = stability(index, ceramic)
    assert low.status == "fail"
    assert (
        "ESR can be 0.002 ohm, below the 0.3 ohm U1's datasheet requires" in low.summary
    )

    def small(d: Any) -> None:
        d["components"]["C2"]["value"] = "10uF"

    assert stability(index, small).status == "fail"
    assert "can be 9 uF, below the 10 uF" in stability(index, small).summary

    def high(d: Any) -> None:
        d["components"]["C2"]["params"]["esr_max"] = "30ohm"

    assert "above the 22 ohm U1's datasheet allows" in stability(index, high).summary


def test_stability_is_never_assumed(index: LibraryIndex) -> None:
    def no_region(d: Any) -> None:
        del d["components"]["U1"]["params"]["c_out_esr_min"]

    result = stability(index, no_region)
    assert result.status == "unverified" and "no c_out_esr_min" in result.missing[0]

    def one_value(d: Any) -> None:
        d["components"]["C2"]["params"] = {
            "tolerance": "10%",
            "dielectric": "tantalum",
            "esr": "1ohm",
        }

    result = stability(index, one_value)
    assert result.status == "unverified"
    assert "one value is not a range" in result.missing[0]

    def class_two(d: Any) -> None:
        d["components"]["C2"]["params"]["dielectric"] = "X5R"

    # A class II ceramic's capacitance under bias needs its curve.
    result = stability(index, class_two)
    assert result.status == "unverified" and "DC-bias curve" in " ".join(result.missing)

    def unknown(d: Any) -> None:
        d["components"]["C2"]["params"]["dielectric"] = "mystery"

    assert (
        "does not know the dielectric mystery" in stability(index, unknown).missing[0]
    )

    def two(d: Any) -> None:
        d["components"]["C3"] = dict(d["components"]["C2"])
        d["connections"]["+3V3"].append("C3.1")
        d["connections"]["GND"].append("C3.2")

    result = stability(index, two)
    assert result.status == "unverified" and "combined ESR" in result.summary

    def none(d: Any) -> None:
        d["connections"]["+3V3"].remove("C2.1")
        d["connections"]["GND"].remove("C2.2")
        del d["components"]["C2"]
        d["blocks"] = {}
        d["placement"] = {}

    assert stability(index, none).status == "fail"

    def unknown_but_failing(d: Any) -> None:
        # No tolerance on the capacitance, and an ESR that is too low:
        # what is known already fails, so it fails.
        d["components"]["C2"]["params"] = {
            "dielectric": "tantalum",
            "esr_min": "0.1ohm",
            "esr_max": "2ohm",
        }

    assert stability(index, unknown_but_failing).status == "fail"


def junction(index: LibraryIndex, change: Any) -> RequirementResult:
    data = example()
    params = data["components"]["U1"]["params"]
    params.update({"v_out": "3.3V", "v_out_tolerance": "2%", "i_q": "10mA"})
    data["loads"] = [{"net": "+3V3", "current": "300mA"}]
    # Without the LED branch the rail's current is the stated load alone.
    for net in ("+3V3", "LED_A", "GND"):
        data["connections"][net] = [
            p for p in data["connections"][net] if p.split(".")[0] not in ("R1", "D1")
        ]
    del data["connections"]["LED_A"], data["nets"]["LED_A"]
    del data["components"]["R1"], data["components"]["D1"]
    data["blocks"] = {}
    data["placement"] = {}
    data["requirements"] = [REQUIREMENTS[2]]
    change(data)
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    return results[0]


def test_a_supply_tolerance_and_datasheet_limits_let_a_junction_be_proven(
    index: LibraryIndex,
) -> None:
    plain = junction(index, lambda d: None)
    assert plain.status == "unverified"
    assert set(plain.missing) == {
        "no tolerance on V(VIN_5V)",
        "U1: no tolerance on Iq",
        "U1: no tolerance on theta_JA",
    }

    def stated(d: Any) -> None:
        d["nets"]["VIN_5V"]["tolerance"] = "5%"
        d["components"]["U1"]["limits"] = {"i_q": "max", "theta_ja": "max"}

    proven = junction(index, stated)
    assert proven.status == "pass"

    # A junction's maximum needs each value's maximum. A stated minimum,
    # or a limit that does not say which it is, proves nothing here.
    def wrong_side(d: Any) -> None:
        stated(d)
        d["components"]["U1"]["limits"]["i_q"] = "min"

    def no_side(d: Any) -> None:
        stated(d)
        d["components"]["U1"]["limits"] = ["i_q", "theta_ja"]

    wrong = junction(index, wrong_side)
    assert wrong.status == "unverified"
    assert any("Iq is stated as its minimum" in m for m in wrong.missing)
    unsaid = junction(index, no_side)
    assert unsaid.status == "unverified"
    assert any("does not say whether" in m for m in unsaid.missing)
    assert any('"i_q": "max"' in r for r in unsaid.to_resolve)
    # Worst case: 5.25 V in, 3.234 V out, 300 mA, 10 mA of its own:
    # (5.25 - 3.234) * 0.3 + 5.25 * 0.01 = 0.6573 W, times 90 degC/W, plus 40.
    assert "at most 99.2 degC over the tolerances" in proven.summary

    def only_supply(d: Any) -> None:
        d["nets"]["VIN_5V"]["tolerance"] = "5%"

    # A value that is not marked as a limit still has no tolerance.
    assert set(junction(index, only_supply).missing) == {
        "U1: no tolerance on Iq",
        "U1: no tolerance on theta_JA",
    }
    with pytest.raises(ValueError, match="limits names i_adj, which params does not"):
        junction(index, lambda d: d["components"]["U1"].update(limits=["i_adj"]))


def test_a_thermal_policy_lets_a_typical_theta_ja_be_used_and_says_so(
    index: LibraryIndex,
) -> None:
    def with_margin(margin: str | None) -> Any:
        def change(d: Any) -> None:
            d["nets"]["VIN_5V"]["tolerance"] = "5%"
            d["components"]["U1"]["limits"] = {"i_q": "max"}
            if margin is not None:
                d["policy"] = {"theta_ja_margin": margin}

        return change

    # A datasheet's theta_JA is typical, so with no policy nothing is proven.
    assert junction(index, with_margin(None)).status == "unverified"
    # Worst case as before, 0.6573 W, now times 90 degC/W plus 20%:
    # 0.6573 * 108 + 40 = 110.99 degC.
    met = junction(index, with_margin("20%"))
    assert met.status == "pass"
    assert "at most 111.0 degC over the tolerances" in met.summary
    assert "thermal policy" in met.summary and "20%" in met.summary
    theta = next(g for g in met.results[0].inputs if g.name == "theta_JA")
    assert "thermal policy" in theta.source
    # A margin the junction does not survive fails: 0.6573 * 135 + 40 = 128.7.
    assert junction(index, with_margin("50%")).status == "fail"

    # A stated maximum needs no policy and is not widened by one.
    def both(d: Any) -> None:
        with_margin("20%")(d)
        d["components"]["U1"]["limits"]["theta_ja"] = "max"

    exact = junction(index, both)
    assert "at most 99.2 degC" in exact.summary and "policy" not in exact.summary


def test_a_rail_that_feeds_an_indicator_has_a_worst_case_current(
    index: LibraryIndex,
) -> None:
    def data_with(v_f_tolerance: str | None) -> dict[str, Any]:
        data = example()
        params = data["components"]["U1"]["params"]
        params.update({"v_out": "3.3V", "v_out_tolerance": "2%", "i_q": "10mA"})
        data["components"]["U1"]["limits"] = {"i_q": "max", "theta_ja": "max"}
        data["nets"]["VIN_5V"]["tolerance"] = "5%"
        if v_f_tolerance is not None:
            data["components"]["D1"]["params"]["v_f_tolerance"] = v_f_tolerance
        data["requirements"] = [REQUIREMENTS[2]]
        return data

    def result(data: dict[str, Any]) -> RequirementResult:
        return evaluate_requirements(ground(Design.model_validate(data), index))[1][0]

    proven = result(data_with("10%"))
    assert proven.status == "pass", proven
    # The indicator's most: (3.3 * 1.02 - 2.0 * 0.9) / (1000 * 0.99) = 1.5818 mA,
    # on top of the 300 mA load. Then, at 5.25 V in and 3.234 V out,
    # ((5.25 - 3.234) * 0.3015818 + 5.25 * 0.01) * 90 + 40 = 99.44 degC.
    assert "at most 99.4 degC over the tolerances" in proven.summary
    current = next(g for g in proven.results[0].inputs if g.name == "Iout")
    assert current.number == pytest.approx(0.3015818, abs=1e-7)
    assert "most" in current.origin

    # The 99.44 is a bound: the indicator draws most at the highest output
    # voltage and the regulator drops most at the lowest, where the true
    # worst case is 99.42. A limit between the two is neither proven nor
    # broken.
    between = data_with("10%")
    between["requirements"][0] = {**REQUIREMENTS[2], "limit": "99.43degC"}
    sliver = result(between)
    assert sliver.status == "unverified"
    assert "a bound that is not reached" in sliver.summary

    # With the diode's spread not given, the most is not known and nothing
    # is proven; the result names the diode.
    unknown = result(data_with(None))
    assert unknown.status == "unverified"
    assert any("D1" in m and "forward voltage" in m for m in unknown.missing)
    assert any("v_f_tolerance" in r for r in unknown.to_resolve)


def attenuation(index: LibraryIndex, wanted: str, change: Any = None) -> Any:
    data: dict[str, Any] = {
        "design": "rc",
        "policy": {"margins": "stated"},
        "nets": {
            "IN": {"role": "signal"},
            "OUT": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        "components": {
            "R1": {"lib_id": "Device:R", "value": "1k", "params": {"tolerance": "1%"}},
            "C1": {
                "lib_id": "Device:C",
                "value": "100nF",
                "params": {"tolerance": "5%", "dielectric": "C0G"},
            },
        },
        "connections": {"IN": ["R1.1"], "OUT": ["R1.2", "C1.1"], "GND": ["C1.2"]},
        "requirements": [
            {
                "type": "min_attenuation",
                "input": "IN",
                "output": "OUT",
                "frequency": "10kHz",
                "value": wanted,
            }
        ],
    }
    if change is not None:
        change(data)
    return evaluate_requirements(ground(Design.model_validate(data), index))[1][0]


def test_a_filters_loss_is_judged_with_its_parts_at_their_lowest(
    index: LibraryIndex,
) -> None:
    # Nominal: x = 2 pi * 10 kHz * 1 k * 100 nF = 6.2832, loss
    # 10 log10(1 + x^2) = 16.07 dB. Lowest: 990 ohm and 95 nF give
    # x = 5.9094 and 15.55 dB.
    met = attenuation(index, "15dB")
    assert met.status == "pass"
    assert "16.07 dB at nominal and at least 15.55 dB" in met.summary
    # Between the two, the nominal value would pass and the corner does not.
    assert attenuation(index, "16dB").status == "fail"
    assert attenuation(index, "20dB").status == "fail"


def test_a_filters_loss_is_not_established_on_what_is_not_known(
    index: LibraryIndex,
) -> None:
    bare = attenuation(index, "15dB", lambda d: d["components"]["R1"].pop("params"))
    assert bare.status == "unverified"
    assert any("no tolerance on R" in m for m in bare.missing)

    def x7r(d: Any) -> None:
        d["components"]["C1"]["params"]["dielectric"] = "X7R"

    lossy = attenuation(index, "15dB", x7r)
    assert lossy.status == "unverified" and "X7R" in lossy.missing[0]

    def loaded(d: Any) -> None:
        d["components"]["U1"] = {"lib_id": "Device:QUAD"}
        d["connections"]["OUT"].append("U1.EN")
        d["connections"]["GND"].append("U1.GND*")

    other = attenuation(index, "15dB", loaded)
    assert other.status == "unverified" and "U1 also on the output" in other.missing[0]


def adjustable(index: LibraryIndex, change: Any) -> RequirementResult:
    data = example()
    data["nets"]["ADJ"] = {"role": "signal"}
    data["nets"]["+3V3"]["voltage"] = "5V"
    params = data["components"]["U1"]["params"]
    params.update({"v_ref": "1.25V", "v_ref_tolerance": "1%", "i_adj": "100uA"})
    data["components"]["RA"] = {
        "lib_id": "Device:R",
        "value": "240",
        "params": {"tolerance": "1%"},
    }
    data["components"]["RB"] = {
        "lib_id": "Device:R",
        "value": "715",
        "params": {"tolerance": "1%"},
    }
    data["connections"]["+3V3"].append("RA.1")
    # The small library's regulator has a ground pin where an adjustable
    # one has its adjust pin; here that pin is the divider's tap.
    data["connections"]["GND"].remove("U1.GND")
    data["connections"]["ADJ"] = ["U1.GND", "RA.2", "RB.1"]
    data["connections"]["GND"].append("RB.2")
    data["requirements"] = [
        {"type": "rail_voltage", "net": "+3V3", "nominal": "5V", "tolerance": "5%"}
    ]
    change(data)
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    return results[0]


def test_an_adjustable_regulators_output_comes_from_its_divider(
    index: LibraryIndex,
) -> None:
    good = adjustable(index, lambda d: None)
    assert good.status == "pass"
    # Lowest: 1.2375 * (1 + 707.85 / 242.4) with no adjust current = 4.851.
    # Highest: 1.2625 * (1 + 722.15 / 237.6) + 100 uA * 722.15 = 5.172.
    assert "+3V3 is 4.851 V to 5.172 V from U1's reference" in good.summary
    assert "(RA, RB)" in good.summary

    def loose_reference(d: Any) -> None:
        d["components"]["U1"]["params"]["v_ref_tolerance"] = "4%"

    # The LM317's own 4% takes the output outside 5%.
    wide = adjustable(index, loose_reference)
    assert wide.status == "fail" and "4.704 V to 5.323 V" in wide.summary

    def no_tolerance(d: Any) -> None:
        del d["components"]["U1"]["params"]["v_ref_tolerance"]

    result = adjustable(index, no_tolerance)
    assert (
        result.status == "unverified" and "range is not established" in result.summary
    )

    def no_adjust_current(d: Any) -> None:
        del d["components"]["U1"]["params"]["i_adj"]

    result = adjustable(index, no_adjust_current)
    assert (
        result.status == "unverified" and "no adjust-pin current" in result.missing[0]
    )

    def no_divider(d: Any) -> None:
        d["connections"]["ADJ"].remove("RB.1")
        d["connections"]["GND"].remove("RB.2")
        del d["components"]["RB"]

    result = adjustable(index, no_divider)
    assert result.status == "unverified" and "divider was not found" in result.summary


def test_a_tantalum_capacitor_counts_at_its_marked_value_less_tolerance(
    index: LibraryIndex,
) -> None:
    data = example()
    data["components"]["C2"]["params"] = {"tolerance": "10%", "dielectric": "tantalum"}
    data["requirements"] = [
        {"type": "min_capacitance", "between": ["+3V3", "GND"], "value": "19uF"}
    ]
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    assert results[0].status == "pass" and "19.8 uF" in results[0].summary
    data["requirements"][0]["value"] = "22uF"
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    assert results[0].status == "fail"


def test_a_rails_greatest_current_is_followed_through_a_linear_regulator(
    index: LibraryIndex,
) -> None:
    from kairo.verify import requirements

    def most(change: Any) -> Any:
        data = example()
        change(data)
        grounded = ground(Design.model_validate(data), index)
        return requirements._load_current(  # pyright: ignore[reportPrivateUsage]
            grounded.design, build_tree(grounded), "VIN_5V"
        )

    def tolerances(data: dict[str, Any]) -> None:
        data["components"]["U1"]["params"]["v_out_tolerance"] = "2%"
        data["components"]["D1"]["params"]["v_f_tolerance"] = "10%"
        # The small library's connector is not one of KiCad's connector
        # libraries, so it must say it draws nothing itself.
        data["components"]["J1"].setdefault("params", {})["i_supply"] = "0A"
        data["components"]["J1"]["limits"] = {"i_supply": "max"}

    def with_quiescent(data: dict[str, Any], limits: Any = None) -> None:
        tolerances(data)
        data["components"]["U1"]["params"]["i_q"] = "5mA"
        if limits is not None:
            data["components"]["U1"]["limits"] = limits

    # The input rail feeds only the regulator, which passes on what its
    # output rail draws and its own current. With that own current not
    # given there is no greatest, whatever the rest come to: it once came
    # out as 301.6 mA, the rest alone, as if it were the most.
    unknown = most(tolerances)
    assert not unknown.is_bound
    assert unknown.missing == ("U1's quiescent current is not given",)

    # What the output rail lacks is named from the input rail, beside what
    # the input rail itself lacks, and nothing is filled in.
    bare = most(lambda data: None)
    assert not bare.is_bound
    assert bare.missing == (
        "J1 (Device:Conn) joins VIN_5V to GND, and the current through it is not known",
        "U1's quiescent current is not given",
        "+3V3, fed through U1: +3V3: no tolerance on its voltage",
        "+3V3, fed through U1: D1: no tolerance on its forward voltage",
    )

    # The regulator's own current counts only if the design marks the
    # stated figure as the datasheet's maximum: a typical one is no bound.
    # Marked, the output rail's most is the 300 mA load and the LED branch
    # at its own worst case, (3.366 - 1.8) / 990 = 1.5818 mA, plus 5 mA.
    typical = most(with_quiescent)
    assert typical.missing == (
        "U1 quiescent: U1's stated i_q is not marked as a maximum, so it may be "
        "a typical figure",
    )
    assert "list it under limits as max" in typical.resolve[0]
    marked = most(lambda data: with_quiescent(data, {"i_q": "max"}))
    assert marked.is_bound and not marked.missing
    assert marked.given.value.value == pytest.approx(0.3 + (3.366 - 1.8) / 990 + 0.005)
    # Marked as a minimum, it is still not a maximum.
    assert most(lambda data: with_quiescent(data, {"i_q": "min"})).missing
