"""Simulating part of a real, human-drawn board.

The pedalboard's switching regulator has a compensation network of R14
(20 kilohms) in series with C31 (1.2 nF) to ground. Those two parts, cut
out of the board by name, are a low-pass filter whose corner is
1 / (2 pi R C) = 6631.5 Hz.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kairo.design_tools import DesignTools
from kairo.kicad.edit import set_value
from kairo.server import Settings, Workspace
from kairo.spice.engines import find_simulator

pytestmark = [pytest.mark.kicad, pytest.mark.spice]

BOARD = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "real"
    / "pedalboard-pedalboard-hw"
    / "tree"
)
FACTS: dict[str, Any] = {
    "nets": {"GND": {"role": "ground"}, "+3V3": {"role": "power", "voltage": "3.3V"}},
    "components": {"U3": {"params": {"i_supply": "30mA"}, "source": "stated"}},
    "loads": [{"net": "+3V3", "current": "50mA"}],
    "requirements": [{"type": "rail_voltage", "net": "+3V3", "min": "3.2V"}],
}
SWEEP = {
    "analysis": {"kind": "ac", "f_start": "100Hz", "f_stop": "1MHz"},
    "stimuli": [{"net": "Net-(U6-COMP)", "kind": "ac", "value": "1V"}],
}


@pytest.fixture
def tools(tmp_path: Path, kicad_cli_path: Path) -> tuple[DesignTools, Path]:
    if find_simulator("ngspice")[0] is None:
        pytest.skip("ngspice not found")
    board = tmp_path / "pedalboard"
    shutil.copytree(BOARD, board)
    settings = Settings((board,), tmp_path / "cache", kicad_cli_path, tier="propose")
    return DesignTools(Workspace(settings), None), board


def test_two_parts_of_a_real_board_are_simulated_as_drawn(
    tools: tuple[DesignTools, Path],
) -> None:
    design_tools, board = tools
    out = design_tools.simulate_schematic(str(board), ["R14", "C31"], SWEEP, FACTS)
    assert out["simulated"]["parts"] == ["C31", "R14"]
    assert out["simulated"]["nets"] == ["GND", "Net-(C31-Pad1)", "Net-(U6-COMP)"]
    # The facts about the rest of the board do not apply, and are named.
    assert out["simulated"]["facts_left_out"] == [
        "net +3V3: none of the named parts is on it",
        "part U3: not among the named parts",
        "a load on +3V3: none of the named parts is on that net",
        "requirements: a simulation judges its own limits",
    ]
    cutoff = next(m for m in out["metrics"] if m["metric"] == "cutoff")
    assert cutoff["where"] == "Net-(C31-Pad1)"
    assert cutoff["value"] == pytest.approx(1 / (2 * math.pi * 20e3 * 1.2e-9), rel=1e-3)
    # Both parts were read from the drawing: the board writes "20K".
    assert out["to_resolve"] == []
    assert {m["ref"] for m in out["models"]} >= {"R14", "C31"}
    assert not any(board.rglob("*.cir"))
    assert "monte_carlo" not in out

    # Asked for, a sampled spread is reported beside the result. The
    # corner falls as R14 rises, so over 1% either way it spans less
    # than 1% each side of 6631.5 Hz.
    facts = {
        **FACTS,
        "components": {"R14": {"params": {"tolerance": "1%"}, "source": "stated"}},
    }
    sampled = design_tools.simulate_schematic(
        str(board),
        ["R14", "C31"],
        {
            **SWEEP,
            "monte_carlo": {
                "samples": 20,
                "seed": 3,
                "distributions": {"R14": {"shape": "uniform"}},
            },
        },
        facts,
    )
    report = sampled["monte_carlo"]
    assert report["sampled"] == ["R14: uniform within 1%"]
    corner = next(s for s in report["spread"] if s["metric"] == "cutoff")
    exact = 1 / (2 * math.pi * 20e3 * 1.2e-9)
    assert exact / 1.0101 < corner["lowest"] < corner["highest"] < exact / 0.9899
    assert corner["samples"] == 20


def test_a_circuit_with_a_part_left_open_is_never_called_checked(
    tools: tuple[DesignTools, Path],
) -> None:
    design_tools, board = tools
    # With R14's value unreadable the resistor is left open, nothing
    # reaches the capacitor, and no number is measured. Such a run came
    # back "checked", having checked nothing.
    edit = set_value(board / "pedalboard-hw.kicad_sch", "R14", "tbd")
    for name, content in edit.files.items():
        (board / name).write_bytes(content.encode("utf-8"))
    out = design_tools.simulate_schematic(
        str(board), ["R14", "C31"], SWEEP, {"nets": {"GND": {"role": "ground"}}}
    )
    assert out["metrics"] == []
    assert out["status"] == "unverified"
    assert any("R14" in line for line in out["to_resolve"])
    assert any("No number was measured" in note for note in out["notes"])

    with pytest.raises(ToolError, match=r"E_FACTS_INVALID.*R9999"):
        design_tools.simulate_schematic(str(board), ["R9999"], SWEEP, FACTS)
    with pytest.raises(ToolError, match=r"E_FACTS_INVALID.*parts is empty"):
        design_tools.simulate_schematic(str(board), [], SWEEP, FACTS)
