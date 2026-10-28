"""Remembering a chip's datasheet figures by its part number, and using them.

The chip is the step-down regulator of `minilib.buck`, whose figures are
those of TI's TPS54202 datasheet (SLVSD26C, page 5).
"""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from minilib import buck, make_index

from kairo.design_tools import PARTS_FOLDER, DesignTools
from kairo.kicad.cli import KicadCli
from kairo.kicad.drawn import read_drawn
from kairo.kicad.netlist import Component, Net, Netlist, Node
from kairo.parts import PartStore
from kairo.parts.fill import fill_from_store
from kairo.server import Settings, Workspace

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_kicad_cli.py"
DOCUMENT = "TPS54202 datasheet SLVSD26C, Texas Instruments"
FACTS: dict[str, Any] = {
    "regulator": {"value": "switching", "page": "1"},
    "v_ref": {"value": "0.596V", "page": "5", "condition": "VIN = 12V"},
    "v_ref_tolerance": {"value": "2.52%", "page": "5"},
    "f_sw": {"value": "390kHz", "page": "5", "limit": "min"},
    "i_limit": {"value": "2.5A", "page": "5", "limit": "min"},
    "i_out_max": {"value": "2A", "page": "1"},
}


@pytest.fixture
def tools(tmp_path: Path) -> DesignTools:
    libraries = tmp_path / "libraries"
    libraries.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings((root,), tmp_path / "cache", tier="write")
    cli = KicadCli((sys.executable, str(FAKE), "silent"), temp_root=tmp_path)
    return DesignTools(Workspace(settings, cli=cli, index=make_index(libraries)))


def named_only() -> dict[str, Any]:
    """The step-down stage with its chip named and none of its figures stated."""
    data = buck()
    data["components"]["U1"] = {"lib_id": "Sw:BUCK", "params": {"mpn": "TPS54202"}}
    return data


def statuses(report: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r["requirement"], r["status"]) for r in report["requirements"]]


def test_a_named_chip_takes_what_was_saved_for_it(
    tools: DesignTools, tmp_path: Path
) -> None:
    stated = tools.analyze_power_tree(buck())
    assert "parts_store" not in stated
    # Named and nothing more, the chip is no regulator at all.
    before = tools.analyze_power_tree(named_only())
    assert statuses(before) != statuses(stated)

    saved = tools.save_part("TPS54202", "Texas Instruments", DOCUMENT, "C", FACTS)
    assert Path(saved["file"]) == tmp_path / "cache" / PARTS_FOLDER / "TPS54202.json"
    assert saved["saved"]["i_limit"] == {
        "value": "2.5A",
        "limit": "min",
        "source": f"{DOCUMENT}, C, 5",
        "review": "unreviewed",
    }

    after = tools.analyze_power_tree(named_only())
    assert statuses(after) == statuses(stated)
    assert after["rails"] == stated["rails"]
    (line,) = after["parts_store"]["filled"]
    assert line.startswith("U1 (TPS54202): ")
    assert "unreviewed: v_ref, v_ref_tolerance, f_sw (min), i_limit (min)" in line
    assert "no person has checked" in after["parts_store"]["note"]


