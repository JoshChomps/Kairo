"""The real ``kairo`` command, driven over stdio the way an MCP client drives it.

The server is started as its own process on a copy of a real board, with
the installed KiCad, and every tool is called through the protocol. The
library tools index every installed library first, which takes about half
a minute.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.kicad

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"


async def call_all(params: StdioServerParameters, project: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    async with Client(params) as client:
        results["tools"] = sorted(t.name for t in (await client.list_tools()).tools)
        calls: dict[str, dict[str, Any]] = {
            "list_projects": {},
            "project_summary": {"project": str(project)},
            "read_schematic": {"project": str(project)},
            "trace_net": {"project": str(project), "net": "/ANT"},
            "run_erc": {"project": str(project)},
            "render_schematic": {"project": str(project)},
            "find_symbol": {"query": "AMS1117-3.3"},
            "get_symbol_pins": {"lib_id": "Regulator_Linear:AMS1117-3.3"},
            "find_footprint": {"query": "SOT-223-3_TabPin2"},
            "server_info": {},
            "recent_calls": {},
        }
        for name, arguments in calls.items():
            result = await client.call_tool(name, arguments)
            assert not result.is_error, (name, result.content)
            results[name] = result.structured_content
    return results


def test_every_tool_answers_over_stdio_on_a_real_board(
    kicad_cli_path: Path, tmp_path: Path
) -> None:
    project = tmp_path / "work" / "mikoto"
    shutil.copytree(REAL / "zhiayang-mikoto" / "tree", project)
    before = sorted(p.name for p in project.iterdir())
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "kairo",
            "--allowed-root",
            str(tmp_path / "work"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--kicad-cli",
            str(kicad_cli_path),
        ],
    )
    results = asyncio.run(call_all(params, project))

    # The default tier is read: the tools that look, describe, and say
    # which build serves and what it was asked.
    assert len(results["tools"]) == 18
    assert results["list_projects"]["projects_total"] == 1
    summary = results["project_summary"]
    assert (summary["component_count"], summary["net_count"]) == (67, 93)
    assert summary["erc"]["passed"] is False
    assert results["read_schematic"]["components"][0]["ref"] == "AE1"
    assert [pin["ref"] for pin in results["trace_net"]["pins"]] == ["AE1", "C22", "R11"]
    assert results["run_erc"]["status"] == "findings"
    assert results["run_erc"]["errors"] == 0
    (drawing,) = results["render_schematic"]["files"]
    assert Path(drawing).is_file()
    assert Path(drawing).is_relative_to(tmp_path / "cache")
    assert results["find_symbol"]["symbols"][0]["lib_id"] == (
        "Regulator_Linear:AMS1117-3.3"
    )
    assert [(p["number"], p["name"]) for p in results["get_symbol_pins"]["pins"]] == [
        ("1", "GND"),
        ("2", "VO"),
        ("3", "VI"),
    ]
    assert results["find_footprint"]["footprints"][0]["pad_count"] == 4
    assert results["server_info"]["extension"] == {"status": "not_an_extension"}
    assert results["server_info"]["programs"]["kicad_cli"]["path"] is not None
    assert results["recent_calls"]["by_tool"]["find_footprint"]["calls"] == 1
    # Reading a project leaves nothing behind in it.
    assert sorted(p.name for p in project.iterdir()) == before
