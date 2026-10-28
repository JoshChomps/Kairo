"""Adding, removing and reconnecting parts in a real, human-drawn board.

The board is the pedalboard fixture, saved by KiCad 10. KiCad is the
judge throughout: after each change its netlist must hold exactly the
connections asked for and its rule check nothing it did not have before.
"""

from __future__ import annotations

import asyncio
import difflib
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from kairo.design_tools import DesignTools
from kairo.kicad.cli import KicadCli
from kairo.kicad.drawn_edit import Project, add_part, reconnect, remove_part
from kairo.kicad.edit import EditError, set_value
from kairo.kicad.netlist import parse_netlist
from kairo.kicad.sexpr import to_text
from kairo.library import LibraryIndex
from kairo.server import Settings, Workspace, build_server

pytestmark = pytest.mark.kicad

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
BOARD = REAL / "pedalboard-pedalboard-hw" / "tree"
OLDER = REAL / "git4dcc-rtb-c13" / "tree"
ROOT = "pedalboard-hw.kicad_sch"
RESISTOR = {
    "lib_id": "Device:R",
    "value": "4k7",
    "footprint": "Resistor_SMD:R_0603_1608Metric",
}


@pytest.fixture
def board(tmp_path: Path) -> Path:
    copy = tmp_path / "pedalboard"
    shutil.copytree(BOARD, copy)
    return copy


@pytest.fixture(scope="module")
def cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # One cache for the module, so the libraries are indexed once.
    return tmp_path_factory.mktemp("cache")


@pytest.fixture
def workspace(board: Path, cache: Path, kicad_cli_path: Path) -> Workspace:
    return Workspace(Settings((board,), cache, kicad_cli_path, tier="write"))


@pytest.fixture
def index(workspace: Workspace) -> LibraryIndex:
    return workspace._library()  # pyright: ignore[reportPrivateUsage]


def nets(board: Path, kicad_cli_path: Path) -> dict[str, set[tuple[str, str]]]:
    text = KicadCli((str(kicad_cli_path),)).export_netlist(board / ROOT).text
    assert text is not None
    return {
        net.name: {(node.ref, node.pin) for node in net.nodes}
        for net in parse_netlist(text).nets
    }


def drawn_items(project: Project, name: str) -> list[str]:
    """Everything on a sheet but the file's own copies of library symbols."""
    sheet = project.sheets[(project.folder / name).resolve()]
    return [
        to_text(node)
        for node in sheet.document.root.lists()
        if node.head != "lib_symbols"
    ]


def test_the_sheets_are_found_with_the_paths_kicad_gives_them(board: Path) -> None:
    project = Project(board / ROOT)
    names = {sheet.path.name: sheet.names for sheet in project.sheets.values()}
    assert names == {
        "pedalboard-hw.kicad_sch": ["/"],
        "psu.kicad_sch": ["/Power Supply/"],
        "leds.kicad_sch": ["/LEDs/"],
        "audio.kicad_sch": ["/Audio/"],
        "pico.kicad_sch": ["/MIDI/"],
        "connect.kicad_sch": ["/Connectivity/"],
    }
    # A symbol's instance path is the root's identifier, then the sheet box's.
    psu = project.sheets[(board / "psu.kicad_sch").resolve()]
    assert psu.instances == [
        "/df521c2a-9827-4355-b809-366f4b88d5d7/0d7c87b0-1309-475f-a9d7-edcf3dee5e4e"
    ]
    assert project.files() == {}


def test_a_part_is_found_by_this_projects_reference_only(board: Path) -> None:
    # Symbols on the power sheet still carry references from a project
    # they were copied from, one of which is C1; this project's C1 is on
    # the MIDI sheet. Reading every remembered reference once sent a
    # removal, and would have sent a change of value, to the wrong part.
    project = Project(board / ROOT)
    sheet = project.sheet_of("C1")
    assert sheet is not None and sheet.path.name == "pico.kicad_sch"
    edit = set_value(board / ROOT, "C1", "220n")
    assert list(edit.files) == ["pico.kicad_sch"] and edit.symbols == 1