def test_what_the_design_states_is_never_replaced(
    tools: DesignTools, tmp_path: Path
) -> None:
    tools.save_part("TPS54202", "Texas Instruments", DOCUMENT, "C", FACTS)
    data = named_only()
    data["components"]["U1"]["params"]["i_limit"] = "3A"
    data["components"]["U1"]["source"] = "measured on the bench"
    store = PartStore(tmp_path / "cache" / PARTS_FOLDER)
    filled, notes = fill_from_store(data, store)
    chip = filled["components"]["U1"]
    assert chip["params"]["i_limit"] == "3A"
    assert chip["params"]["f_sw"] == "390kHz"
    # The stated limit keeps the side its author gave it, here none.
    assert chip["limits"] == {"f_sw": "min"}
    assert chip["source"].startswith("measured on the bench; saved for TPS54202, ")
    assert "i_limit" not in notes.filled[0]
    # The design given is left as it was.
    assert data["components"]["U1"]["params"] == {"mpn": "TPS54202", "i_limit": "3A"}


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        ({}, "facts is empty"),
        ({"v_reference": {"value": "0.6V", "page": "5"}}, "not a parameter"),
        ({"mpn": {"value": "X", "page": "1"}}, "not a parameter"),
        ({"v_ref": "0.596V"}, "give v_ref as"),
        ({"v_ref": {"value": "0.596V"}}, "give v_ref as"),
        ({"v_ref": {"value": "0.596A", "page": "5"}}, "v_ref: "),
        ({"v_ref": {"value": "0.6V", "page": "5", "limit": "typ"}}, "v_ref: "),
    ],
)
def test_a_figure_laid_out_wrongly_is_refused_and_nothing_is_saved(
    tools: DesignTools, tmp_path: Path, facts: dict[str, Any], message: str
) -> None:
    with pytest.raises(ToolError, match=f"E_PART_INVALID: .*{message}"):
        tools.save_part("TPS54202", "Texas Instruments", DOCUMENT, "C", facts)
    assert not (tmp_path / "cache" / PARTS_FOLDER).exists()


def test_a_figure_a_person_has_checked_is_not_changed(
    tools: DesignTools, tmp_path: Path
) -> None:
    saved = tools.save_part("TPS54202", "Texas Instruments", DOCUMENT, "C", FACTS)
    path = Path(saved["file"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["facts"]["i_limit"]["review"] = "reviewed"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ToolError, match=r"E_PART_REVIEWED: TPS54202's i_limit is 2.5A"):
        tools.save_part(
            "TPS54202",
            "Texas Instruments",
            DOCUMENT,
            "C",
            {"i_limit": {"value": "3.9A", "page": "5", "limit": "max"}},
        )
    # Saving it again as it is, or saving others, keeps the person's mark.
    again = tools.save_part(
        "TPS54202",
        "Texas Instruments",
        DOCUMENT,
        "D",
        {
            "i_limit": {"value": "2.5A", "page": "6", "limit": "min"},
            "i_q": {"value": "45uA", "page": "5"},
        },
    )
    assert again["saved"]["i_limit"]["review"] == "reviewed"
    assert again["saved"]["i_limit"]["source"].endswith("C, 5")
    assert again["saved"]["i_q"]["source"].endswith("D, 5")
    assert set(again["saved"]) == {*FACTS, "i_q"}

    # A file that cannot be read is said, and takes no part in a design.
    path.write_text("{", encoding="utf-8")
    report = tools.analyze_power_tree(named_only())
    assert report["parts_store"]["unusable"][0].startswith("U1: TPS54202.json")
    with pytest.raises(ToolError, match="E_PART_INVALID"):
        tools.save_part("TPS54202", "Texas Instruments", DOCUMENT, "C", FACTS)


def test_a_drawn_chip_with_a_part_number_field_takes_them_too(
    tools: DesignTools, tmp_path: Path
) -> None:
    tools.save_part("TPS54202", "Texas Instruments", DOCUMENT, "C", FACTS)
    chip = Component("U1", "TPS54202", "", "Own:Buck", "", "/", (("MPN", "tps54202"),))
    netlist = Netlist(
        "test",
        ("/",),
        (chip,),
        (Net("VIN", (Node("U1", "3", "VIN", "power_in"),)),),
    )
    store = PartStore(tmp_path / "cache" / PARTS_FOLDER)
    drawn = read_drawn(netlist, "drawn", None, None, store)
    # The field spells the number in small letters; it is the same part.
    part = drawn.grounded.design.components["U1"]
    assert drawn.numbered == {"U1": "tps54202, from its field 'MPN'"}
    assert drawn.remembered is not None and len(drawn.remembered.filled) == 1
    assert part.limits == {"f_sw": "min", "i_limit": "min"}
    assert part.params["regulator"] == "switching"
    assert "saved for TPS54202" in part.source
