# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from minilib import ldo, make_index

from kairo.design_tools import (
    MAX_ROUNDS,
    STALL_ROUNDS,
    DesignTools,
    _pages,  # pyright: ignore[reportPrivateUsage]
    _value_and_tolerance,  # pyright: ignore[reportPrivateUsage]
)
from kairo.kicad.cli import KicadCli
from kairo.server import TIERS, TOOL_DOCS, Settings, Workspace, build_server

TESTS = Path(__file__).resolve().parent.parent
FAKE = TESTS / "fakes" / "fake_kicad_cli.py"


@pytest.fixture
def tools(tmp_path: Path) -> DesignTools:
    libraries = tmp_path / "libraries"
    libraries.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings((root,), tmp_path / "cache", tier="write")
    # KiCad is stood in for by a program that is never asked anything here:
    # these tests stop before a file is written.
    cli = KicadCli((sys.executable, str(FAKE), "silent"), temp_root=tmp_path)
    return DesignTools(Workspace(settings, cli=cli, index=make_index(libraries)))


def folder(tmp_path: Path, name: str = "ldo") -> str:
    return str(tmp_path / "root" / name)


def test_each_tier_offers_its_tools_and_those_below() -> None:
    def offered(tier: str) -> set[str]:
        settings = Settings((Path.cwd(),), Path.cwd() / "unused", tier=tier)
        return {tool.name for tool in asyncio.run(build_server(settings).list_tools())}

    read, propose, write = (offered(tier) for tier in TIERS)
    assert read < propose < write == set(TOOL_DOCS)
    assert {"design_schema", "accuracy_report", "read_schematic"} <= read
    assert read.isdisjoint(
        {"calc", "plan_design", "apply_design", "edit_design", "undo"}
    )
    assert propose - read == {
        "calc",
        "check_schematic",
        "analyze_power_tree",
        "plan_design",
        "save_part",
        "simulate",
        "simulate_schematic",
        "verify_design",
    }
    assert write - propose == {
        "apply_design",
        "edit_design",
        "make_board",
        "edit_schematic",
        "save_facts",
        "set_value",
        "undo",
    }
    for name, doc in TOOL_DOCS.items():
        assert doc.tier in TIERS, name
    with pytest.raises(ValueError, match="tier must be one of"):
        build_server(Settings((Path.cwd(),), Path.cwd(), tier="admin"))


def test_calc_runs_a_cited_formula_and_never_invents_a_tolerance(
    tools: DesignTools,
) -> None:
    result = tools.calc(
        "led_current",
        {
            "v_supply": {
                "value": "3.3V",
                "tolerance": "2%",
                "source": "U1 datasheet p.4",
            },
            "v_f": "2.0V",
            "r": {"value": "1k", "tolerance": "1%"},
        },
    )
    assert (result["status"], result["value"]) == ("ok", "0.0013 A")
    assert result["corners"] == {
        "status": "unverified",
        "missing": ["no tolerance on v_f"],
    }
    assert result["accuracy"]["status"] == "unverified"
    sources = {t["parameter"]: t["source"] for t in result["accuracy"]["tolerances"]}
    assert sources == {
        "v_supply": "U1 datasheet p.4",
        "v_f": "missing",
        "r": "user-stated",
    }
    with pytest.raises(ToolError, match=r"E_CALC_UNKNOWN.*led_current"):
        tools.calc("magic", {})
    with pytest.raises(ToolError, match=r"E_CALC_INPUT.*no input 'volts'"):
        tools.calc("led_current", {"volts": "3V"})
    with pytest.raises(ToolError, match=r"E_CALC_INPUT: r: .*expected ohm"):
        tools.calc("led_current", {"v_supply": "3.3V", "v_f": "2V", "r": "1kV"})
    with pytest.raises(ToolError, match="v_supply must be above v_f"):
        tools.calc("led_current", {"v_supply": "1V", "v_f": "2V", "r": "1k"})


def test_calc_reads_a_tolerance_written_after_its_value(tools: DesignTools) -> None:
    # The BMS work of 2026-10-16 wrote "0.3m 1%" and was refused: only the
    # object form carried a tolerance.
    result = tools.calc(
        "led_current", {"v_supply": "3.3V +/-2%", "v_f": "2.0V 10%", "r": "1k 1%"}
    )
    assert result["corners"]["status"] != "unverified"
    tolerances = {
        t["parameter"]: t["tolerance"] for t in result["accuracy"]["tolerances"]
    }
    assert tolerances == {"v_supply": "2%", "v_f": "10%", "r": "1%"}
    # A percentage on its own is a value, not a tolerance.
    assert _value_and_tolerance("90%") == {"value": "90%"}
    with pytest.raises(ToolError, match=r"E_CALC_INPUT: r: .*'10k 1%'"):
        tools.calc("led_current", {"v_supply": "3.3V", "v_f": "2V", "r": "1kV"})


