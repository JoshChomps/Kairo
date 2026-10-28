"""Checking a real, human-drawn board against stated facts, read through KiCad."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from kairo.server import Settings, build_server

pytestmark = pytest.mark.kicad

BOARD = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "real"
    / "git4dcc-rtb-c13"
    / "tree"
)
FACTS = {
    "nets": {
        "GND": {"role": "ground"},
        "+3V3": {"role": "power", "voltage": "3.3V"},
    },
    "loads": [{"net": "+3V3", "current": "50mA", "note": "stated"}],
}


def test_a_drawn_board_is_read_and_nothing_about_it_is_guessed(
    kicad_cli_path: Path, tmp_path: Path
) -> None:
    board = tmp_path / "c13"
    shutil.copytree(BOARD, board)
    before = {p: p.read_bytes() for p in board.rglob("*.kicad_sch")}
    settings = Settings((board,), tmp_path / "cache", kicad_cli_path, tier="propose")

    async def talk() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(settings)) as client:
            for facts in (None, FACTS, {"nets": {"NO_SUCH": {"role": "ground"}}}):
                arguments: dict[str, Any] = {"project": str(board)}
                if facts is not None:
                    arguments["facts"] = facts
                out.append(await client.call_tool("check_schematic", arguments))
        return out

    bare, stated, wrong = asyncio.run(talk())
    assert not bare.is_error, bare.content
    first = bare.structured_content
    # KiCad's netlist of this board has 122 parts; the 16 pins it marks as
    # left open are on no net.
    assert first["read"] == {
        "parts": 122,
        "nets": 99,
        "pins_left_open_on_purpose": 16,
        # Its parts give their LCSC numbers in a field named "LCSC Part #".
        "part_numbers_from_the_drawing": 91,
    }
    assert [n["net"] for n in first["unstated_supply_nets"]["listed"]] == [
        "+3V3",
        "GND",
        "Net-(U2-EN)",
    ]
    assert first["rails"] == {} and first["regulators"] == []

    assert not stated.is_error, stated.content
    second = stated.structured_content
    assert second["unstated_supply_nets"]["total"] == 1
    rail = second["rails"]["+3V3"]
    # Only what was stated is counted: the decoupling capacitors, marked
    # "100n" with no unit, carry no steady current.
    assert rail["draws"] == [{"what": "load: stated", "current": "50 mA"}]
    assert rail["current"] == ">= 50 mA"
    unknown = {u["part"] for u in rail["unknown"]}
    # Every chip supplied from the rail, and each diode and resistor that
    # leads off it, is named rather than left out.
    assert {"IC1", "U1", "D25", "R6", "JP1"} <= unknown
    assert not any(part.startswith("C") for part in unknown)
    # Kairo's review finds nothing to fault or suggest on this board, and
    # with no roles stated it says which rules it could not run.
    assert second["review"]["faults"]["total"] == 0
    assert second["review"]["suggestions"]["total"] == 0
    assert "rules_not_run" not in second["review"]
    assert "8 rules" in first["review"]["rules_not_run"]

    assert wrong.is_error
    assert "E_FACTS_INVALID" in str(wrong.content)
    assert {p: p.read_bytes() for p in board.rglob("*.kicad_sch")} == before


def test_facts_kept_with_the_project_are_used_without_being_restated(
    kicad_cli_path: Path, tmp_path: Path
) -> None:
    board = tmp_path / "c13"
    shutil.copytree(BOARD, board)
    sheets = {p: p.read_bytes() for p in board.rglob("*.kicad_sch")}
    settings = Settings((board,), tmp_path / "cache", kicad_cli_path, tier="write")

    async def talk() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(settings)) as client:
            project = {"project": str(board)}
            calls: list[tuple[str, dict[str, Any]]] = [
                ("save_facts", {**project, "facts": {"nets": {"NO_SUCH": {}}}}),
                ("save_facts", {**project, "facts": FACTS}),
                ("check_schematic", project),
                # A call's facts are laid over the kept ones.
                (
                    "check_schematic",
                    {
                        **project,
                        "facts": {"loads": [{"net": "+3V3", "current": "80mA"}]},
                    },
                ),
            ]
            for name, arguments in calls:
                out.append(await client.call_tool(name, arguments))
        return out

    refused, saved, bare, over = asyncio.run(talk())
    assert refused.is_error and "E_FACTS_INVALID" in str(refused.content)
    assert not saved.is_error, saved.content
    kept = board / "kairo" / "facts.json"
    assert saved.structured_content["file"] == str(kept)
    assert saved.structured_content["holds"] == {"loads": 1, "nets": 2}
    assert json.loads(kept.read_text(encoding="utf-8")) == FACTS

    assert not bare.is_error, bare.content
    first = bare.structured_content
    assert first["kept_facts"]["file"] == str(kept)
    assert first["rails"]["+3V3"]["current"] == ">= 50 mA"
    second = over.structured_content
    assert second["rails"]["+3V3"]["current"] == ">= 80 mA"
    # The kept file is unchanged by a check, and the drawing by all of it.
    assert json.loads(kept.read_text(encoding="utf-8")) == FACTS
    assert {p: p.read_bytes() for p in board.rglob("*.kicad_sch")} == sheets
