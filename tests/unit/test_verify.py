# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from minilib import ldo, make_index

from kairo.build import build_project
from kairo.ir import Design
from kairo.kicad.cli import KicadCli
from kairo.kicad.geometry import Box
from kairo.kicad.schematic_writer import write_schematic
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.verify.findings import Finding
from kairo.verify.layout import Drawing, DrawnText, check_layout, drawing_of
from kairo.verify.report import LAYERS, make_report
from kairo.verify.structure import StructureRules, check_structure

TESTS = Path(__file__).resolve().parent.parent
FAKE = TESTS / "fakes" / "fake_kicad_cli.py"


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def structure(index: LibraryIndex, change: Any, **rules: Any) -> list[Finding]:
    data = ldo()
    change(data)
    return check_structure(
        ground(Design.model_validate(data), index), StructureRules(**rules)
    )


def test_the_reference_design_has_no_structure_findings(index: LibraryIndex) -> None:
    assert structure(index, lambda d: None) == []


def add_gate(d: dict[str, Any]) -> None:
    d["components"]["U2"] = {"lib_id": "Device:GATE"}
    d["connections"]["+3V3"].append("U2.VCC")
    d["connections"]["GND"].append("U2.GND")
    d["connections"]["LED_A"].append("U2.4")
    d["nets"]["IN"] = {"role": "signal"}
    d["connections"]["IN"] = ["U2.2", "J1.Pin_2"]
    d["connections"]["GND"].remove("J1.Pin_2")


@pytest.mark.parametrize(
    ("change", "code", "where"),
    [
        (
            lambda d: d["connections"]["VIN_5V"].remove("U1.VI"),
            "E_POWER_PIN_UNCONNECTED",
            ("U1", "VI", None),
        ),
        (
            lambda d: (
                d["connections"]["VIN_5V"].remove("U1.VI"),
                d["connections"]["LED_A"].append("U1.VI"),
            ),
            "E_POWER_PIN_ON_SIGNAL_NET",
            ("U1", "VI", "LED_A"),
        ),
        (
            lambda d: (
                d["components"].update(U2=dict(d["components"]["U1"])),
                d["connections"]["VIN_5V"].append("U2.VI"),
                d["connections"]["+3V3"].append("U2.VO"),
                d["connections"]["GND"].append("U2.GND"),
            ),
            "E_OUTPUTS_SHORTED",
            ("U1", "VO", "+3V3"),
        ),
        (
            lambda d: (
                add_gate(d),
                d["connections"]["IN"].remove("U2.2"),
                d["connections"]["IN"].append("C1.2"),
                d["connections"]["GND"].remove("C1.2"),
            ),
            "E_INPUT_FLOATING",
            ("U2", "2", None),
        ),
        (
            lambda d: d["connections"]["LED_A"].remove("D1.A"),
            "E_NET_SINGLE_PIN",
            ("R1", "1" if False else "2", "LED_A"),
        ),
        (
            lambda d: d["connections"]["LED_A"].remove("D1.A"),
            "E_PIN_UNCONNECTED",
            ("D1", "A", None),
        ),
        (
            lambda d: d.update(no_connect=["D1.A"]),
            "E_NO_CONNECT_CONNECTED",
            ("D1", "A", "LED_A"),
        ),
    ],
)
def test_each_seeded_structure_fault_is_found_at_l2_with_its_place(
    index: LibraryIndex, change: Any, code: str, where: tuple[str, str, str | None]
) -> None:
    found = [f for f in structure(index, change) if f.code == code]
    assert len(found) == 1, [f.code for f in structure(index, change)]
    finding = found[0]
    assert (finding.layer, finding.severity) == ("L2", "error")
    assert (finding.where.ref, finding.where.pin, finding.where.net) == where
    assert finding.fix_hint


