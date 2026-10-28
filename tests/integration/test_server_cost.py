"""What the tools cost a client: the size of every response on real boards.

A model pays for every character a tool sends back, so no response may
pass ``RESPONSE_BUDGET`` characters, every entry of a paged list must be
reachable, and reading a project must leave it byte for byte as it was.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from kairo.kicad.cli import KicadCli
from kairo.library import Library, LibraryIndex
from kairo.server import Settings, Workspace, response_text

pytestmark = pytest.mark.kicad

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
#: Characters in one response. The largest measured on these boards and
#: libraries is about 20,700 (the pins of a 100-pin STM32H7).
RESPONSE_BUDGET = 25_000
BOARDS = {
    "antmicro-osm-l-bmc-som": "bmc-reference-osm.kicad_pro",
    "antmicro-polarfire-som": "polarfire-som.kicad_pro",
    "sanctuary-rpi-cm5io-mini-itx": "CM5IO.kicad_pro",
}


def tree_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        digest.update(str(path.relative_to(folder)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def within_budget(result: dict[str, Any]) -> dict[str, Any]:
    assert len(response_text(result)) <= RESPONSE_BUDGET
    return result


@pytest.mark.parametrize("board", sorted(BOARDS))
def test_a_real_board_is_read_whole_within_the_budget_and_left_untouched(
    board: str, kicad_cli_path: Path, tmp_path: Path
) -> None:
    tree = tmp_path / "work" / board
    shutil.copytree(REAL / board / "tree", tree)
    before = tree_hash(tree)
    settings = Settings((tmp_path / "work",), tmp_path / "cache", kicad_cli_path)
    workspace = Workspace(settings)
    project = str(tree / BOARDS[board])

    summary = within_budget(workspace.project_summary(project))
    lists: dict[str, list[dict[str, Any]]] = {}
    for what in ("components", "nets"):
        first = within_budget(workspace.read_schematic(project, what))
        lists[what] = list(first[what])
        for page in range(2, first[f"{what}_pages"] + 1):
            result = within_budget(workspace.read_schematic(project, what, page))
            lists[what] += result[what]
    assert len(lists["components"]) == summary["component_count"]
    assert len(lists["nets"]) == summary["net_count"]
    assert len({net["name"] for net in lists["nets"]}) == summary["net_count"]

    widest = max(lists["nets"], key=lambda net: net["pin_count"])
    first = within_budget(workspace.trace_net(project, widest["name"]))
    pins = len(first["pins"])
    for page in range(2, first["pins_pages"] + 1):
        pins += len(
            within_budget(workspace.trace_net(project, widest["name"], page))["pins"]
        )
    assert pins == widest["pin_count"]

    erc = within_budget(workspace.run_erc(project))
    shown = len(erc["violations"])
    # Later pages read the run just made, so they add up to its total.
    for page in range(2, erc["violations_pages"] + 1):
        shown += len(within_budget(workspace.run_erc(project, page))["violations"])
    assert shown == erc["violations_total"] == sum(erc["by_type"].values())
    for kind, count in erc["by_type"].items():
        only = within_budget(workspace.run_erc(project, violation_type=kind))
        assert only["violations_total"] == count

    within_budget(workspace.render_schematic(project))
    assert tree_hash(tree) == before


def test_the_largest_symbols_fit_the_budget(
    kicad_cli_path: Path, kicad_demos: Path, tmp_path: Path
) -> None:
    # The STM32H7 library has the symbols with the most alternate pin names,
    # which made the largest responses of all 22,860 installed symbols.
    library = kicad_demos.parent / "symbols" / "MCU_ST_STM32H7.kicad_sym"
    index = LibraryIndex()
    report = index.update([Library("symbol", "MCU_ST_STM32H7", library)])
    assert not report.problems
    settings = Settings((tmp_path,), tmp_path / "cache", kicad_cli_path)
    workspace = Workspace(settings, cli=KicadCli((str(kicad_cli_path),)), index=index)
    names = index.symbol_names("MCU_ST_STM32H7")
    assert len(names) > 100
    largest = 0
    for name in names:
        first = workspace.get_symbol_pins(f"MCU_ST_STM32H7:{name}")
        sizes = [len(response_text(first))]
        for page in range(2, first["pins_pages"] + 1):
            result = workspace.get_symbol_pins(f"MCU_ST_STM32H7:{name}", page)
            sizes.append(len(response_text(result)))
        largest = max(largest, *sizes)
    assert 10_000 < largest <= RESPONSE_BUDGET
    found = within_budget(workspace.find_symbol("STM32H7", limit=50))
    assert len(found["symbols"]) == 50