def test_pages_may_be_a_number_or_text() -> None:
    assert _pages(5) == [5]
    assert _pages("5-7, 9") == [5, 6, 7, 9]
    assert _pages([2, 3]) == [2, 3]
    with pytest.raises(ToolError, match="E_PAGES_INVALID"):
        _pages("page five")


def test_a_plan_checks_everything_and_writes_nothing(
    tools: DesignTools, tmp_path: Path
) -> None:
    plan = tools.plan_design(ldo())
    assert plan["can_apply"] is True
    assert plan["preview"]["paper"] == "A4" and plan["preview"]["connected_pins"] == 13
    assert plan["verifier"]["layers"]["L2"]["status"] == "pass"
    assert plan["verifier"]["layers"]["L3"]["status"] == "not run"
    assert plan["unverified"] == ["max_junction_temp", "regulator_stability"]
    assert list((tmp_path / "root").iterdir()) == []

    wrong = ldo()
    wrong["connections"]["VIN_5V"][1] = "U1.VIN"
    plan = tools.plan_design(wrong)
    assert plan["can_apply"] is False and plan["preview"] is None
    assert plan["verifier"]["findings"][0]["code"] == "E_PIN_UNKNOWN"

    open_pin = ldo()
    open_pin["connections"]["VIN_5V"].remove("U1.VI")
    plan = tools.plan_design(open_pin)
    assert plan["can_apply"] is False
    assert plan["verifier"]["findings"][0]["code"] == "E_POWER_PIN_UNCONNECTED"


def test_a_malformed_design_names_the_fields(tools: DesignTools) -> None:
    bad = ldo()
    bad["nets"]["+3V3"]["voltage"] = "3.3"
    bad["loads"][0]["current"] = 300
    with pytest.raises(ToolError) as caught:
        tools.plan_design(bad)
    message = str(caught.value)
    assert message.startswith("E_DESIGN_INVALID")
    assert "nets.+3V3.voltage" in message and "loads.0.current" in message
    assert "design_schema" in message


def test_the_power_tree_and_accuracy_report_name_what_to_supply(
    tools: DesignTools,
) -> None:
    tree = tools.analyze_power_tree(ldo())
    assert tree["rails"]["+3V3"]["current"] == "301.3 mA"
    assert tree["rails"]["VIN_5V"]["current"] == ">= 301.3 mA"
    # J1 is in no connector library here, so it is not taken to be empty.
    assert [u["part"] for u in tree["rails"]["VIN_5V"]["unknown"]] == ["J1", "U1"]
    assert tree["regulators"] == [
        {"ref": "U1", "kind": "linear", "from": "VIN_5V", "makes": "+3V3"}
    ]
    report = tools.accuracy_report(ldo())
    # The test design turns Kairo's default margins off, and says so.
    assert report["policies"] == [
        "Margins: only tolerances the design states were used; Kairo's "
        "default margins are turned off."
    ]
    with_policy = tools.accuracy_report(
        {**ldo(), "policy": {"margins": "stated", "theta_ja_margin": "20%"}}
    )
    assert len(with_policy["policies"]) == 2
    assert "plus 20%" in with_policy["policies"][1]
    assert "not a pass against a datasheet limit" in with_policy["policies"][1]
    assert list(report)[:3] == ["design", "status", "to_resolve"]
    assert report["status"] == "unverified"
    assert report["counts"] == {
        "pass": 1,
        "fail": 0,
        "unverified": 2,
        "pass_on_default_margins": 0,
    }
    first = report["to_resolve"][0]
    assert "quiescent current (i_q)" in first["supply"]
    assert first["resolves"] == ["requirement 1: max_junction_temp (U1)"]
    assert {
        "ref": "R1",
        "parameter": "value",
        "tolerance": "1%",
        "source": "user-stated",
    } in (report["tolerances"])


def test_an_unmeetable_requirement_is_refused_with_the_arithmetic(
    tools: DesignTools, tmp_path: Path
) -> None:
    heavy = ldo()
    heavy["loads"][0]["current"] = "900mA"
    result = tools.apply_design(heavy, folder(tmp_path))
    assert result["written"] is False
    assert result["code"] == "E_REQUIREMENT_UNSATISFIABLE"
    assert "do not loosen" in result["message"]
    (failed,) = result["failed_requirements"]
    assert failed["type"] == "max_junction_temp" and failed["status"] == "fail"
    # 40 + (5 - 3.3) * 0.9013 * 90 = 177.9 degC against 125.
    assert "177.9 degC" in failed["summary"] and "125 degC" in failed["summary"]
    (arithmetic,) = failed["arithmetic"]
    assert arithmetic["equation"].startswith("TJ = TA +")
    assert arithmetic["inputs"]["Iout"].startswith("0.9013 A")
    assert not (tmp_path / "root" / "ldo").exists()