def test_a_pin_left_open_on_purpose_is_not_a_fault(index: LibraryIndex) -> None:
    def change(d: dict[str, Any]) -> None:
        d["connections"]["LED_A"].remove("D1.A")
        d["connections"]["LED_A"].remove("R1.2")
        d["no_connect"] = ["D1.A", "R1.2"]

    codes = [f.code for f in structure(index, change)]
    assert codes == ["W_NET_EMPTY"]
    # A pin the library types as not connected needs no listing either.
    assert structure(index, add_gate) == []


def test_a_supply_pin_with_no_capacitor_to_ground_is_a_warning(
    index: LibraryIndex,
) -> None:
    def no_input_capacitor(d: dict[str, Any]) -> None:
        del d["components"]["C1"]
        d["connections"]["VIN_5V"].remove("C1.1")
        d["connections"]["GND"].remove("C1.2")
        d["blocks"]["power_in"].remove("C1")

    (finding,) = structure(index, no_input_capacitor)
    assert (finding.code, finding.severity) == ("W_DECOUPLING_MISSING", "warning")
    assert (finding.where.ref, finding.where.pin, finding.where.net) == (
        "U1",
        "VI",
        "VIN_5V",
    )
    # The rule can be switched off.
    assert structure(index, no_input_capacitor, decoupling=False) == []


# The drawing


def drawing(**changes: Any) -> Drawing:
    facts: dict[str, Any] = {
        "sheet": Box(10, 10, 200, 150),
        "bodies": {
            "R1": Box(50, 50, 55, 60),
            "R2": Box(80, 50, 85, 60),
            "p1": Box(50, 70, 53, 73),
        },
        "parts": ["R1", "R2"],
        "wires": [Box(52.5, 60, 52.5, 70)],
        "texts": [
            DrawnText("R1", "Reference", "R1", Box(56, 52, 60, 54), True),
            DrawnText("R2", "Reference", "R2", Box(86, 52, 90, 54), True),
            DrawnText("NET", "label", "NET", Box(52.5, 68, 58, 70), False),
        ],
    }
    facts.update(changes)
    return drawing_of(**facts)


def test_a_sound_drawing_has_no_findings() -> None:
    assert check_layout(drawing()) == []


@pytest.mark.parametrize(
    ("changes", "code", "says"),
    [
        (
            {
                "bodies": {
                    "R1": Box(50, 50, 55, 60),
                    "R2": Box(54, 55, 59, 65),
                    "p1": Box(50, 70, 53, 73),
                }
            },
            "E_SYMBOLS_OVERLAP",
            "R1 and R2",
        ),
        (
            {
                "bodies": {
                    "R1": Box(50, 50, 55, 60),
                    "R2": Box(198, 50, 203, 60),
                    "p1": Box(50, 70, 53, 73),
                }
            },
            "E_OFF_SHEET",
            "R2 is drawn partly outside",
        ),
        (
            {
                "texts": [
                    DrawnText("R1", "Reference", "R1", Box(56, 52, 60, 54), True),
                    DrawnText("R1", "Value", "10k", Box(58, 53, 62, 55), True),
                ]
            },
            "E_TEXT_OVERLAP",
            "the reference 'R1' of R1 is drawn over the value '10k' of R1",
        ),
        (
            {"texts": [DrawnText("R1", "Reference", "R1", Box(53, 55, 57, 57), True)]},
            "E_TEXT_OVERLAP",
            "drawn over R1",
        ),
        (
            {"texts": [DrawnText("R1", "Reference", "R1", Box(51, 62, 55, 64), True)]},
            "E_TEXT_OVERLAP",
            "drawn over a wire",
        ),
        (
            {"texts": [DrawnText("R1", "Reference", "R1", Box(74, 52, 78, 54), True)]},
            "E_TEXT_AMBIGUOUS",
            "nearer R2 than R1",
        ),
        (
            {"texts": [DrawnText("R1", "Value", "10k", Box(5, 52, 9, 54), True)]},
            "E_OFF_SHEET",
            "the value '10k' of R1 is off the sheet",
        ),
        (
            {"pin_texts": [DrawnText("R1", "pin", "1", Box(57, 53, 58, 55), True)]},
            "E_TEXT_OVERLAP",
            "the reference 'R1' of R1 is drawn over the pin text '1' of R1",
        ),
    ],
)
def test_each_seeded_drawing_fault_is_found(
    changes: dict[str, Any], code: str, says: str
) -> None:
    findings = [f for f in check_layout(drawing(**changes)) if f.code == code]
    assert findings, [f.message for f in check_layout(drawing(**changes))]
    assert all(f.layer == "L2" and f.severity == "error" for f in findings)
    assert any(says in f.message for f in findings)


