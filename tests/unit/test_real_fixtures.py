"""The parser, format check, readers and ERC classifier on real open-hardware boards.

See ``tests/fixtures/real/README.md`` for where the boards come from. These
tests need no KiCad; the ones that run kicad-cli on the boards are in
``tests/integration/test_real_fixtures_kicad.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairo.hashing import tree_sha256
from kairo.kicad.cli import ErcStatus, classify_erc_report
from kairo.kicad.formats import BOARD, SCHEMATIC, UnsupportedFormatError, check_format
from kairo.kicad.project import parse_project
from kairo.kicad.sexpr import parse
from kairo.library import LibraryIndex, read_library_table, read_symbol_library

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"

# Saved by KiCad 8; taken from CopperBench.
KICAD_8 = [
    "antmicro-jetson-orin-baseboard",
    "antmicro-microphone-board",
    "openlighting-dmxsun-baseboard",
    "zhiayang-mikoto",
]
# Saved by the KiCad 9 or 10 program: the schematic format and how many
# sheet files each project has.
CURRENT: dict[str, tuple[int, int]] = {
    "antmicro-artix-dc-scm": (20250114, 11),
    "antmicro-com-express-7-baseboard": (20250114, 18),
    "antmicro-jetson-nano-baseboard": (20250114, 8),
    "antmicro-osm-l-bmc-som": (20250114, 31),
    "antmicro-polarfire-som": (20250114, 18),
    "antmicro-sodimm-ddr5-tester": (20250114, 17),
    "craigg96-vesc6-open-hardware": (20250114, 6),
    "git4dcc-rtb-c13": (20250114, 5),
    "greatscottgadgets-hackrf-pro": (20250114, 12),
    "lsartory-clavier": (20250114, 13),
    "pedalboard-pedalboard-hw": (20260306, 6),
    "sanctuary-rpi-cm5io-mini-itx": (20260306, 4),
}
BOARDS = sorted([*KICAD_8, *CURRENT])
SEXPR_SUFFIXES = {
    ".kicad_sch",
    ".kicad_sym",
    ".kicad_mod",
    ".kicad_pcb",
    ".kicad_wks",
    ".kicad_dru",
}
TABLES = {"sym-lib-table", "fp-lib-table"}


def files(board: str, *suffixes: str) -> list[Path]:
    return sorted(
        p
        for p in (REAL / board / "tree").rglob("*")
        if p.is_file() and (p.suffix in suffixes or p.name in suffixes)
    )


def test_every_board_is_accounted_for() -> None:
    assert sorted(p.name for p in REAL.iterdir() if p.is_dir()) == BOARDS
    for board in BOARDS:
        assert (REAL / board / "LICENSE").is_file()


@pytest.mark.parametrize("board", BOARDS)
def test_every_kicad_file_round_trips_byte_for_byte(board: str) -> None:
    paths = files(board, *SEXPR_SUFFIXES, *TABLES)
    assert paths
    for path in paths:
        data = path.read_bytes()
        assert parse(data.decode("utf-8")).to_text().encode("utf-8") == data, path


@pytest.mark.parametrize("board", BOARDS)
def test_the_project_file_round_trips_byte_for_byte(board: str) -> None:
    (path,) = files(board, ".kicad_pro")
    data = path.read_bytes()
    assert parse_project(data.decode("utf-8")).to_text().encode("utf-8") == data


@pytest.mark.parametrize("board", sorted(CURRENT))
def test_schematics_the_kicad_program_saved_are_in_a_format_kairo_reads(
    board: str,
) -> None:
    version, sheet_count = CURRENT[board]
    sheets = files(board, ".kicad_sch")
    assert len(sheets) == sheet_count
    for path in sheets:
        document = parse(path.read_bytes().decode("utf-8"))
        assert check_format(document, SCHEMATIC) == version, path
        generator = document.root.first("generator")
        assert generator is not None
        assert generator.values() == ["eeschema"], path


@pytest.mark.parametrize(
    ("board", "library", "symbols", "pins"),
    [
        ("greatscottgadgets-hackrf-pro", "praline.kicad_sym", 24, 418),
        ("sanctuary-rpi-cm5io-mini-itx", "CM5IO.kicad_sym", 18, 742),
    ],
)
def test_a_projects_own_symbol_library_is_read(
    board: str, library: str, symbols: int, pins: int
) -> None:
    (path,) = [p for p in files(board, ".kicad_sym") if p.name == library]
    found = read_symbol_library(parse(path.read_bytes().decode("utf-8")))
    assert len(found) == symbols
    assert sum(len(s.pins) for s in found if s.body == s.name) == pins
    numbers = [p.number for s in found if s.body == s.name for p in s.pins]
    assert all(isinstance(n, str) for n in numbers)


@pytest.mark.parametrize("board", KICAD_8)
def test_kicad_8_files_are_refused_by_name_not_guessed_at(board: str) -> None:
    for path in files(board, ".kicad_sch", ".kicad_pcb"):
        kind = SCHEMATIC if path.suffix == ".kicad_sch" else BOARD
        with pytest.raises(UnsupportedFormatError, match=r"written by KiCad 8\.0"):
            check_format(parse(path.read_bytes().decode("utf-8")), kind)


@pytest.mark.parametrize(
    ("board", "warnings"),
    [
        ("antmicro-jetson-orin-baseboard", 1751),
        ("antmicro-microphone-board", 44),
        ("openlighting-dmxsun-baseboard", 224),
        ("zhiayang-mikoto", 295),
    ],
)
def test_the_recorded_erc_baseline_classifies_as_findings(
    board: str, warnings: int
) -> None:
    text = (REAL / board / "baseline" / "erc.json").read_bytes().decode("utf-8")
    result = classify_erc_report(text)
    assert result.status is ErcStatus.FINDINGS
    assert (result.error_count, result.warning_count) == (0, warnings)
    assert result.kicad_version == "10.0.6"


def test_a_tree_hash_matches_the_one_copperbench_published() -> None:
    # CopperBench's fixture.json for mikoto records this hash for the same
    # tree, so the copy here is the one CopperBench tested.
    assert tree_sha256(REAL / "zhiayang-mikoto" / "tree") == (
        "85e81c58af200cc3fb39c8ec0e711acbc02fa4929ad9d9ffeddcfa8aff9dae42"
    )


def test_a_projects_own_kicad_8_libraries_are_reported_not_skipped() -> None:
    project = REAL / "zhiayang-mikoto" / "tree"
    variables = {"KIPRJMOD": project.as_posix()}
    symbols, symbol_problems = read_library_table(
        project / "sym-lib-table", "symbol", variables
    )
    footprints, footprint_problems = read_library_table(
        project / "fp-lib-table", "footprint", variables
    )
    assert symbol_problems == []
    assert footprint_problems == []
    assert [lib.nickname for lib in symbols] == ["mikoto"]
    report = LibraryIndex().update([*symbols, *footprints], workers=1)
    assert report.read == 34
    # Every one is in a KiCad 8 format, which the reader does not read.
    assert len(report.problems) == 34
    assert all("UnsupportedFormatError" in p.reason for p in report.problems)


def test_a_kicad_10_projects_own_library_is_indexed_through_its_table() -> None:
    project = REAL / "sanctuary-rpi-cm5io-mini-itx" / "tree"
    symbols, problems = read_library_table(
        project / "sym-lib-table", "symbol", {"KIPRJMOD": project.as_posix()}
    )
    assert problems == []
    index = LibraryIndex()
    assert index.update(symbols, workers=1).problems == ()
    (library,) = symbols
    names = index.symbol_names(library.nickname)
    assert len(names) == 18
    first = index.symbol(f"{library.nickname}:{names[0]}")
    assert first is not None
    assert first.pins
