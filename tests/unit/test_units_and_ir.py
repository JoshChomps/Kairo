# These tests change plain JSON data with small lambdas, which a strict type
# checker cannot follow; the checks that follow do not help here.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from minilib import ldo
from pydantic import ValidationError

from kairo.ir import Design, split_pin
from kairo.units import Quantity, QuantityError, format_quantity, parse_quantity

EXAMPLE = (
    Path(__file__).resolve().parent.parent.parent
    / "evals"
    / "reference_designs"
    / "ldo_3v3"
    / "design.json"
)


@pytest.mark.parametrize(
    ("text", "unit", "want"),
    [
        ("5V", "V", Quantity(5.0, "V")),
        ("3.3 V", None, Quantity(3.3, "V")),
        ("300mA", "A", Quantity(0.3, "A")),
        ("22uF", "F", Quantity(22e-6, "F")),
        ("22µF", "F", Quantity(22e-6, "F")),
        ("90degC/W", "degC/W", Quantity(90.0, "degC/W")),
        ("40degC", "degC", Quantity(40.0, "degC")),
        ("10%", "%", Quantity(10.0, "%")),
        ("1k", "ohm", Quantity(1000.0, "ohm")),
        ("4.7kΩ", "ohm", Quantity(4700.0, "ohm")),
        ("1M", None, Quantity(1e6, "")),
        ("0.125W", "W", Quantity(0.125, "W")),
        ("100n", "F", Quantity(100e-9, "F")),
        ("400kHz", "Hz", Quantity(400e3, "Hz")),
    ],
)
def test_quantities_are_read_exactly(
    text: str, unit: str | None, want: Quantity
) -> None:
    assert parse_quantity(text, unit, bare=True) == want


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        ("five volts", "V"),
        ("5", "degC"),
        ("5A", "V"),
        ("5kdegC", "degC"),
        ("", "V"),
        ("1e3", "ohm"),
        ("10 %%", "%"),
        ("3.3V extra", "V"),
        # No unit: amps or milliamps? Kairo does not pick.
        ("300", "A"),
    ],
)
def test_what_is_not_a_quantity_is_an_error_not_a_guess(text: str, unit: str) -> None:
    with pytest.raises(QuantityError):
        parse_quantity(text, unit)


def test_the_plans_example_validates() -> None:
    design = Design.model_validate(json.loads(EXAMPLE.read_text(encoding="utf-8")))
    assert design.components["U1"].quantity("theta_ja") == Quantity(90.0, "degC/W")
    assert design.components["U1"].source == "user-stated"
    assert design.components["C1"].params["dielectric"] == "X5R"
    assert design.nets["+3V3"].voltage == Quantity(3.3, "V")
    assert design.loads[0].current == Quantity(0.3, "A")
    assert [r.type for r in design.requirements] == [
        "min_capacitance",
        "rail_voltage",
        "max_junction_temp",
        "max_component_power",
    ]
    assert design.requirements[0].basis == "effective"  # pyright: ignore[reportAttributeAccessIssue]


