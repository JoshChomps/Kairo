# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from minilib import ldo, make_index
from pydantic import ValidationError

from kairo.ir import Design, PatchError, apply_patch, design_schema, parse_patch
from kairo.library.grounding import GroundingError, ground
from kairo.units import Quantity


def patched(*operations: dict[str, Any]) -> Design:
    return apply_patch(Design.model_validate(ldo()), parse_patch(list(operations)))


def test_a_part_is_added_connected_and_placed() -> None:
    design = patched(
        {"op": "add_net", "name": "EN", "net": {"role": "signal"}},
        {
            "op": "add_component",
            "ref": "R2",
            "component": {"lib_id": "Device:R", "value": "10k"},
            "block": "regulator",
        },
        {"op": "connect", "net": "EN", "pins": ["R2.1"]},
        {"op": "connect", "net": "GND", "pins": ["R2.2"]},
    )
    assert design.components["R2"].value == "10k"
    assert design.connections["EN"] == ["R2.1"]
    assert design.connections["GND"][-1] == "R2.2"
    assert design.blocks["regulator"] == ["U1", "C2", "R2"]
    # Everything else is as it was.
    before = Design.model_validate(ldo())
    assert design.components["U1"] == before.components["U1"]
    assert design.connections["VIN_5V"] == before.connections["VIN_5V"]


def test_the_original_design_is_not_changed() -> None:
    original = Design.model_validate(ldo())
    kept = original.model_dump(mode="json")
    apply_patch(original, parse_patch([{"op": "remove", "ref": "D1"}]))
    assert original.model_dump(mode="json") == kept


def test_a_parameter_is_set_and_removed() -> None:
    design = patched(
        {"op": "set_param", "ref": "U1", "name": "i_q", "value": "5mA"},
        {"op": "set_param", "ref": "U1", "name": "v_dropout", "value": None},
        {"op": "set_param", "ref": "R1", "name": "value", "value": "2k2"},
        {
            "op": "set_param",
            "ref": "U1",
            "name": "source",
            "value": "AMS1117 rev 1.4 p.3",
        },
    )
    assert design.components["U1"].quantity("i_q") == Quantity(0.005, "A")
    assert "v_dropout" not in design.components["U1"].params
    assert design.components["R1"].value == "2k2"
    assert design.components["U1"].source == "AMS1117 rev 1.4 p.3"


def test_removing_a_part_takes_its_pins_and_its_place() -> None:
    design = patched({"op": "remove", "ref": "D1"})
    assert "D1" not in design.components
    assert design.connections["LED_A"] == ["R1.2"]
    assert "D1.K" not in design.connections["GND"]
    assert design.blocks["indicator"] == ["R1"]


def test_a_pin_moves_only_by_disconnecting_first() -> None:
    with pytest.raises(PatchError, match=r"operation 0: R1\.2 is on LED_A"):
        patched({"op": "connect", "net": "GND", "pins": ["R1.2"]})
    design = patched(
        {"op": "disconnect", "pins": ["R1.2"]},
        {"op": "connect", "net": "GND", "pins": ["R1.2"]},
    )
    assert design.connections["LED_A"] == ["D1.A"]
    # Connecting a pin where it already is changes nothing.
    same = patched({"op": "connect", "net": "LED_A", "pins": ["R1.2"]})
    assert same == Design.model_validate(ldo())


@pytest.mark.parametrize(
    ("operation", "says"),
    [
        ({"op": "add_net", "name": "GND", "net": {"role": "ground"}}, "already exists"),
        (
            {"op": "add_component", "ref": "R1", "component": {"lib_id": "Device:R"}},
            "already exists",
        ),
        ({"op": "connect", "net": "VBAT", "pins": ["R1.1"]}, "add_net first"),
        ({"op": "connect", "net": "GND", "pins": ["R1"]}, "REF.PIN"),
        ({"op": "disconnect", "pins": ["R1.9"]}, "not connected"),
        ({"op": "set_param", "ref": "R1", "name": "colour", "value": "red"}, "colour"),
        ({"op": "set_param", "ref": "R9", "name": "value", "value": "1k"}, "R9"),
        ({"op": "remove", "ref": "R9"}, "does not exist"),
    ],
)
def test_an_operation_that_cannot_apply_is_named(
    operation: dict[str, Any], says: str
) -> None:
    with pytest.raises(PatchError, match="operation 0") as caught:
        patched(operation)
    assert says in str(caught.value) and caught.value.index == 0