def test_adding_then_removing_a_part_leaves_the_drawing_as_it_was(
    board: Path, index: LibraryIndex
) -> None:
    project = Project(board / ROOT)
    before = drawn_items(project, "audio.kicad_sch")
    added = add_part(
        project,
        index,
        ref="R900",
        pins={"1": "+3V3", "2": "/Audio/CM4_LED_ACT"},
        sheet="audio.kicad_sch",
        **RESISTOR,
    )
    assert added.file == "audio.kicad_sch"
    assert added.endings == {
        "1": ("+3V3", "power symbol"),
        "2": ("/Audio/CM4_LED_ACT", "label"),
    }
    # The project draws +3V3 with a power symbol, so the new pin gets one,
    # numbered after the last the project has.
    assert added.power == ("#PWR074",)
    after = drawn_items(project, "audio.kicad_sch")
    # Nothing that was there is touched: the old items are all still
    # there, in order, with five new ones among them (the part, two
    # wires, a label and a power symbol).
    assert [item for item in after if item in set(before)] == before
    assert len(after) == len(before) + 5
    assert list(project.files()) == ["audio.kicad_sch"]
    # The same request on a fresh copy gives the same file, identifiers too.
    again = Project(board / ROOT)
    add_part(
        again,
        index,
        ref="R900",
        pins={"1": "+3V3", "2": "/Audio/CM4_LED_ACT"},
        sheet="audio.kicad_sch",
        **RESISTOR,
    )
    assert again.files() == project.files()

    removed = remove_part(project, "R900")
    assert (removed.symbols, removed.wires, removed.shortened) == (1, 2, 0)
    assert removed.labels == ("CM4_LED_ACT",) and removed.power == ("#PWR074",)
    assert drawn_items(project, "audio.kicad_sch") == before


def test_what_cannot_be_drawn_is_refused_with_its_reason(
    board: Path, index: LibraryIndex, tmp_path: Path
) -> None:
    project = Project(board / ROOT)

    def add(**changes: Any) -> None:
        arguments: dict[str, Any] = {
            "ref": "R900",
            "pins": {"1": "+3V3", "2": "GND"},
            "sheet": "audio.kicad_sch",
            **RESISTOR,
            **changes,
        }
        add_part(project, index, **arguments)

    cases: dict[str, dict[str, Any]] = {
        "E_REF_EXISTS": {"ref": "R19"},
        "E_SHEET_NEEDED": {"sheet": None},
        "E_SHEET_UNKNOWN": {"sheet": "nowhere.kicad_sch"},
        "E_REF_NOT_FOUND": {"sheet": None, "beside": "R9999"},
        "E_SYMBOL_UNKNOWN": {"lib_id": "Device:NoSuchPart"},
        # An op amp has a + pin in each of units A and B.
        "E_PIN: .*units A, B .*numbers 3, 5": {
            "lib_id": "Amplifier_Operational:LM358",
            "pins": {"+": "GND"},
        },
        "E_PIN_UNCONNECTED": {"pins": {"1": "+3V3"}},
        "E_PIN_TWICE": {"no_connect": ["2"]},
        "E_PIN:": {"pins": {"1": "+3V3", "9": "GND"}},
        # A net KiCad named from a pin has no name a label could carry.
        "E_NET_UNNAMED": {"pins": {"1": "+3V3", "2": "Net-(D6-A)"}},
        # A net local to another sheet cannot be reached by a label here.
        "E_NET_NOT_ON_SHEET": {"pins": {"1": "+3V3", "2": "/MIDI/I2C_SDA"}},
    }
    for code, changes in cases.items():
        with pytest.raises(EditError, match=code):
            add(**changes)
    assert project.files() == {}
    with pytest.raises(EditError, match="E_REF_NOT_FOUND"):
        remove_part(project, "R9999")
    with pytest.raises(EditError, match="E_PIN"):
        reconnect(project, "R19.7", "+3V3")

    # A board saved by KiCad 9 is refused: only KiCad 10's format is drawn.
    older = tmp_path / "c13"
    shutil.copytree(OLDER, older)
    with pytest.raises(EditError, match="E_FORMAT_UNSUPPORTED"):
        add_part(
            Project(older / "C13.kicad_sch"),
            index,
            ref="R900",
            pins={"1": "+3V3", "2": "GND"},
            sheet="C13rcm.kicad_sch",
            **RESISTOR,
        )