def broken(change: Any) -> dict[str, Any]:
    data = ldo()
    change(data)
    return data


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda d: d["nets"]["VIN_5V"].pop("voltage"), "power net needs"),
        (lambda d: d["nets"]["GND"].update(colour="black"), "colour"),
        (
            lambda d: d["components"]["U1"]["params"].update(magic="1V"),
            "unknown parameter 'magic'",
        ),
        (
            lambda d: d["components"]["U1"]["params"].update(v_dropout="1.3A"),
            "v_dropout",
        ),
        (
            lambda d: d["components"]["U1"]["params"].update(theta_ja=90),
            "theta_ja as text",
        ),
        (lambda d: d["components"]["U1"].update(lib_id="AMS1117"), "lib_id"),
        (lambda d: d["connections"]["LED_A"].append("R9.1"), "part 'R9'"),
        (lambda d: d["connections"]["LED_A"].append("C1.1"), "C1.1 is on both"),
        (lambda d: d["connections"].update(NOPE=["R1.1"]), "net 'NOPE'"),
        (lambda d: d["connections"]["LED_A"].append("R1"), "REF.PIN"),
        (lambda d: d["loads"][0].update(net="VBAT"), "net 'VBAT'"),
        (lambda d: d["loads"][0].update(current="300"), "current"),
        (lambda d: d["requirements"].append({"type": "be_good"}), "be_good"),
        (lambda d: d["requirements"][0].update(component="U7"), "part 'U7'"),
        (lambda d: d["blocks"]["indicator"].append("C1"), "C1 is in both"),
        (lambda d: d["placement"]["order"].append("nowhere"), "block 'nowhere'"),
        (lambda d: d.update(design="has spaces"), "design"),
        (lambda d: d.update(kicad_version="8"), "kicad_version"),
    ],
)
def test_a_malformed_design_fails_on_the_field_at_fault(change: Any, says: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Design.model_validate(broken(change))
    assert says in str(caught.value)


def test_a_bare_number_is_read_only_where_the_caller_allows_it() -> None:
    assert parse_quantity("1k", "ohm", bare=True) == Quantity(1000.0, "ohm")
    with pytest.raises(QuantityError, match="expected ohm"):
        parse_quantity("1k", "ohm")
    with pytest.raises(QuantityError):
        parse_quantity("40", "degC", bare=True)


def test_a_pin_name_may_hold_dots_but_must_have_a_part() -> None:
    assert split_pin("U1.VO") == ("U1", "VO")
    assert split_pin("U3.IO.1") == ("U3", "IO.1")
    with pytest.raises(ValueError, match=r"REF\.PIN"):
        split_pin(".VO")


@pytest.mark.parametrize("source", ["small", "plan"])
def test_a_design_prints_back_as_it_was_written(source: str) -> None:
    data = ldo() if source == "small" else json.loads(EXAMPLE.read_text("utf-8"))
    design = Design.model_validate(data)
    again = Design.model_validate(design.model_dump(mode="json", exclude_defaults=True))
    assert again == design


def test_a_length_is_in_millimetres_and_takes_no_prefix() -> None:
    assert parse_quantity("0.25mm", "mm") == Quantity(0.25, "mm")
    assert parse_quantity("35 mm") == Quantity(35.0, "mm")
    # A bare number is never taken to be a length.
    with pytest.raises(QuantityError, match="expected mm"):
        parse_quantity("0.25", "mm", bare=True)
    # 1m stays a thousandth, as it always was.
    assert parse_quantity("1m", "", bare=True) == Quantity(0.001, "")
    assert parse_quantity(format_quantity(Quantity(0.035, "mm")), "mm").value == 0.035


@pytest.mark.parametrize(
    ("text", "unit", "value"),
    [
        # IEC 60062's marking, as real boards write values: the pedalboard
        # fixture's R14 is "20K" and its R22 "2K2".
        ("20K", "ohm", 20e3),
        ("2K2", "ohm", 2200.0),
        ("4k7", "ohm", 4700.0),
        ("4R7", "ohm", 4.7),
        ("R47", "ohm", 0.47),
        ("100R", "ohm", 100.0),
        ("1M5", "ohm", 1.5e6),
        ("4n7", "F", 4.7e-9),
        ("1u5", "F", 1.5e-6),
        ("4u7", "H", 4.7e-6),
        # Ordinary prefixed numbers are read as they always were.
        ("1M", "ohm", 1e6),
        ("100n", "F", 100e-9),
    ],
)
def test_a_component_value_marked_with_a_letter_for_the_point_is_read(
    text: str, unit: str, value: float
) -> None:
    read = parse_quantity(text, unit, bare=True)
    assert read.unit == unit and read.value == pytest.approx(value)


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        ("20K", "F"),  # K is no capacitor's letter
        ("K", "ohm"),  # a letter alone is no value
        ("4m7", "ohm"),  # milli or a slip for mega: not read
        ("2K2", "V"),  # the marking is for component values only
    ],
)
def test_the_letter_marking_is_not_stretched(text: str, unit: str) -> None:
    with pytest.raises(QuantityError):
        parse_quantity(text, unit, bare=True)
    # And never where a bare number is not allowed.
    with pytest.raises(QuantityError):
        parse_quantity("2K2", "ohm")
