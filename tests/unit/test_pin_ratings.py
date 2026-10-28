"""Pins against their absolute maximum ratings, and the nets' ranges they rest on.

The board is the front of a battery monitor, as on TI's BQ76952 boards:
the pack terminal P+ comes in through a connector and reaches the
monitor's PACK pin through 10 kohm, and the monitor's CHG output drives a
FET's gate. Worked by hand: P+ is stated 32.5 V to 54.6 V with surges to
97 V, so PACK follows it to 97 V, past an 85 V rating, unless a 78 V
Zener holds it; a 16 V gate Zener with a 1 V forward drop, 10% spread,
holds the gate from -1.1 V to 16 V of its source.
"""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from minilib import make_index
from pydantic import ValidationError

from kairo.defaults import net_spread
from kairo.ir import Design, Net
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.verify.requirements import RequirementResult, evaluate_requirements


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def monitor() -> dict[str, Any]:
    return {
        "design": "monitor",
        "policy": {"margins": "stated"},
        "nets": {
            "PACK_P": {
                "role": "power",
                "min": "32.5V",
                "max": "54.6V",
                "surge_max": "97V",
                "surge_min": "-2V",
            },
            "BATT": {"role": "power", "min": "32.5V", "max": "54.6V"},
            "PACK_SNS": {"role": "signal"},
            "GATE": {"role": "signal"},
            "MID": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        "components": {
            "J1": {"lib_id": "Connector:Conn_01x02"},
            "U1": {
                "lib_id": "Mon:MON",
                "source": "test data",
                "pin_ratings": {"PACK": {"max": "85V", "min": "-0.3V", "to": "VSS"}},
            },
            "R1": {"lib_id": "Device:R", "value": "10k"},
            "Q1": {
                "lib_id": "Mon:NMOS",
                "pin_ratings": {"G": {"max": "20V", "min": "-20V", "to": "S"}},
            },
        },
        "connections": {
            "PACK_P": ["J1.Pin_1", "R1.1"],
            "PACK_SNS": ["R1.2", "U1.PACK"],
            "BATT": ["U1.BAT", "Q1.S"],
            "GATE": ["U1.CHG", "Q1.G"],
            "MID": ["Q1.D"],
            "GND": ["J1.Pin_2", "U1.VSS"],
        },
        "no_connect": ["U1.TS"],
    }


def results(
    index: LibraryIndex, change: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, RequirementResult]:
    data = monitor()
    if change is not None:
        change(data)
    _, found = evaluate_requirements(ground(Design.model_validate(data), index))
    return {r.where.ref or "": r for r in found if r.type == "pin_ratings"}


def zener(clamp: str | None = "78V", **more: str) -> dict[str, Any]:
    params = {"v_clamp": clamp, **more} if clamp else dict(more)
    return {"lib_id": "Device:D_Zener", "params": params, "source": "test data"}


def test_a_pin_that_follows_a_surge_past_its_rating_fails(index: LibraryIndex) -> None:
    pack = results(index)["U1"]
    assert pack.status == "fail"
    assert "reaches 97 V" in pack.summary
    assert "follows PACK_P" in pack.summary
    assert "rated 85 V" in pack.summary


def test_a_zener_with_a_stated_clamp_holds_the_pin_within_it(
    index: LibraryIndex,
) -> None:
    def clamped(data: dict[str, Any]) -> None:
        data["components"]["D1"] = zener()
        data["connections"]["PACK_SNS"].append("D1.K")
        data["connections"]["GND"].append("D1.A")

    pack = results(index, clamped)["U1"]
    # The low side has no forward drop stated for D1, so it rests on the
    # surge P+ is stated to reach: -2 V, past the -0.3 V rating.
    assert "highest 78 V (held by D1's clamping voltage above GND)" in pack.summary
    assert pack.status == "fail"
    assert "reaches -2 V" in pack.summary
    assert "a clamp holds a net only at the current" in pack.summary.lower()


def test_a_zener_with_no_clamping_voltage_is_not_credited(
    index: LibraryIndex,
) -> None:
    def unstated(data: dict[str, Any]) -> None:
        data["components"]["D1"] = zener(clamp=None)
        data["connections"]["PACK_SNS"].append("D1.K")
        data["connections"]["GND"].append("D1.A")

    pack = results(index, unstated)["U1"]
    assert pack.status == "fail"
    assert "reaches 97 V" in pack.summary


def test_a_diode_from_kiCads_diode_library_is_a_clamp_too(index: LibraryIndex) -> None:
    # Found on the 13-cell BMS (revision C.5, 2026-10-19): a BAT54 drawn
    # with Diode:BAT54W beside PACK's Zener was not credited, because only
    # the Device library's diode symbols were known as clamps.
    def clamped(data: dict[str, Any]) -> None:
        data["components"]["D1"] = zener(v_f="0.9V", v_f_tolerance="0%")
        data["components"]["D2"] = {
            "lib_id": "Diode:BAT54W",
            "params": {"v_f": "0.25V", "v_f_tolerance": "0%"},
            "limits": {"v_f": "max"},
            "source": "test data",
        }
        data["connections"]["PACK_SNS"] += ["D1.K", "D2.K"]
        data["connections"]["GND"] += ["D1.A", "D2.A"]
        data["no_connect"].append("D2.NC")
        data["nets"]["PACK_P"]["surge_min"] = "-2V"

    pack = results(index, clamped)["U1"]
    assert "lowest -0.25 V (held by D2's forward voltage" in pack.summary
    assert pack.status == "pass"


def test_a_line_off_the_board_with_no_surge_stated_is_not_passed(
    index: LibraryIndex,
) -> None:
    def no_surge(data: dict[str, Any]) -> None:
        del data["nets"]["PACK_P"]["surge_max"]
        del data["nets"]["PACK_P"]["surge_min"]

    pack = results(index, no_surge)["U1"]
    assert pack.status == "unverified"
    assert "leaves the board through J1, no surge_max" in pack.summary
    assert pack.to_resolve


def test_a_divider_is_only_bounded_by_its_ends(index: LibraryIndex) -> None:
    # A second resistor to ground makes PACK_SNS a divider tap: it lies
    # between 0 V and 97 V but is not shown to reach 97 V.
    def divided(data: dict[str, Any]) -> None:
        data["components"]["R2"] = {"lib_id": "Device:R", "value": "1k"}
        data["connections"]["PACK_SNS"].append("R2.1")
        data["connections"]["GND"].append("R2.2")

    pack = results(index, divided)["U1"]
    assert pack.status == "unverified"
    assert "may reach 97 V" in pack.summary


def test_a_gate_driven_by_a_chip_needs_a_clamp_or_a_stated_range(
    index: LibraryIndex,
) -> None:
    gate = results(index)["Q1"]
    assert gate.status == "unverified"
    assert "U1.CHG (output) can drive GATE" in gate.summary


def test_a_gate_zener_to_the_source_holds_the_gate_source_voltage(
    index: LibraryIndex,
) -> None:
    def gate_zener(data: dict[str, Any]) -> None:
        data["components"]["D2"] = zener(clamp="16V", v_f="1V", v_f_tolerance="10%")
        data["connections"]["GATE"].append("D2.K")
        data["connections"]["BATT"].append("D2.A")

    gate = results(index, gate_zener)["Q1"]
    assert gate.status == "pass"
    assert "highest 16 V" in gate.summary
    assert "lowest -1.1 V" in gate.summary


def test_a_rating_on_a_pin_the_part_lacks_is_not_passed(index: LibraryIndex) -> None:
    def misnamed(data: dict[str, Any]) -> None:
        data["components"]["U1"]["pin_ratings"] = {"PAKC": {"max": "85V"}}

    pack = results(index, misnamed)["U1"]
    assert pack.status == "unverified"
    assert "no connected pin of U1 has that name" in pack.summary


def test_a_part_with_no_ratings_gets_no_pin_check(index: LibraryIndex) -> None:
    assert "R1" not in results(index)


def test_a_supply_given_as_a_range_has_its_midpoint_and_wider_side() -> None:
    battery = Net.model_validate({"role": "power", "min": "32.5V", "max": "54.6V"})
    assert battery.voltage is not None
    assert battery.voltage.value == pytest.approx(43.55)
    design = Design.model_validate(
        {
            "design": "pack",
            "nets": {
                "B": {"role": "power", "voltage": "48V", "min": "32.5V", "max": "54.6V"}
            },
            "components": {},
            "connections": {},
        }
    )
    spread = net_spread(design, "B")
    assert spread is not None
    # 48 V to 32.5 V is the wider side: 15.5 / 48.
    assert spread.value == pytest.approx(15.5 / 48 * 100)


@pytest.mark.parametrize(
    "net",
    [
        {"role": "power", "min": "5V"},
        {"role": "power", "min": "6V", "max": "5V"},
        {"role": "power", "min": "4V", "max": "6V", "tolerance": "5%"},
        {"role": "power", "voltage": "7V", "min": "4V", "max": "6V"},
        {"role": "ground", "surge_max": "1V"},
        {"role": "power", "voltage": "5V", "surge_max": "4V"},
    ],
)
def test_a_range_that_does_not_add_up_is_refused(net: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Net.model_validate(net)


def test_a_pin_rating_needs_a_limit() -> None:
    data = monitor()
    data["components"]["U1"]["pin_ratings"] = {"PACK": {"to": "VSS"}}
    with pytest.raises(ValidationError, match="max, its min, or both"):
        Design.model_validate(data)


def test_a_gate_zener_before_a_gate_resistor_holds_the_gate_at_rest(
    index: LibraryIndex,
) -> None:
    # As on the BMS board: the Zener sits on the drive node, and each FET's
    # gate is reached through its own small resistor.
    def gate_resistor(data: dict[str, Any]) -> None:
        data["components"]["D2"] = zener(clamp="16V", v_f="1V", v_f_tolerance="10%")
        data["components"]["R3"] = {"lib_id": "Device:R", "value": "4.7"}
        data["nets"]["Q1_G"] = {"role": "signal"}
        data["connections"]["GATE"] = ["U1.CHG", "D2.K", "R3.1"]
        data["connections"]["Q1_G"] = ["R3.2", "Q1.G"]
        data["connections"]["BATT"].append("D2.A")

    gate = results(index, gate_resistor)["Q1"]
    assert gate.status == "pass"
    assert "through R3 at rest" in gate.summary


def test_a_resistor_to_a_net_that_draws_current_is_not_followed(
    index: LibraryIndex,
) -> None:
    # The gate node also feeds a chip's output, so its far side may carry
    # current and the Zener on the drive node does not bound it.
    def driven(data: dict[str, Any]) -> None:
        data["components"]["D2"] = zener(clamp="16V", v_f="1V", v_f_tolerance="10%")
        data["components"]["R3"] = {"lib_id": "Device:R", "value": "4.7"}
        data["nets"]["Q1_G"] = {"role": "signal"}
        data["connections"]["GATE"] = ["D2.K", "R3.1"]
        data["connections"]["Q1_G"] = ["R3.2", "Q1.G", "U1.CHG"]
        data["connections"]["BATT"].append("D2.A")

    gate = results(index, driven)["Q1"]
    assert gate.status == "unverified"


def test_a_test_point_is_not_a_way_off_the_board(index: LibraryIndex) -> None:
    # KiCad's test point is Connector:TestPoint; the BMS board's on CHG's
    # drive net made that net read as one that leaves the board.
    def probed(data: dict[str, Any]) -> None:
        data["nets"]["GATE"] = {"role": "signal", "min": "32.5V", "max": "67.6V"}
        data["components"]["TP1"] = {"lib_id": "Connector:TestPoint"}
        data["connections"]["GATE"].append("TP1.1")
        data["components"]["U1"]["pin_ratings"]["CHG"] = {"max": "85V", "to": "VSS"}

    pack = results(index, probed)["U1"]
    assert "CHG on GATE, from VSS: highest 67.6 V" in pack.summary
