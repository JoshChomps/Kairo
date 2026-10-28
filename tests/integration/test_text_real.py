"""Text sizes and positions, checked against KiCad's own drawings.

KiCad's SVG export states each text's length and draws its strokes, so it
shows both how long KiCad thinks a text is and where its ink falls.
"""
# The calibration script is loaded from its file, so its types are unknown here.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from kairo.build import build_project
from kairo.ir import Design
from kairo.kicad.cli import KicadCli, RenderStatus
from kairo.kicad.field_placement import pin_text_boxes
from kairo.kicad.geometry import Placement
from kairo.kicad.sexpr import SList, parse
from kairo.library import Library, LibraryIndex, install_variables, read_library_table
from kairo.library.tables import Kind
from kairo.text_placement.kicad_text import (
    ADVANCE_21,
    DEFAULT_SIZE,
    LENGTH_CONSTANT,
    text_height,
    text_length,
)

pytestmark = pytest.mark.kicad

ROOT = Path(__file__).resolve().parent.parent.parent
REAL = ROOT / "tests" / "fixtures" / "real"
EXAMPLE = ROOT / "evals" / "reference_designs" / "ldo_3v3" / "design.json"
BOARDS = {
    "sanctuary-rpi-cm5io-mini-itx": "CM5IO.kicad_sch",
    "pedalboard-pedalboard-hw": "pedalboard-hw.kicad_sch",
    "greatscottgadgets-hackrf-pro": "praline.kicad_sch",
}
# KiCad's drawing gives a text's length, its size in points of 0.75 mm, the
# text, and then the strokes it is drawn with.
TEXT = re.compile(
    r'textLength="([\d.]+)" font-size="([\d.]+)"[^>]*>([^<]*)</text>\s*(?:</g>\s*)?'
    r'<g class="stroked-text"><desc>[^<]*</desc>(.*?)</g>',
    re.S,
)
NUMBER = re.compile(r"-?\d+\.?\d*")
ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}
#: How far ink may fall outside a predicted box, in mm. KiCad's default
#: stroke is 0.15 mm wide, so half of that lies outside any outline.
TOLERANCE = 0.1


def calibration_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "calibrate", ROOT / "evals" / "text_placement" / "calibrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drawn_texts(
    svg: str,
) -> list[tuple[str, float, float, tuple[float, float, float, float]]]:
    """Each drawn text: what it says, its stated length, its size, its ink box."""
    found: list[tuple[str, float, float, tuple[float, float, float, float]]] = []
    for length, points, text, strokes in TEXT.findall(svg):
        numbers = [float(n) for n in NUMBER.findall(strokes)]
        if not numbers:
            continue
        for entity, character in ENTITIES.items():
            text = text.replace(entity, character)
        xs, ys = numbers[0::2], numbers[1::2]
        found.append(
            (
                text,
                float(length),
                float(points) * 0.75,
                (min(xs), min(ys), max(xs), max(ys)),
            )
        )
    return found


def test_the_table_is_what_this_kicad_measures(kicad_cli_path: Path) -> None:
    module = calibration_module()
    table = module.calibrate(KicadCli((str(kicad_cli_path),)))
    assert table["constant"] == LENGTH_CONSTANT
    assert table["spread"] == 0.0
    measured = {c: round(a * 21) for c, a in table["advance"].items()}
    measured[" "] = round(table["space"] * 21)
    assert measured == ADVANCE_21
    for advance in table["advance"].values():
        assert abs(advance * 21 - round(advance * 21)) < 0.01