def test_the_writers_own_drawing_passes_its_checks(index: LibraryIndex) -> None:
    written = write_schematic(ground(Design.model_validate(ldo()), index), index)
    assert check_layout(written.drawing) == []
    assert len(written.drawing.texts) == 22 + 5
    # The connector shows its pin numbers, which the check must see.
    assert {pin.text for pin in written.drawing.pin_texts if pin.owner == "J1"} == {
        "1",
        "2",
    }
    assert written.drawing.parts == frozenset({"J1", "U1", "C1", "C2", "R1", "D1"})


# The report


def finding(layer: str, severity: str = "error", code: str = "E_X") -> Finding:
    return Finding(layer, code, severity, "something")  # pyright: ignore[reportArgumentType]


def test_a_report_passes_only_when_every_layer_ran_clean() -> None:
    clean = make_report([], list(LAYERS))
    assert clean.passed and clean.score == 1.0
    assert all(clean.status(layer) == "pass" for layer in LAYERS)
    warned = make_report([finding("L2", "warning")], list(LAYERS))
    assert warned.passed
    stopped = make_report([finding("L2")], ["L1", "L2"])
    assert not stopped.passed
    assert [stopped.status(layer) for layer in LAYERS] == [
        "not run",
        "pass",
        "fail",
        "not run",
        "not run",
    ]
    # A clean run that never reached KiCad is not a pass.
    assert not make_report([], ["L1", "L2"]).passed
    unavailable = make_report(
        [finding("L3", code="E_NETLIST_UNAVAILABLE")], ["L0", "L1", "L2", "L3"]
    )
    assert unavailable.status("L3") == "unavailable" and not unavailable.passed


def test_the_score_rises_as_errors_fall_and_is_never_the_verdict() -> None:
    many = make_report([finding("L2")] * 4, ["L1", "L2"])
    fewer = make_report([finding("L2")], ["L1", "L2"])
    assert 0 < many.score < fewer.score < 1
    assert many.as_dict()["passed"] is False
    assert "verdict" in many.as_dict()["score_note"]


def test_findings_are_ordered_by_layer_and_paged() -> None:
    findings = [finding("L4"), finding("L2", "warning"), finding("L2"), finding("L1")]
    report = make_report(findings + [finding("L4")] * 30, list(LAYERS))
    first = report.as_dict()
    assert [f["layer"] for f in first["findings"][:3]] == ["L1", "L2", "L2"]
    assert [f["severity"] for f in first["findings"][1:3]] == ["error", "warning"]
    assert (
        first["findings_total"],
        first["findings_pages"],
        len(first["findings"]),
    ) == (34, 2, 20)
    assert first["layers"]["L4"] == {"status": "fail", "errors": 31, "warnings": 0}
    assert len(report.as_dict(2)["findings"]) == 14
    with pytest.raises(ValueError, match="pages 1 to 2"):
        report.as_dict(3)


# The build, with KiCad stood in for


def test_a_structure_fault_stops_the_build_before_anything_is_drawn(
    index: LibraryIndex, tmp_path: Path
) -> None:
    data = ldo()
    data["connections"]["VIN_5V"].remove("U1.VI")
    cli = KicadCli((sys.executable, str(FAKE), "tools"), temp_root=tmp_path)
    result = build_project(Design.model_validate(data), tmp_path / "p", index, cli)
    assert not result.passed
    assert result.report.reached == ("L1", "L2")
    assert [f.code for f in result.findings] == ["E_POWER_PIN_UNCONNECTED"]
    assert not (tmp_path / "p").exists()
