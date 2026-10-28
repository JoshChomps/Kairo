"""Kairo's default margins: used where no tolerance is stated, and always named."""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from minilib import buck, ldo, make_index

from kairo.accuracy.report import accuracy_report
from kairo.defaults import DEFAULT_MARGINS, collecting, net_spread, part_spread
from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.verify.requirements import RequirementResult, evaluate_requirements


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def stage(change: Any = None, **policy: Any) -> dict[str, Any]:
    """The step-down stage with Kairo's default margins on."""
    data = buck()
    data["policy"] = policy
    if change is not None:
        change(data)
    return data


def run(index: LibraryIndex, data: dict[str, Any]) -> dict[str, RequirementResult]:
    _, results = evaluate_requirements(ground(Design.model_validate(data), index))
    return {result.type: result for result in results}


def test_a_stated_figure_is_used_and_a_missing_one_gets_the_default() -> None:
    design = Design.model_validate(stage())
    with collecting() as used:
        stated = part_spread(design, design.components["R1"], "tolerance")
        assert stated is not None and (stated.value, stated.is_default) == (1.0, False)
        assert used == []
        # C1 states no tolerance: a capacitor's default, named with the part.
        fallback = part_spread(design, design.components["C1"], "tolerance")
        assert fallback is not None and fallback.is_default
        assert fallback.value == DEFAULT_MARGINS["capacitor"] == 20.0
        assert fallback.source == (
            "Kairo's default margin for a capacitor (20%); C1 states none"
        )
        supply = net_spread(design, "VIN")
        assert supply is not None and (supply.value, supply.is_default) == (5.0, False)
        # A regulator is no resistor, capacitor or inductor: it has no
        # general tolerance, and gets none.
        assert part_spread(design, design.components["U1"], "tolerance") is None
    assert used == ["C1's tolerance: 20%"]


def test_a_project_can_change_a_margin_or_turn_them_off() -> None:
    wider = Design.model_validate(stage(default_margins={"capacitor": "30%"}))
    spread = part_spread(wider, wider.components["C1"], "tolerance")
    assert spread is not None and spread.value == 30.0
    off = Design.model_validate(stage(margins="stated"))
    assert part_spread(off, off.components["C1"], "tolerance") is None
    with pytest.raises(ValueError, match="default_margins"):
        Design.model_validate(stage(default_margins={"transistor": "30%"}))


def test_a_check_that_rests_on_a_default_passes_and_says_which(
    index: LibraryIndex,
) -> None:
    def no_tolerances(data: dict[str, Any]) -> None:
        del data["components"]["L1"]["params"]["tolerance"]
        del data["components"]["R1"]["params"]
        del data["components"]["R2"]["params"]

    # With the inductor at Kairo's 30% and the divider at 5% each:
    #   the rail is 0.581 * (1 + 95/13.965) to 0.611 * (1 + 105/12.635),
    #   4.533 V to 5.689 V, which is outside 5 V within 7%: a fail, and the
    #   result says the defaults may be why.
    results = run(index, stage(no_tolerances))
    rail = results["rail_voltage"]
    assert rail.status == "fail" and rail.basis == "default margins"
    assert rail.on_defaults == ("R1's tolerance: 5%", "R2's tolerance: 5%")
    assert "On Kairo's default margins: R1's tolerance: 5%" in rail.summary
    assert (
        "State the real figures (R1's tolerance, R2's tolerance)"
        in (rail.to_resolve[-1])
    )
    # The peak current still passes, and names every default behind it.
    peak = results["max_inductor_current"]
    assert peak.status == "pass" and peak.basis == "default margins"
    assert "L1's tolerance: 30%" in peak.on_defaults
    shown = peak.as_dict()
    assert shown["basis"] == "default margins"
    assert shown["on_default_margins"] == list(peak.on_defaults)

    # The same design with every figure stated rests on none.
    stated = run(index, stage())
    assert all(r.on_defaults == () for r in stated.values())
    assert stated["max_inductor_current"].basis == "stated values"
    assert "on_default_margins" not in stated["max_inductor_current"].as_dict()

    # With the defaults turned off, the missing figures leave it unverified.
    strict = run(index, stage(no_tolerances, margins="stated"))
    assert strict["max_inductor_current"].status == "unverified"
    assert strict["rail_voltage"].status == "unverified"


def test_a_fact_about_one_part_is_never_defaulted(index: LibraryIndex) -> None:
    # A saturation current, a switching frequency and an ESR are facts
    # about one part, not spreads: no margin stands in for them.
    def no_facts(data: dict[str, Any]) -> None:
        del data["components"]["L1"]["params"]["i_sat"]
        del data["components"]["C3"]["params"]["esr_max"]

    results = run(index, stage(no_facts))
    assert results["max_inductor_current"].status == "unverified"
    assert results["max_ripple"].status == "unverified"
    assert results["max_ripple"].missing == ("C3: no greatest ESR",)


def test_the_accuracy_report_counts_and_lists_what_rested_on_defaults(
    index: LibraryIndex,
) -> None:
    data = ldo()
    data["policy"] = {}
    data["requirements"] = [
        {"type": "rail_voltage", "net": "+3V3", "nominal": "3.3V", "tolerance": "6%"},
        {"type": "max_component_power", "component": "R1", "derating": "50%"},
    ]
    data["components"]["U1"]["params"]["v_out"] = "3.3V"
    grounded = ground(Design.model_validate(data), index)
    _, results = evaluate_requirements(grounded)
    report = accuracy_report(grounded.design, results)
    by_type = {r.type: r for r in results}
    # The regulator states no output tolerance and the LED no spread on its
    # forward voltage: 5% and 20% are Kairo's.
    assert by_type["rail_voltage"].status == "pass"
    assert by_type["rail_voltage"].on_defaults == ("U1's v_out_tolerance: 5%",)
    assert by_type["max_component_power"].status == "pass"
    assert "D1's v_f_tolerance: 20%" in by_type["max_component_power"].on_defaults
    assert report.counts["pass_on_default_margins"] == 2
    assert report.policies[0].startswith(
        "Default margins: where the design states no tolerance"
    )
    assert "U1's v_out_tolerance: 5%" in report.policies[0]
    line = next(r for r in report.results if "rail_voltage" in r["check"])
    assert line["basis"] == "default margins"