def test_a_patch_that_leaves_an_invalid_design_is_refused_whole() -> None:
    # U1 is named by a requirement, so it cannot simply disappear.
    with pytest.raises(PatchError, match="part 'U1'") as caught:
        patched({"op": "remove", "ref": "U1"})
    assert caught.value.index is None
    with pytest.raises(PatchError, match="v_f"):
        patched({"op": "set_param", "ref": "D1", "name": "v_f", "value": "2"})


def test_unknown_operations_are_refused() -> None:
    with pytest.raises(PatchError, match="E_PATCH_INVALID"):
        parse_patch([{"op": "teleport", "ref": "R1"}])
    with pytest.raises(PatchError):
        parse_patch([{"op": "remove", "ref": "R1", "also": "everything"}])
    with pytest.raises(PatchError):
        parse_patch({"op": "remove", "ref": "R1"})


def test_errors_name_the_field_at_fault() -> None:
    data = ldo()
    data["components"]["U1"]["params"]["theta_ja"] = "90"
    data["nets"]["+3V3"]["voltage"] = "3.3"
    data["loads"][0]["current"] = "lots"
    with pytest.raises(ValidationError) as caught:
        Design.model_validate(data)
    places = {
        ".".join(str(part) for part in error["loc"]) for error in caught.value.errors()
    }
    assert places == {"components.U1", "nets.+3V3.voltage", "loads.0.current"}


def test_the_plans_example_validates_as_written_and_grounding_corrects_it(
    tmp_path: Path,
) -> None:
    # The plan wrote the connector's pins as J1.1 and J1.2 and called its pin
    # names illustrative. That is a valid design; it is grounding that says
    # the library calls them Pin_1 and Pin_2.
    data = ldo()
    data["connections"]["VIN_5V"][0] = "J1.1"
    data["connections"]["GND"][0] = "J1.2"
    design = Design.model_validate(data)
    with pytest.raises(GroundingError) as caught:
        ground(design, make_index(tmp_path))
    assert [f.code for f in caught.value.findings] == ["E_PIN_UNKNOWN"] * 2
    assert "Pin_1 (1)" in caught.value.findings[0].message


def test_the_schema_lists_everything_a_model_may_write() -> None:
    schema = design_schema()
    assert json.loads(json.dumps(schema)) == schema
    assert schema["parameters"]["theta_ja"] == "degC/W"
    assert schema["parameters"]["dielectric"] == "text"
    requirement_types = {
        name
        for name, definition in schema["design"]["$defs"].items()
        if "type" in definition.get("properties", {})
    }
    assert requirement_types == {
        "MinCapacitance",
        "RailVoltage",
        "MaxJunctionTemp",
        "MaxComponentPower",
        "MinAttenuation",
        "MaxInductorCurrent",
        "MaxRailCurrent",
        "MaxRipple",
        "MinHoldupTime",
        "ProgrammedCurrent",
        "Setting",
    }
    operations = json.dumps(schema["patch_operations"])
    for name in (
        "add_net",
        "add_component",
        "connect",
        "disconnect",
        "set_param",
        "remove",
    ):
        assert f'"{name}"' in operations
    assert any("unit" in rule for rule in schema["rules"])


def test_the_schema_shows_a_whole_design_that_is_valid() -> None:
    from kairo.ir import Design
    from kairo.ir.schema import EXAMPLE

    assert design_schema("design")["example"] == EXAMPLE
    Design.model_validate(EXAMPLE)
    # Asked for the patch operations alone, it says a design is elsewhere:
    # a local model asked for this part, never saw a design's shape, and
    # guessed at one 25 times.
    assert "part 'design'" in design_schema("patch")["more"]
    assert "example" not in design_schema("patch")