@pytest.mark.parametrize("board", sorted(BOARDS))
def test_lengths_agree_with_kicad_on_every_text_of_a_real_board(
    board: str, kicad_cli_path: Path, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(REAL / board / "tree", tree)
    rendered = KicadCli((str(kicad_cli_path),)).export_svg(
        tree / BOARDS[board], tmp_path / "svg"
    )
    assert rendered.status is RenderStatus.OK, rendered.reason
    compared = 0
    for path in rendered.files:
        for text, length, size, _ in drawn_texts(path.read_text(encoding="utf-8")):
            # The table is measured at KiCad's default size, which is the
            # size Kairo writes. Other sizes are a few percent off, because
            # KiCad's stroke width does not scale with them in step; and
            # markup and unmeasured characters are counted wide on purpose.
            if abs(size - DEFAULT_SIZE) > 0.002:
                continue
            if any(c not in ADVANCE_21 for c in text) or re.search(r"[~_^$]\{", text):
                continue
            compared += 1
            # KiCad prints four decimals.
            assert text_length(text, size) == pytest.approx(length, abs=0.001), text
    # Measured on KiCad 10.0.6: 5,139 such texts on the HackRF Pro alone,
    # every one exact.
    assert compared > 1500


@pytest.fixture(scope="module")
def index(kicad_cli_path: Path, kicad_demos: Path) -> LibraryIndex:
    share = kicad_demos.parent
    version = KicadCli((str(kicad_cli_path),)).version()
    assert version is not None
    variables = install_variables(share, version.major, {})
    wanted = {
        "Connector", "Regulator_Linear", "Device", "power",
        "Connector_PinHeader_2.54mm", "Package_TO_SOT_SMD", "Capacitor_SMD",
        "Resistor_SMD", "LED_SMD", "Sensor_Temperature", "Package_SO", "Diode_SMD",
        "Capacitor_Tantalum_SMD",
        "Connector_USB", "Amplifier_Operational", "MCU_Microchip_ATtiny",
        "Regulator_Switching", "Inductor_SMD", "Battery_Management", "Package_DFN_QFN",
    }  # fmt: skip
    libraries: list[Library] = []
    tables: tuple[tuple[Kind, str], ...] = (
        ("symbol", "sym-lib-table"),
        ("footprint", "fp-lib-table"),
    )
    for kind, table in tables:
        found, _ = read_library_table(share / "template" / table, kind, variables)
        libraries += [library for library in found if library.nickname in wanted]
    index = LibraryIndex()
    assert not index.update(libraries).problems
    return index


def test_every_placed_text_is_drawn_inside_the_box_it_was_given(
    index: LibraryIndex, kicad_cli_path: Path, tmp_path: Path
) -> None:
    cli = KicadCli((str(kicad_cli_path),))
    design = Design.model_validate(json.loads(EXAMPLE.read_text(encoding="utf-8")))
    folder = tmp_path / "ldo_3v3"
    assert build_project(design, folder, index, cli).passed
    sheet = folder / "ldo_3v3.kicad_sch"
    rendered = cli.export_svg(sheet, tmp_path / "svg")
    assert rendered.status is RenderStatus.OK
    drawn = drawn_texts(rendered.files[0].read_text(encoding="utf-8"))

    root = parse(sheet.read_bytes().decode("utf-8")).root
    checked = 0
    for symbol in root.lists("symbol"):
        for prop in symbol.lists("property"):
            name, text = prop.values()[:2]
            if name not in ("Reference", "Value") or prop.first("hide") is not None:
                continue
            at = prop.first("at")
            assert isinstance(at, SList)
            x, y, angle = (float(v) for v in at.values())
            length, height = text_length(text), text_height(text)
            half_x, half_y = (
                (length / 2, height / 2) if angle == 0 else (height / 2, length / 2)
            )
            box = (x - half_x, y - half_y, x + half_x, y + half_y)
            # The drawing of this text is the one of the same words whose
            # ink is nearest the field's anchor.
            same = [d for d in drawn if d[0] == text]
            assert same, text
            _, stated, size, ink = min(
                same,
                key=lambda d: (
                    abs((d[3][0] + d[3][2]) / 2 - x) + abs((d[3][1] + d[3][3]) / 2 - y)
                ),
            )
            assert size == pytest.approx(DEFAULT_SIZE, abs=0.001)
            assert stated == pytest.approx(length, abs=0.001)
            assert ink[0] >= box[0] - TOLERANCE and ink[2] <= box[2] + TOLERANCE, text
            assert ink[1] >= box[1] - TOLERANCE and ink[3] <= box[3] + TOLERANCE, text
            checked += 1
    assert checked == 22


def test_no_reference_design_has_text_overlapping_or_left_out(
    index: LibraryIndex,
) -> None:
    from kairo.kicad.schematic_writer import write_schematic
    from kairo.library.grounding import ground

    seen = 0
    for folder in sorted((ROOT / "evals" / "reference_designs").iterdir()):
        design = Design.model_validate_json(
            (folder / "design.json").read_text(encoding="utf-8")
        )
        written = write_schematic(ground(design, index), index)
        score = written.text_score
        assert (score.overlapping, score.ambiguous, score.unplaced) == (0, 0, 0), (
            folder.name,
            [f.message for f in written.findings],
        )
        seen += 1
    # The sensor design has a ground symbol in the middle of a four-pin
    # header, which had no room for its name until its lead was lengthened.
    assert seen >= 11


def test_no_reference_designs_text_covers_a_no_connect_cross(
    index: LibraryIndex,
) -> None:
    # Read from the written file, not from the writer's own record: the
    # record once left the crosses out, so text placed just outside a
    # chip's body covered them (the ATtiny's value over two of its pins).
    from kairo.kicad.schematic_writer import write_schematic
    from kairo.library.grounding import ground

    crossed = 0
    for folder in sorted((ROOT / "evals" / "reference_designs").iterdir()):
        design = Design.model_validate_json(
            (folder / "design.json").read_text(encoding="utf-8")
        )
        root = parse(write_schematic(ground(design, index), index).text).root
        marks = [
            tuple(float(v) for v in at.values()[:2])
            for mark in root.lists("no_connect")
            if isinstance(at := mark.first("at"), SList)
        ]
        crossed += len(marks)
        for symbol in root.lists("symbol"):
            for prop in symbol.lists("property"):
                name, text = prop.values()[:2]
                if name not in ("Reference", "Value") or prop.first("hide"):
                    continue
                at = prop.first("at")
                assert isinstance(at, SList)
                x, y, angle = (float(v) for v in at.values())
                length, height = text_length(text), text_height(text)
                half_x, half_y = (
                    (length / 2, height / 2) if angle == 0 else (height / 2, length / 2)
                )
                for mx, my in marks:
                    apart = abs(mx - x) > half_x + 0.5 or abs(my - y) > half_y + 0.5
                    assert apart, (folder.name, text, (mx, my))
    assert crossed > 0


#: Real boards whose every sheet is drawn to check pin texts; between them
#: they have pins with names inside and outside the body, two text sizes,
#: and turned and mirrored symbols.
PIN_BOARDS = ("pedalboard-pedalboard-hw", "antmicro-jetson-nano-baseboard")


def placement_of(symbol: SList) -> Placement:
    at = symbol.first("at")
    assert isinstance(at, SList)
    x, y, angle = (float(v) for v in at.values()[:3])
    mirror = symbol.first("mirror")
    flip = mirror.values()[0] if mirror is not None else ""
    return Placement(x, y, int(angle) % 360, flip)  # pyright: ignore[reportArgumentType]


def pin_texts(root: SList) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Every pin text Kairo expects on a sheet, with its box."""
    libraries = {
        node.values()[0]: node
        for node in (root.first("lib_symbols") or SList()).lists("symbol")
    }
    found: list[tuple[str, tuple[float, float, float, float]]] = []
    for symbol in root.lists("symbol"):
        lib_id = symbol.first("lib_id")
        unit = symbol.first("unit")
        assert lib_id is not None and unit is not None
        style = symbol.first("body_style")
        definition = libraries[lib_id.values()[0]]
        if definition.first("power") is not None:
            continue
        for text, box in pin_text_boxes(
            definition,
            placement_of(symbol),
            int(unit.values()[0]),
            int(style.values()[0]) if style is not None else 1,
        ):
            found.append((text, (box.left, box.top, box.right, box.bottom)))
    return found


@pytest.mark.parametrize("board", PIN_BOARDS)
def test_pin_texts_are_drawn_inside_the_boxes_kairo_gives_them(
    board: str, kicad_cli_path: Path, tmp_path: Path
) -> None:
    cli = KicadCli((str(kicad_cli_path),))
    tree = tmp_path / "tree"
    shutil.copytree(REAL / board / "tree", tree)
    checked = 0
    for number, sheet in enumerate(sorted(tree.rglob("*.kicad_sch"))):
        # Each sheet is drawn on its own, so its file and its drawing match.
        rendered = cli.export_svg(sheet, tmp_path / f"svg{number}")
        assert rendered.status is RenderStatus.OK, rendered.reason
        drawn = drawn_texts(rendered.files[0].read_text(encoding="utf-8"))
        for text, box in pin_texts(parse(sheet.read_bytes().decode("utf-8")).root):
            # The drawing of a pin text is the one of the same words whose
            # ink is nearest the box; a text KiCad did not draw is skipped
            # by looking no further than a few millimetres.
            near = [
                ink
                for said, _, _, ink in drawn
                if said == text
                and abs((ink[0] + ink[2]) - (box[0] + box[2])) / 2
                + abs((ink[1] + ink[3]) - (box[1] + box[3])) / 2
                < 3
            ]
            if not near:
                continue
            ink = min(
                near,
                key=lambda i: (
                    abs(i[0] + i[2] - box[0] - box[2])
                    + abs(i[1] + i[3] - box[1] - box[3])
                ),
            )
            assert ink[0] >= box[0] - TOLERANCE and ink[2] <= box[2] + TOLERANCE, (
                sheet.name, text, box, ink)  # fmt: skip
            assert ink[1] >= box[1] - TOLERANCE and ink[3] <= box[3] + TOLERANCE, (
                sheet.name, text, box, ink)  # fmt: skip
            checked += 1
    assert checked > 300


def test_no_reference_designs_text_covers_a_pin_text(index: LibraryIndex) -> None:
    # Read from the written file: the writer's own record once left pin
    # numbers out, so a connector's reference was placed over its pin 1.
    from kairo.kicad.schematic_writer import write_schematic
    from kairo.library.grounding import ground

    seen = 0
    for folder in sorted((ROOT / "evals" / "reference_designs").iterdir()):
        design = Design.model_validate_json(
            (folder / "design.json").read_text(encoding="utf-8")
        )
        root = parse(write_schematic(ground(design, index), index).text).root
        pins = pin_texts(root)
        seen += len(pins)
        for symbol in root.lists("symbol"):
            for prop in symbol.lists("property"):
                name, text = prop.values()[:2]
                if name not in ("Reference", "Value") or prop.first("hide"):
                    continue
                at = prop.first("at")
                assert isinstance(at, SList)
                x, y, angle = (float(v) for v in at.values())
                length, height = text_length(text), text_height(text)
                half_x, half_y = (
                    (length / 2, height / 2) if angle == 0 else (height / 2, length / 2)
                )
                for pin, box in pins:
                    apart = (
                        x + half_x <= box[0]
                        or box[2] <= x - half_x
                        or y + half_y <= box[1]
                        or box[3] <= y - half_y
                    )
                    assert apart, (folder.name, text, pin)
    assert seen > 0