def test_a_pull_up_is_swapped_and_kicad_confirms_it(
    board: Path, cache: Path, kicad_cli_path: Path
) -> None:
    settings = Settings((board,), cache, kicad_cli_path, tier="write")
    before = (board / "pico.kicad_sch").read_bytes()
    nets_before = nets(board, kicad_cli_path)
    operations = [
        {
            "op": "add_part",
            "ref": "R900",
            "pins": {"1": "+3V3", "2": "/MIDI/I2C_SDA"},
            "beside": "R19",
            **RESISTOR,
        },
        {"op": "remove_part", "ref": "R19"},
    ]

    async def talk() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(settings)) as client:
            for name, arguments in (
                ("edit_schematic", {"project": str(board), "operations": operations}),
                ("undo", {"folder": str(board)}),
            ):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, (name, result.content)
                out.append(result.structured_content)
                out.append((board / "pico.kicad_sch").read_bytes())
                out.append(nets(board, kicad_cli_path) if name != "undo" else None)
        return out

    changed, on_disk, nets_after, undone, restored, _ = asyncio.run(talk())
    assert changed["written"] is True and changed["problems"] == []
    assert [op["op"] for op in changed["operations"]] == ["add_part", "remove_part"]
    # R19 pulled SDA up to +3V3; now R900 does, and nothing else moved.
    assert nets_after["/MIDI/I2C_SDA"] == (
        nets_before["/MIDI/I2C_SDA"] - {("R19", "2")}
    ) | {("R900", "2")}
    assert nets_after["+3V3"] == (nets_before["+3V3"] - {("R19", "1")}) | {
        ("R900", "1")
    }
    others = set(nets_before) - {"/MIDI/I2C_SDA", "+3V3"}
    assert all(nets_after[name] == nets_before[name] for name in others)
    # Only one file changed, by a small share of its lines, and no line
    # that stayed was altered except the one wire cut back to its junction.
    (change,) = changed["files"]
    assert change["file"] == "pico.kicad_sch" and change["ratio"] < 0.03
    old_lines = before.decode().splitlines()
    new_lines = on_disk.decode().splitlines()
    replaced = [
        tag
        for tag, *_ in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False
        ).get_opcodes()
        if tag == "replace"
    ]
    assert len(replaced) <= 1
    assert undone["undone"] is True and restored == before


def test_parts_of_several_units_are_drawn_whole_and_kicad_confirms_them(
    workspace: Workspace, board: Path, index: LibraryIndex
) -> None:
    tools = DesignTools(workspace, None)
    kicad = Path(workspace.kicad().command[0])
    before = nets(board, kicad)
    operations: list[dict[str, Any]] = [
        # Unit A follows an existing net; unit B is the usual unused
        # amplifier, input to ground and output to its own inverting
        # input; unit C is the supply. Pins are named where the name is
        # one pin and numbered where it is in both amplifiers.
        {
            "op": "add_part",
            "ref": "U900",
            "lib_id": "Amplifier_Operational:LM358",
            "value": "LM358",
            "pins": {
                "3": "/LEDs/RGB_DATA",
                "1": "/LEDs/FOLLOW",
                "2": "/LEDs/FOLLOW",
                "5": "GND",
                "6": "/LEDs/SPARE",
                "7": "/LEDs/SPARE",
                "V+": "+3V3",
                "V-": "GND",
            },
            "sheet": "leds.kicad_sch",
        },
        # Four gates that share their supply pins: those are drawn on
        # every unit and joined on each.
        {
            "op": "add_part",
            "ref": "U901",
            "lib_id": "74xx_IEEE:7400",
            "value": "7400",
            "pins": {
                **{n: "GND" for n in ("1", "2", "4", "5", "9", "10", "12", "13")},
                "VCC": "+3V3",
                "GND": "GND",
            },
            "no_connect": ["3", "6", "8", "11"],
            "sheet": "leds.kicad_sch",
        },
    ]
    out = tools.edit_schematic(str(board), operations)
    assert out["written"] is True, out["problems"]
    added = {op["ref"]: op for op in out["operations"]}
    assert len(added["U900"]["units"]) == 3 and len(added["U901"]["units"]) == 4
    assert len(set(added["U901"]["units"])) == 4

    after = nets(board, kicad)
    assert after["/LEDs/FOLLOW"] == {("U900", "1"), ("U900", "2")}
    assert after["/LEDs/SPARE"] == {("U900", "6"), ("U900", "7")}
    assert after["/LEDs/RGB_DATA"] == before["/LEDs/RGB_DATA"] | {("U900", "3")}
    gate_inputs = {("U901", n) for n in ("1", "2", "4", "5", "9", "10", "12", "13")}
    assert (
        after["GND"]
        == before["GND"] | {("U900", "5"), ("U900", "4"), ("U901", "7")} | gate_inputs
    )
    assert after["+3V3"] == before["+3V3"] | {("U900", "8"), ("U901", "14")}
    others = set(before) - {"/LEDs/RGB_DATA", "GND", "+3V3"}
    assert all(after[name] == before[name] for name in others)

    # A pin is found by number though it has a name, and a refusal names
    # the units a name is found in.
    project = Project(board / ROOT)
    with pytest.raises(EditError, match="has a pin '-' in units A, B"):
        add_part(
            project,
            index,
            ref="U902",
            lib_id="Amplifier_Operational:LM358",
            value="LM358",
            footprint=None,
            pins={"-": "GND"},
            sheet="audio.kicad_sch",
        )
    with pytest.raises(EditError, match="E_PIN_UNCONNECTED: U902 has pins with no net"):
        add_part(
            project,
            index,
            ref="U902",
            lib_id="Amplifier_Operational:LM358",
            value="LM358",
            footprint=None,
            pins={"2": "GND", "+*": "GND"},
            sheet="audio.kicad_sch",
        )
    assert project.files() == {}


