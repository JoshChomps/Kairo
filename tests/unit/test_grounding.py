# These tests change plain JSON data with small lambdas, which a strict type
# checker cannot follow; the checks that follow do not help here.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from minilib import ldo, make_index

from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import GroundingError, ground
from kairo.library.resolve import PinError, resolve_pin


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def test_a_name_resolves_exactly_then_by_case_then_by_alias(
    index: LibraryIndex,
) -> None:
    regulator = index.symbol("Reg:REG-3.3")
    assert regulator is not None
    assert [p.number for p in resolve_pin(regulator, "U1", "VO")] == ["2"]
    assert [p.number for p in resolve_pin(regulator, "U1", "vo")] == ["2"]
    with pytest.raises(PinError, match="E_PIN_UNKNOWN") as caught:
        resolve_pin(regulator, "U1", "VIN")
    assert caught.value.pins == ["GND (1)", "VO (2)", "VI (3)"]
    assert "VI (3)" in str(caught.value)
    aliases = {"VIN": "VI"}
    assert [p.number for p in resolve_pin(regulator, "U1", "VIN", aliases=aliases)] == [
        "3"
    ]


def test_a_number_is_a_pin_only_where_the_names_are_not(index: LibraryIndex) -> None:
    resistor, regulator = index.symbol("Device:R"), index.symbol("Reg:REG-3.3")
    assert resistor is not None and regulator is not None
    assert [p.number for p in resolve_pin(resistor, "R1", "2")] == ["2"]
    # A regulator's pins have names, so a number is not accepted for one:
    # a model that recalls "pin 3 is the input" must name the pin instead.
    with pytest.raises(PinError, match="E_PIN_UNKNOWN"):
        resolve_pin(regulator, "U1", "3")
    # A symbol can mix the two: its unnamed pins are reached by number, its
    # named ones only by name.
    gate = index.symbol("Device:GATE")
    assert gate is not None
    assert [p.number for p in resolve_pin(gate, "U3", "2")] == ["2"]
    assert [p.number for p in resolve_pin(gate, "U3", "VCC")] == ["5"]
    with pytest.raises(PinError, match="E_PIN_UNKNOWN"):
        resolve_pin(gate, "U3", "5")


def test_a_name_on_several_pins_needs_a_star(index: LibraryIndex) -> None:
    quad = index.symbol("Device:QUAD")
    assert quad is not None
    with pytest.raises(PinError, match=r"E_PIN_AMBIGUOUS.*U2\.GND\*"):
        resolve_pin(quad, "U2", "GND")
    assert [p.number for p in resolve_pin(quad, "U2", "GND*")] == ["1", "2"]


def test_the_plans_example_grounds(index: LibraryIndex) -> None:
    grounded = ground(Design.model_validate(ldo()), index)
    assert [(p.ref, p.pin.number) for p in grounded.net_pins("VIN_5V")] == [
        ("J1", "1"),
        ("U1", "3"),
        ("C1", "1"),
    ]
    assert len(grounded.pins) == 13
    assert grounded.symbols["U1"].name == "REG-3.3"


def changed(change: Any) -> Design:
    data = ldo()
    change(data)
    return Design.model_validate(data)


@pytest.mark.parametrize(
    ("change", "code", "says"),
    [
        (
            lambda d: d["components"]["U1"].update(lib_id="Reg:REG-3.4"),
            "E_SYMBOL_UNKNOWN",
            "Reg:REG-3.3",
        ),
        (
            lambda d: d["components"]["R1"].update(footprint="Pkg:R_0602"),
            "E_FOOTPRINT_UNKNOWN",
            "Pkg:R_0602",
        ),
        (
            lambda d: d["connections"]["VIN_5V"].__setitem__(1, "U1.VIN"),
            "E_PIN_UNKNOWN",
            "VI (3)",
        ),
        (
            lambda d: d["connections"]["LED_A"].append("D1.k"),
            "E_PIN_ON_TWO_NETS",
            "LED_A and GND",
        ),
        (lambda d: d["components"]["R1"].update(unit=2), "E_UNIT_UNKNOWN", "unit 2"),
        (
            # A three-pin regulator on a two-pad footprint.
            lambda d: d["components"]["U1"].update(footprint="Pkg:R_0603"),
            "E_PIN_PAD_MISMATCH",
            "pin(s) 3 with no pad of that number on Pkg:R_0603 (its pads: 1, 2)",
        ),
    ],
)
def test_every_seeded_wrong_name_is_refused_with_what_exists(
    index: LibraryIndex, change: Any, code: str, says: str
) -> None:
    with pytest.raises(GroundingError) as caught:
        ground(changed(change), index)
    (finding,) = caught.value.findings
    assert (finding.layer, finding.code, finding.severity) == ("L1", code, "error")
    assert says in finding.message


def test_every_problem_is_reported_not_only_the_first(index: LibraryIndex) -> None:
    def change(d: dict[str, Any]) -> None:
        d["components"]["C1"]["lib_id"] = "Device:Cap"
        d["connections"]["+3V3"][0] = "U1.VOUT"
        d["connections"]["LED_A"][1] = "D1.ANODE"

    with pytest.raises(GroundingError) as caught:
        ground(changed(change), index)
    assert sorted(f.code for f in caught.value.findings) == [
        "E_PIN_UNKNOWN",
        "E_PIN_UNKNOWN",
        "E_SYMBOL_UNKNOWN",
    ]
    assert {f.where.ref for f in caught.value.findings} == {"C1", "U1", "D1"}
