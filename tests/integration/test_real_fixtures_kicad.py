"""The real boards, with KiCad 10 itself doing the reading.

The KiCad 8 boards are copied to a temporary folder and every sheet is
re-saved with ``kicad-cli sch upgrade --force``, which writes the format
Kairo reads. This is a command-line save; the boards saved by the KiCad 9
and 10 programs cover what the program itself writes.

Those boards are run through kicad-cli's netlist export and ERC as they
are. Three are run by default; set ``KAIRO_FULL_CORPUS=1`` to run all twelve
(about two minutes more).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kairo.kicad.cli import ErcStatus, KicadCli, NetlistStatus
from kairo.kicad.formats import SCHEMATIC, check_format
from kairo.kicad.netlist import parse_netlist
from kairo.kicad.sexpr import parse
from kairo.process import run_process

pytestmark = pytest.mark.kicad

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
ROOT_SHEETS = {
    "antmicro-jetson-orin-baseboard": "jetson-orin-baseboard.kicad_sch",
    "antmicro-microphone-board": "hardware/microphone-board.kicad_sch",
    "openlighting-dmxsun-baseboard": (
        "hardware/baseboard_2slots/baseboard_2slots.kicad_sch"
    ),
    "zhiayang-mikoto": "mikoto.kicad_sch",
}
# Root sheet, then what kicad-cli 10.0.6's netlist holds: sheets, parts, nets.
CURRENT: dict[str, tuple[str, int, int, int]] = {
    "antmicro-artix-dc-scm": ("artix-dc-scm.kicad_sch", 11, 544, 699),
    "antmicro-com-express-7-baseboard": (
        "com-express-7-baseboard.kicad_sch",
        19,
        805,
        994,
    ),
    "antmicro-jetson-nano-baseboard": ("jetson-nano-baseboard.kicad_sch", 8, 373, 487),
    "antmicro-osm-l-bmc-som": ("bmc-reference-osm.kicad_sch", 20, 384, 1062),
    "antmicro-polarfire-som": ("polarfire-som.kicad_sch", 18, 460, 770),
    "antmicro-sodimm-ddr5-tester": ("sodimm-ddr5-tester.kicad_sch", 17, 617, 757),
    "craigg96-vesc6-open-hardware": ("VESC6_OPEN_HARDWARE.kicad_sch", 6, 128, 117),
    "git4dcc-rtb-c13": ("C13.kicad_sch", 5, 122, 115),
    "greatscottgadgets-hackrf-pro": ("praline.kicad_sch", 12, 577, 490),
    "lsartory-clavier": ("clavier.kicad_sch", 13, 344, 289),
    "pedalboard-pedalboard-hw": ("pedalboard-hw.kicad_sch", 6, 169, 289),
    "sanctuary-rpi-cm5io-mini-itx": ("CM5IO.kicad_sch", 4, 68, 257),
}
DEFAULT = {
    "antmicro-polarfire-som",
    "greatscottgadgets-hackrf-pro",
    "sanctuary-rpi-cm5io-mini-itx",
}


@pytest.mark.parametrize("board", sorted(ROOT_SHEETS))
def test_a_board_kicad_10_re_saved_round_trips_and_runs_erc(
    board: str, kicad_cli_path: Path, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(REAL / board / "tree", tree)
    sheets = sorted(tree.rglob("*.kicad_sch"))
    for sheet in sheets:
        result = run_process(
            [str(kicad_cli_path), "sch", "upgrade", "--force", str(sheet)],
            timeout_s=300,
            cwd=sheet.parent,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = sheet.read_bytes()
        document = parse(data.decode("utf-8"))
        assert check_format(document, SCHEMATIC) == 20260306
        assert document.to_text().encode("utf-8") == data

    erc = KicadCli((str(kicad_cli_path),)).run_erc(tree / ROOT_SHEETS[board])
    # These boards have no ERC errors; their warnings come from libraries a
    # bare copy cannot find. What matters here is that the report is read.
    assert erc.status is ErcStatus.FINDINGS, erc.reason
    assert erc.error_count == 0


@pytest.mark.parametrize("board", sorted(CURRENT))
def test_kicads_netlist_and_erc_for_a_board_are_read(
    board: str, kicad_cli_path: Path, tmp_path: Path
) -> None:
    if board not in DEFAULT and os.environ.get("KAIRO_FULL_CORPUS") != "1":
        pytest.skip("set KAIRO_FULL_CORPUS=1 to run every board")
    root_sheet, sheets, parts, nets = CURRENT[board]
    tree = tmp_path / "tree"
    shutil.copytree(REAL / board / "tree", tree)
    before = sorted(p.name for p in tree.iterdir())
    cli = KicadCli((str(kicad_cli_path),))

    exported = cli.export_netlist(tree / root_sheet)
    assert exported.status is NetlistStatus.OK, exported.reason
    assert exported.text is not None
    netlist = parse_netlist(exported.text)
    assert (len(netlist.sheets), len(netlist.components), len(netlist.nets)) == (
        sheets,
        parts,
        nets,
    )
    refs = {c.ref for c in netlist.components}
    assert len(refs) == parts
    assert {node.ref for net in netlist.nets for node in net.nodes} <= refs

    erc = cli.run_erc(tree / root_sheet)
    # A real board has findings; what must not happen is a report Kairo
    # cannot read.
    assert erc.status is ErcStatus.FINDINGS, erc.reason
    assert sorted(p.name for p in tree.iterdir()) == before
