"""The whole loop from a design to a checked project, through an MCP client.

The server is built at the write tier on KiCad's own libraries and driven
through the MCP library's client, the way Claude Code drives it: read the
schema, plan, apply, read the result back with the read tools, edit,
verify, undo.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from kairo.accuracy.report import REPORT_FILE
from kairo.server import Settings, build_server, default_cache_dir

pytestmark = pytest.mark.kicad

EXAMPLE = (
    Path(__file__).resolve().parent.parent.parent
    / "evals"
    / "reference_designs"
    / "ldo_3v3"
    / "design.json"
)


def test_the_example_goes_from_design_to_checked_project_and_back(
    kicad_cli_path: Path, tmp_path: Path
) -> None:
    import os

    root = tmp_path / "projects"
    root.mkdir()
    # The library index is the machine's own, built once and reused, as it
    # is for a real client.
    settings = Settings(
        (root,), default_cache_dir(os.environ), kicad_cli_path, tier="write"
    )
    design = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    folder = str(root / "ldo_3v3")

    async def talk() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        async with Client(build_server(settings)) as client:

            async def call(name: str, **arguments: Any) -> Any:
                result = await client.call_tool(name, arguments)
                assert not result.is_error, (name, result.content)
                # Every response is small enough for a model to read.
                text = "".join(getattr(part, "text", "") for part in result.content)
                assert 0 < len(text) < 25_000, name
                return result.structured_content

            seen["tools"] = {t.name for t in (await client.list_tools()).tools}
            seen["schema"] = await call("design_schema")
            seen["plan"] = await call("plan_design", design=design)
            seen["applied"] = await call("apply_design", design=design, folder=folder)
            seen["summary"] = await call("project_summary", project=folder)
            seen["net"] = await call("trace_net", project=folder, net="+3V3")
            seen["verified"] = await call("verify_design", folder=folder)
            seen["edited"] = await call(
                "edit_design",
                folder=folder,
                operations=[
                    {"op": "set_param", "ref": "R1", "name": "value", "value": "2k2"}
                ],
            )
            seen["after_edit"] = await call("read_schematic", project=folder)
            seen["undone"] = await call("undo", folder=folder)
            seen["after_undo"] = await call("read_schematic", project=folder)
            seen["report"] = await call("accuracy_report", design=design)
        return seen

    seen = asyncio.run(talk())
    # Every tool of the write tier, make_board included.
    assert len(seen["tools"]) == 33
    assert "make_board" in seen["tools"]
    assert seen["plan"]["can_apply"] is True

    applied = seen["applied"]
    assert applied["written"] is True
    assert applied["verifier"]["passed"] is True
    assert [
        applied["verifier"]["layers"][layer]["status"]
        for layer in ("L0", "L1", "L2", "L3", "L4")
    ] == ["pass"] * 5
    assert {Path(f["file"]).name for f in applied["files"]} == {
        "ldo_3v3.kicad_pro",
        "ldo_3v3.kicad_sch",
        "accuracy-report.md",
    }
    assert applied["accuracy"]["status"] == "unverified"
    resolve = " ".join(applied["accuracy"]["to_resolve"])
    assert "U1's quiescent current (i_q)" in resolve
    assert "C2" in resolve and "DC-bias curve" in resolve

    # The read tools see, through KiCad, the circuit that was asked for.
    assert (seen["summary"]["component_count"], seen["summary"]["net_count"]) == (6, 4)
    assert seen["summary"]["erc"]["passed"] is True
    assert [p["ref"] for p in seen["net"]["pins"]] == ["C2", "R1", "U1"]
    assert seen["verified"]["passed"] is True
    # KiCad's own SPICE netlister reads the file Kairo wrote the same way.
    assert seen["verified"]["spice_netlist"]["status"] == "agrees"
    assert seen["verified"]["spice_netlist"]["differences"] == []

    # The report on disk says the same as the tool.
    saved = (Path(folder) / REPORT_FILE).read_text(encoding="utf-8")
    assert "Status: **unverified**" in saved
    assert "U1's quiescent current (i_q)" in saved and "DC-bias curve" in saved
    assert seen["report"]["status"] == "unverified"
    tolerances = {(t["ref"], t["parameter"]): t for t in seen["report"]["tolerances"]}
    assert tolerances[("R1", "value")]["source"] == "user-stated"
    assert tolerances[("C2", "value")]["tolerance"] == "10%"

    # An edit changes one value, a handful of lines, and KiCad still agrees.
    edited = seen["edited"]
    assert edited["written"] is True
    sheet = next(f for f in edited["files"] if f["file"].endswith(".kicad_sch"))
    assert sheet["created"] is False and 0 < sheet["changed_lines"] <= 4
    assert sheet["changed_ratio"] < 0.01
    values = {c["ref"]: c["value"] for c in seen["after_edit"]["components"]}
    assert values["R1"] == "2k2"
    # Undo puts the first version back.
    assert seen["undone"]["undone"] is True
    values = {c["ref"]: c["value"] for c in seen["after_undo"]["components"]}
    assert values["R1"] == "1k"