def test_a_part_of_several_units_is_removed_whole(
    board: Path, index: LibraryIndex
) -> None:
    project = Project(board / ROOT)
    before = drawn_items(project, "audio.kicad_sch")
    added = add_part(
        project,
        index,
        ref="U900",
        lib_id="Amplifier_Operational:LM358",
        value="LM358",
        footprint=None,
        pins={
            "+*": "GND",
            "-*": "GND",
            "1": "GND",
            "7": "GND",
            "V+": "+3V3",
            "V-": "GND",
        },
        sheet="audio.kicad_sch",
    )
    assert len(added.units) == 3 and added.at == added.units[0]
    removed = remove_part(project, "U900")
    assert removed.symbols == 3
    assert drawn_items(project, "audio.kicad_sch") == before


def test_a_pin_is_moved_to_another_net(workspace: Workspace, board: Path) -> None:
    tools = DesignTools(workspace, None)
    before = nets(board, Path(workspace.kicad().command[0]))
    out = tools.edit_schematic(
        str(board), [{"op": "reconnect", "pin": "R18.2", "net": "/MIDI/I2C_SDA"}]
    )
    assert out["written"] is True, out["problems"]
    after = nets(board, Path(workspace.kicad().command[0]))
    assert after["/MIDI/I2C_SCL"] == before["/MIDI/I2C_SCL"] - {("R18", "2")}
    assert after["/MIDI/I2C_SDA"] == before["/MIDI/I2C_SDA"] | {("R18", "2")}
    # A wire cut back, a new wire and a label: a few lines of a large file.
    (change,) = out["files"]
    assert change["changed_lines"] < 40


def test_a_change_that_leaves_a_pin_open_is_put_back(
    workspace: Workspace, board: Path
) -> None:
    tools = DesignTools(workspace, None)
    before = {path.name: path.read_bytes() for path in board.glob("*.kicad_sch")}
    # R16 feeds an LED: without it the LED's pin is joined to nothing,
    # which KiCad's rule check reports and the board did not have before.
    out = tools.edit_schematic(str(board), [{"op": "remove_part", "ref": "R16"}])
    assert out["written"] is False
    assert any("pin_not_connected (0 before, 1 after)" in p for p in out["problems"])
    assert {p.name: p.read_bytes() for p in board.glob("*.kicad_sch")} == before

    # A bound on how much of a file may change refuses before writing.
    out = tools.edit_schematic(
        str(board), [{"op": "remove_part", "ref": "C21"}], max_changed_ratio=0.001
    )
    assert out["written"] is False
    assert "over the bound of 0.1%" in out["problems"][0]
    assert {p.name: p.read_bytes() for p in board.glob("*.kicad_sch")} == before

    for operations, code in (
        ([], "E_OPERATION"),
        ([{"op": "rewire"}], "E_OPERATION"),
        ([{"op": "remove_part"}], "E_OPERATION"),
        ([{"op": "add_part", "ref": "R900", "pins": ["1"]}], "E_OPERATION"),
        ([{"op": "remove_part", "ref": "R9999"}], "E_REF_NOT_FOUND"),
    ):
        with pytest.raises(ToolError, match=code):
            tools.edit_schematic(str(board), operations)
    assert {p.name: p.read_bytes() for p in board.glob("*.kicad_sch")} == before
