"""The regulator example, written by Kairo and judged by KiCad.

The design is grounded in KiCad's installed libraries, written as a
project, and then KiCad itself says whether it is right: its netlist must
hold exactly the design's connections, its rule check must be clean, and
saving the schematic again with KiCad must change nothing but the lines
that say which program wrote it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kairo.build import build_project
from kairo.ir import Design
from kairo.kicad.cli import ErcStatus, KicadCli
from kairo.kicad.emit import lay_out
from kairo.kicad.netlist import parse_netlist
from kairo.kicad.sexpr import SList, parse
from kairo.kicad.transaction import TransactionError
from kairo.library import Library, LibraryIndex, install_variables, read_library_table
from kairo.library.tables import Kind
from kairo.process import run_process

pytestmark = pytest.mark.kicad

EXAMPLE = (
    Path(__file__).resolve().parent.parent.parent
    / "evals"
    / "reference_designs"
    / "ldo_3v3"
    / "design.json"
)
# The symbol and footprint libraries the examples use.
SYMBOLS = {
    "Connector",
    "Regulator_Linear",
    "Device",
    "power",
    "74xGxx",
    "Diode",
    "Transistor_BJT",
}
WRITER_LINES = ("generator", "generator_version", "embedded_fonts")
FOOTPRINTS = {
    "Connector_PinHeader_2.54mm",
    "Package_TO_SOT_SMD",
    "Capacitor_SMD",
    "Resistor_SMD",
    "LED_SMD",
    "Diode_SMD",
}


@pytest.fixture(scope="module")
def index(kicad_cli_path: Path, kicad_demos: Path) -> LibraryIndex:
    share = kicad_demos.parent
    version = KicadCli((str(kicad_cli_path),)).version()
    assert version is not None
    variables = install_variables(share, version.major, {})
    libraries: list[Library] = []
    tables: tuple[tuple[Kind, str, set[str]], ...] = (
        ("symbol", "sym-lib-table", SYMBOLS),
        ("footprint", "fp-lib-table", FOOTPRINTS),
    )
    for kind, table, wanted in tables:
        found, problems = read_library_table(
            share / "template" / table, kind, variables
        )
        assert problems == []
        libraries += [library for library in found if library.nickname in wanted]
    assert len(libraries) == len(SYMBOLS) + len(FOOTPRINTS)
    index = LibraryIndex()
    assert not index.update(libraries).problems
    return index


@pytest.fixture
def design() -> Design:
    return Design.model_validate(json.loads(EXAMPLE.read_text(encoding="utf-8")))


def test_the_example_is_built_and_kicad_finds_it_right(
    design: Design, index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    cli = KicadCli((str(kicad_cli_path),))
    folder = tmp_path / "ldo_3v3"
    result = build_project(design, folder, index, cli)
    assert result.findings == ()
    assert result.passed
    assert result.erc is not None and result.erc.status is ErcStatus.CLEAN
    # Besides the project, Kairo's record of what it wrote and the same
    # design drawn with wires for a person.
    assert sorted(p.name for p in folder.iterdir()) == [
        "kairo",
        "ldo_3v3.kicad_pro",
        "ldo_3v3.kicad_sch",
        "wired",
    ]
    assert sorted(p.name for p in (folder / "kairo").iterdir()) == ["written.json"]
    assert sorted(p.name for p in (folder / "wired").iterdir()) == [
        "ldo_3v3.kicad_pro",
        "ldo_3v3.kicad_sch",
    ]
    assert {p.name for p in result.wired} == {"ldo_3v3.kicad_pro", "ldo_3v3.kicad_sch"}
    assert not any(f.code == "W_WIRED_NOT_WRITTEN" for f in result.findings)
    # It joins pins with wires where the labelled sheet has a label on each.
    wired_text = (folder / "wired" / "ldo_3v3.kicad_sch").read_text(encoding="utf-8")
    labelled = (folder / "ldo_3v3.kicad_sch").read_text(encoding="utf-8")
    assert wired_text.count("(label ") < labelled.count("(label ")
    wired_netlist = parse_netlist(
        cli.export_netlist(folder / "wired" / "ldo_3v3.kicad_sch").text or ""
    )
    assert {
        net.name: sorted(f"{n.ref}.{n.pin}" for n in net.nodes)
        for net in wired_netlist.nets
    } == {
        "VIN_5V": ["C1.1", "J1.1", "U1.3"],
        "+3V3": ["C2.1", "R1.1", "U1.2"],
        "/LED_A": ["D1.2", "R1.2"],
        "GND": ["C1.2", "C2.2", "D1.1", "J1.2", "U1.1"],
    }

    # KiCad's own netlist, read again here: every pin where the design put it.
    sheet = folder / "ldo_3v3.kicad_sch"
    exported = cli.export_netlist(sheet)
    assert exported.text is not None
    netlist = parse_netlist(exported.text)
    assert sorted(c.ref for c in netlist.components) == [
        "C1",
        "C2",
        "D1",
        "J1",
        "R1",
        "U1",
    ]
    assert {
        net.name: sorted(f"{n.ref}.{n.pin}" for n in net.nodes) for net in netlist.nets
    } == {
        "VIN_5V": ["C1.1", "J1.1", "U1.3"],
        "+3V3": ["C2.1", "R1.1", "U1.2"],
        "/LED_A": ["D1.2", "R1.2"],
        "GND": ["C1.2", "C2.2", "D1.1", "J1.2", "U1.1"],
    }
    values = {c.ref: (c.value, c.footprint) for c in netlist.components}
    assert values["C2"] == ("22uF", "Capacitor_SMD:C_0805_2012Metric")
    assert values["U1"] == ("AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2")

    # Building the same design again gives the same bytes.
    again = tmp_path / "again"
    assert build_project(design, again, index, cli).passed
    for name in ("ldo_3v3.kicad_sch", "ldo_3v3.kicad_pro"):
        assert (again / name).read_bytes() == (folder / name).read_bytes()

    # A project exactly as Kairo wrote it may be built again from a new
    # session (A189), but once a person has changed it, it is never
    # written over.
    assert build_project(design, folder, index, cli).passed
    sheet.write_text(sheet.read_text(encoding="utf-8") + "(edited)", encoding="utf-8")
    with pytest.raises(TransactionError, match="E_FILE_CHANGED"):
        build_project(design, folder, index, cli)


def test_symbols_that_extend_another_are_built_and_match_kicads_library(
    index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    # 1N4148W takes its drawing and simulation fields from 1N4001, and
    # BCX56 from Q_NPN_BCE. KiCad's rule check compares each embedded copy
    # with the library and calls it different if an inherited field is
    # missing.
    design = Design.model_validate(
        {
            "design": "derived",
            "kicad_version": "10",
            "nets": {"A": {"role": "signal"}, "B": {"role": "signal"}},
            "components": {
                "D1": {
                    "lib_id": "Diode:1N4148W",
                    "footprint": "Diode_SMD:D_SOD-123",
                },
                "Q1": {
                    "lib_id": "Transistor_BJT:BCX56",
                    "footprint": "Package_TO_SOT_SMD:SOT-89-3",
                },
            },
            "connections": {"A": ["D1.A", "Q1.E"], "B": ["D1.K", "Q1.C"]},
            "no_connect": ["Q1.B"],
        }
    )
    cli = KicadCli((str(kicad_cli_path),))
    result = build_project(design, tmp_path / "derived", index, cli)
    assert result.findings == ()
    assert result.erc is not None and result.erc.status is ErcStatus.CLEAN


def test_kicad_saves_the_schematic_back_unchanged(
    design: Design, index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    cli = KicadCli((str(kicad_cli_path),))
    folder = tmp_path / "ldo_3v3"
    assert build_project(design, folder, index, cli).passed
    sheet = folder / "ldo_3v3.kicad_sch"
    ours = sheet.read_bytes().decode("utf-8")
    saved = tmp_path / "saved"
    shutil.copytree(folder, saved)
    done = run_process(
        [str(kicad_cli_path), "sch", "upgrade", "--force", str(saved / sheet.name)],
        timeout_s=120,
    )
    assert done.returncode == 0, done.stderr
    theirs = (saved / sheet.name).read_bytes().decode("utf-8")
    assert theirs != ours

    # KiCad's command-line save names itself as the writer and leaves out
    # the embedded-fonts line that the KiCad program writes. With those two
    # things put the same, the files are equal byte for byte.
    def comparable(text: str) -> str:
        document = parse(text)
        root = document.root
        root.items = [
            item
            for item in root.items
            if not (isinstance(item, SList) and item.head in WRITER_LINES)
        ]
        lay_out(root, newline="\r\n" if "\r\n" in text else "\n")
        return document.to_text()

    assert comparable(theirs) == comparable(ours)
    assert ("\r\n" in theirs) == ("\r\n" in ours)


def test_a_wrong_pin_stops_the_build_before_anything_is_written(
    design: Design, index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    data = design.model_dump(mode="json", exclude_defaults=True)
    data["connections"]["VIN_5V"][1] = "U1.VIN"
    result = build_project(
        Design.model_validate(data),
        tmp_path / "bad",
        index,
        KicadCli((str(kicad_cli_path),)),
    )
    assert not result.passed
    (finding,) = result.findings
    assert finding.code == "E_PIN_UNKNOWN"
    assert "VI (3)" in finding.message
    assert not (tmp_path / "bad").exists()


def test_two_outputs_on_one_net_stop_the_build_at_the_structure_layer(
    design: Design, index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    data = design.model_dump(mode="json", exclude_defaults=True)
    data["components"]["U2"] = dict(data["components"]["U1"])
    data["connections"]["VIN_5V"].append("U2.VI")
    data["connections"]["+3V3"].append("U2.VO")
    data["connections"]["GND"].append("U2.GND")
    result = build_project(
        Design.model_validate(data),
        tmp_path / "fault",
        index,
        KicadCli((str(kicad_cli_path),)),
    )
    assert not result.passed
    (finding,) = [f for f in result.findings if f.severity == "error"]
    assert (finding.layer, finding.code) == ("L2", "E_OUTPUTS_SHORTED")
    assert (finding.where.ref, finding.where.pin, finding.where.net) == (
        "U1",
        "VO",
        "+3V3",
    )
    # Nothing was drawn or written, and KiCad was never asked.
    assert result.report.reached == ("L1", "L2")
    assert result.report.status("L3") == "not run"
    assert not (tmp_path / "fault").exists()


def test_a_fault_only_kicads_rule_check_sees_is_caught_there_and_nothing_kept(
    design: Design, index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    # A logic gate's output tied to the ground rail. To the structure layer
    # the net has one driver; KiCad's rule check also counts the power flag
    # on that rail and reports the conflict.
    data = design.model_dump(mode="json", exclude_defaults=True)
    data["components"]["U2"] = {"lib_id": "74xGxx:74LVC1G04"}
    data["connections"]["+3V3"].append("U2.VCC")
    data["connections"]["LED_A"].append("U2.2")
    data["connections"]["GND"] += ["U2.GND", "U2.4"]
    result = build_project(
        Design.model_validate(data),
        tmp_path / "fault",
        index,
        KicadCli((str(kicad_cli_path),)),
    )
    assert not result.passed
    assert result.files == ()
    assert result.report.reached == ("L0", "L1", "L2", "L3", "L4")
    assert [result.report.status(layer) for layer in ("L1", "L2", "L3", "L4")] == [
        "pass",
        "pass",
        "pass",
        "fail",
    ]
    assert any(f.code == "ERC_pin_to_pin" for f in result.findings)
    assert not list((tmp_path / "fault").glob("*.kicad_*"))
    assert 0 < result.report.score < 1


# Names a model plausibly writes from memory instead of looking the pin up.
WRONG_NAMES = (
    "VIN", "VOUT", "IN", "OUT", "INPUT", "OUTPUT", "ADJ", "EN", "VCC", "VDD",
    "VSS", "+", "-", "P", "N", "ANODE", "CATHODE", "PIN1", "Pin1", "A1", "0",
    "3", "4", "99", "TAB", "GND1", "PAD",
)  # fmt: skip


def test_every_seeded_wrong_pin_is_refused_and_no_right_one_is(
    design: Design, index: LibraryIndex
) -> None:
    from kairo.ir import split_pin
    from kairo.library.grounding import GroundingError, ground

    # No false rejection: the reference design grounds as it is.
    grounded = ground(design, index)
    assert len(grounded.pins) == 13

    data = design.model_dump(mode="json", exclude_defaults=True)
    seeded = refused = 0
    for net, pins in data["connections"].items():
        for position, written in enumerate(pins):
            ref, _ = split_pin(written)
            symbol = grounded.symbols[ref]
            real = {pin.name.lower() for pin in symbol.pins}
            real |= {alt.name.lower() for pin in symbol.pins for alt in pin.alternates}
            real |= {
                pin.number
                for pin in symbol.pins
                if pin.name in ("", "~") or pin.name.isdigit()
            }
            for wrong in WRONG_NAMES:
                if wrong.lower() in real:
                    continue
                seeded += 1
                changed = json.loads(json.dumps(data))
                changed["connections"][net][position] = f"{ref}.{wrong}"
                try:
                    ground(Design.model_validate(changed), index)
                except GroundingError as exc:
                    assert [f.code for f in exc.findings] == ["E_PIN_UNKNOWN"]
                    assert exc.findings[0].where.ref == ref
                    refused += 1
    assert seeded > 300
    assert refused == seeded


# Written for this test; not any maker's model. Four nodes, two of them
# ground, as TI's LM1117 model has.
TEST_MODEL = """* test regulator, not a maker's model
.subckt TESTREG IN OUT GND_A GND_B
R1 GND_A GND_B 1m
E1 mid GND_A VALUE { MIN(3.3, V(IN, GND_A)) }
R2 mid OUT 10m
.ends TESTREG
"""


def test_a_registered_model_is_named_in_kicads_own_simulation_fields(
    design: Design, index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    from kairo.spice.registry import ModelRegistry

    models = tmp_path / "models"
    models.mkdir()
    (models / "testreg.lib").write_text(TEST_MODEL, encoding="utf-8")
    registry = ModelRegistry(models)
    registry.register(
        {
            "lib_id": "Regulator_Linear:AMS1117-3.3",
            "file": "testreg.lib",
            "subckt": "TESTREG",
            "nodes": ["VI", "VO", "GND", "GND"],
            "dialect": "spice3",
            "engines": ["ngspice", "xyce"],
            "fidelity": "vendor",
            "manufacturer": "Kairo test data",
            "source": "tests/integration/test_build_ldo.py",
            "version": "1",
            "covers": "nothing; it is a test model",
        }
    )
    cli = KicadCli((str(kicad_cli_path),))
    folder = tmp_path / "ldo_3v3"
    result = build_project(design, folder, index, cli, models=registry)
    assert result.passed and result.findings == ()
    sheet = folder / "ldo_3v3.kicad_sch"

    # KiCad's own SPICE netlist reads the registered file and wires the
    # subcircuit's nodes in order, ground to both of the last two.
    exported = cli.export_netlist(sheet, fmt="spice")
    assert exported.text is not None
    lines = exported.text.splitlines()
    assert f'.include "{(models / "testreg.lib").resolve().as_posix()}"' in lines
    assert "XU1 VIN_5V +3V3 GND GND TESTREG" in lines

    # KiCad saves the fields back as Kairo wrote them.
    saved = tmp_path / "saved"
    shutil.copytree(folder, saved)
    done = run_process(
        [str(kicad_cli_path), "sch", "upgrade", "--force", str(saved / sheet.name)],
        timeout_s=120,
    )
    assert done.returncode == 0, done.stderr

    def without_writer(text: str) -> str:
        document = parse(text)
        root = document.root
        root.items = [
            item
            for item in root.items
            if not (isinstance(item, SList) and item.head in WRITER_LINES)
        ]
        lay_out(root, newline="\r\n" if "\r\n" in text else "\n")
        return document.to_text()

    ours = sheet.read_bytes().decode("utf-8")
    assert '(property "Sim.Pins" "3=1 2=2 1=3 1=4"' in ours
    theirs = (saved / sheet.name).read_bytes().decode("utf-8")
    assert without_writer(theirs) == without_writer(ours)


def test_a_design_on_several_sheets_is_one_project_kicad_finds_right(
    index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    # The example split three ways, its LED apart from its resistor, so
    # LED_A runs between sheets and must reach KiCad as one net.
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["blocks"] = {
        "power_in": ["J1", "C1"],
        "regulator": ["U1", "C2"],
        "resistor": ["R1"],
        "led": ["D1"],
    }
    data["placement"] = {"order": []}
    data["sheets"] = {
        "supply": {"title": "Input and regulator", "blocks": ["power_in", "regulator"]},
        "limit": {"title": "Current limit", "blocks": ["resistor"]},
        "light": {"title": "Indicator", "blocks": ["led"]},
    }
    data["title_block"] = {
        "company": "Kairo tests",
        "revision": "A",
        "date": "2026-10-23",
    }
    design = Design.model_validate(data)
    cli = KicadCli((str(kicad_cli_path),))
    folder = tmp_path / "ldo_sheets"
    result = build_project(design, folder, index, cli)
    assert result.passed, result.findings
    assert result.erc is not None and result.erc.status is ErcStatus.CLEAN
    assert sorted(p.name for p in folder.glob("*.kicad_sch")) == [
        "ldo_3v3.kicad_sch",
        "light.kicad_sch",
        "limit.kicad_sch",
        "supply.kicad_sch",
    ]
    netlist = parse_netlist(cli.export_netlist(folder / "ldo_3v3.kicad_sch").text or "")
    assert {
        net.name: sorted(f"{n.ref}.{n.pin}" for n in net.nodes) for net in netlist.nets
    } == {
        "VIN_5V": ["C1.1", "J1.1", "U1.3"],
        "+3V3": ["C2.1", "R1.1", "U1.2"],
        "/LED_A": ["D1.2", "R1.2"],
        "GND": ["C1.2", "C2.2", "D1.1", "J1.2", "U1.1"],
    }
    # The two sheets LED_A joins each end it in a hierarchical label, and
    # neighbouring on the root, are joined there by one wire.
    for name in ("limit", "light"):
        text = (folder / f"{name}.kicad_sch").read_text(encoding="utf-8")
        assert '(hierarchical_label "LED_A"' in text
        assert "(sheet_instances" not in text
    root = (folder / "ldo_3v3.kicad_sch").read_text(encoding="utf-8")
    assert root.count('(pin "LED_A"') == 2
    assert '(rev "A")' in root and '(company "Kairo tests")' in root