def test_failed_attempts_that_do_not_improve_are_stopped(
    tools: DesignTools, tmp_path: Path
) -> None:
    wrong = ldo()
    wrong["connections"]["VIN_5V"][1] = "U1.VIN"
    target = folder(tmp_path)
    results = [tools.apply_design(wrong, target) for _ in range(STALL_ROUNDS + 1)]
    # The first attempt sets the score to beat; the next three do not beat it.
    assert [r["stop"] for r in results] == [False, False, False, True]
    assert results[-1]["attempts"]["without_improvement"] == STALL_ROUNDS
    assert "tell them" in results[-1]["message"]
    with pytest.raises(ToolError, match="E_NO_PROGRESS"):
        tools.apply_design(wrong, target)
    # Even a correct design is refused now: the user has to be consulted.
    with pytest.raises(ToolError, match=r"E_NO_PROGRESS.*tell the user"):
        tools.apply_design(ldo(), target)
    # Another project is not affected.
    other = tools.apply_design(wrong, folder(tmp_path, "other"))
    assert other["stop"] is False
    assert not (tmp_path / "root" / "ldo").exists()


def test_there_is_a_hard_limit_on_attempts(
    tools: DesignTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kairo.design_tools as module

    # With the stall rule out of the way, the count alone stops it.
    monkeypatch.setattr(module, "STALL_ROUNDS", 10_000)
    wrong = ldo()
    wrong["connections"]["VIN_5V"][1] = "U1.VIN"
    target = folder(tmp_path)
    results = [tools.apply_design(wrong, target) for _ in range(MAX_ROUNDS)]
    assert results[-1]["stop"] is True
    assert results[-1]["attempts"] == {
        "made": MAX_ROUNDS,
        "limit": MAX_ROUNDS,
        "without_improvement": MAX_ROUNDS - 1,
    }
    with pytest.raises(ToolError, match="E_ROUND_LIMIT"):
        tools.apply_design(wrong, target)


#: The tools whose design or operations a model may send as JSON text.
TEXT_ARGUMENTS = {
    "accuracy_report": "design",
    "analyze_power_tree": "design",
    "plan_design": "design",
    "simulate": "design",
    "apply_design": "design",
    "edit_design": "operations",
    "edit_schematic": "operations",
}


def test_a_design_sent_as_json_text_is_read_as_the_object(
    tools: DesignTools, tmp_path: Path
) -> None:
    # A local model sent apply_design its design as one JSON string, and the
    # tool's argument check refused it before Kairo saw it.
    as_object = tools.plan_design(ldo())
    as_text = tools.plan_design(json.dumps(ldo()))
    assert as_text["can_apply"] is True
    assert as_text["preview"] == as_object["preview"]
    # Text that is not JSON is refused saying where it breaks: this one has
    # a closing brace too many, as the model's had.
    broken = json.dumps(ldo()) + "}"
    with pytest.raises(ToolError) as caught:
        tools.plan_design(broken)
    message = str(caught.value)
    assert message.startswith("E_JSON_INVALID: design")
    assert f"character {len(broken) - 1}" in message
    with pytest.raises(ToolError, match=r"E_JSON_INVALID: design .*an object"):
        tools.plan_design("[1, 2]")
    # Operations are read the same way, before the project is looked for.
    target = folder(tmp_path)
    with pytest.raises(ToolError, match=r"E_JSON_INVALID: operations .*a list"):
        tools.edit_design(target, '{"op": "remove"}')
    with pytest.raises(ToolError, match="E_NO_SESSION"):
        tools.edit_design(target, '[{"op": "remove", "ref": "D1"}]')


def test_the_server_lets_a_design_or_operations_through_as_text(
    tmp_path: Path,
) -> None:
    settings = Settings((tmp_path,), tmp_path / "cache", tier="write")
    listed: Any = asyncio.run(build_server(settings).list_tools())
    schemas = {tool.name: tool.input_schema for tool in listed}
    for name, argument in TEXT_ARGUMENTS.items():
        kinds = {
            option.get("type")
            for option in schemas[name]["properties"][argument]["anyOf"]
        }
        assert "string" in kinds, name


def test_only_projects_this_server_built_can_be_edited_or_undone(
    tools: DesignTools, tmp_path: Path
) -> None:
    target = folder(tmp_path)
    for call in (
        lambda: tools.edit_design(target, [{"op": "remove", "ref": "D1"}]),
        lambda: tools.undo(target),
        lambda: tools.verify_design(target),
    ):
        with pytest.raises(ToolError, match="E_NO_SESSION"):
            call()
    with pytest.raises(ToolError, match="E_PATH_NOT_ALLOWED"):
        tools.apply_design(ldo(), str(tmp_path / "elsewhere"))


def test_the_schema_tool_gives_the_shape(tools: DesignTools) -> None:
    schema = tools.design_schema()
    # What writing a design needs; the edit operations are asked for apart.
    assert set(schema) == {"design", "example", "parameters", "rules", "more"}
    assert set(tools.design_schema("patch")) == {"patch_operations", "more"}
    assert set(tools.design_schema("all")) == {
        "design",
        "example",
        "patch_operations",
        "parameters",
        "rules",
    }
    with pytest.raises(ToolError, match="E_SCHEMA_PART"):
        tools.design_schema("everything")
    # Small enough to read once: it was 18,083 characters before the
    # edit operations were split off and the padding taken out.
    # Each kind of requirement added since costs about 400 characters;
    # max_rail_current 465, and programmed_current, the latest, 1,061, as
    # either of its bounds may be left out and it brings two parameters.
    # On 2026-10-16 a net's range and surges, pin ratings and the setting
    # requirement took it from 14,526 to 17,108 (BMS gaps 12 and 19).
    # On 2026-10-23 sheets and the title block took it to 18,601; the board
    # (with its net classes and maker's rules, 5,225) then moved to part
    # "board", leaving 18,875.
    assert len(json.dumps(schema)) < 19_000
    assert "NetClass" not in json.dumps(schema)
    board = tools.design_schema("board")
    assert {"NetClass", "FabRules", "Pour"} <= set(board["board"]["$defs"])
    text = json.dumps(schema["design"])
    assert '"title": "' not in text and "  " not in text
    # Nothing a field means was dropped with the padding.
    assert "Where the params came from" in text
    assert schema["parameters"]["regulator"] == "text"


def test_every_tool_description_is_complete() -> None:
    settings = Settings((Path.cwd(),), Path.cwd() / "unused", tier="write")
    listed: Any = asyncio.run(build_server(settings).list_tools())
    for tool in listed:
        doc = TOOL_DOCS[tool.name]
        for part in (doc.what, doc.needs, doc.limits, doc.not_for):
            assert len(part) > 5, tool.name
        for label in ("\nNeeds: ", "\nLimits: ", "\nNot for: "):
            assert label in tool.description
    # The write tools say when to stop.
    assert "stop and tell the user" in TOOL_DOCS["apply_design"].limits


def test_the_tool_listing_stays_lean() -> None:
    # Every session that loads Kairo pays for the listing: it was 35,857
    # characters before it was trimmed to about 18,400 (2026-10-27).
    settings = Settings((Path.cwd(),), Path.cwd() / "unused", tier="write")
    server = build_server(settings)
    listed: Any = asyncio.run(server.list_tools())
    wire = sum(
        len(tool.model_dump_json(by_alias=True, exclude_none=True)) for tool in listed
    )
    assert wire < 19_500
    for tool in listed:
        text = json.dumps(tool.input_schema)
        assert '"title"' not in text and '{"type": "null"}' not in text, tool.name
        assert tool.output_schema is None, tool.name
    # Calls are still validated against the full schema.
    with pytest.raises(ToolError, match="valid string"):
        asyncio.run(server.call_tool("find_part", {"kind": 3, "value": "1k"}))


def test_the_accuracy_report_says_what_a_simulation_would_rest_on(
    tools: DesignTools,
) -> None:
    simulation = tools.accuracy_report(ldo())["simulation"]
    # Nothing was simulated to say this, and the report says so first.
    assert next(iter(simulation)) == "nothing_was_simulated"
    models = {m["ref"]: m["fidelity"] for m in simulation["models"]}
    assert models["U1"] == "behavioural" and models["C1"] in (
        "primitive",
        "bias-adjusted",
    )
    assert {e["name"] for e in simulation["engines"]} == {"ngspice", "xyce"}
    for engine in simulation["engines"]:
        assert engine["installed"] in (True, False)
        if engine["installed"]:
            assert isinstance(engine["validated"], bool) and engine["version"]
        else:
            assert "searched" in engine["detail"]
    assert simulation["second_simulator"] is all(
        e["installed"] for e in simulation["engines"]
    )
    # Every value a worst case would need a tolerance for is named.
    assert all("to_resolve" in v for v in simulation["values_with_no_tolerance"])
    assert simulation["thresholds"]["changed_by_the_project"] is False
    assert simulation["thresholds"]["agreement_between_runs_and_simulators"]["V"] == (
        "0.1% of the number"
    )
    # A design with no ground net cannot be made into a circuit, and the
    # rest of the report is still given.
    design = ldo()
    design["nets"]["GND"]["role"] = "signal"
    report = tools.accuracy_report(design)
    assert "no ground net" in report["simulation"]["unavailable"]
    assert "status" in report
