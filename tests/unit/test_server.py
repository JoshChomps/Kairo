from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from test_library_reader import DERIVED, REG, library

from kairo import progress
from kairo.kicad import gui
from kairo.kicad.cli import KicadCli, classify_erc_report
from kairo.library import Library, LibraryIndex
from kairo.server import (
    PAGE_SIZE,
    Settings,
    Workspace,
    build_server,
    default_cache_dir,
    erc_report,
    find_projects,
)

TESTS = Path(__file__).resolve().parent.parent
ERC = TESTS / "fixtures" / "kicad10" / "erc"
NETLIST = TESTS / "fixtures" / "kicad10" / "netlist" / "mikoto.net"
FAKE = TESTS / "fakes" / "fake_kicad_cli.py"
REAL_ERC = TESTS / "fixtures" / "real" / "zhiayang-mikoto" / "baseline" / "erc.json"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "root" / "demo"
    folder.mkdir(parents=True)
    (folder / "demo.kicad_pro").write_text("{}", encoding="utf-8")
    (folder / "demo.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    return tmp_path / "root"


@pytest.fixture
def settings(root: Path, tmp_path: Path) -> Settings:
    return Settings(allowed_roots=(root,), cache_dir=tmp_path / "cache")


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KicadCli:
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setenv("FAKE_NETLIST", str(NETLIST))
    monkeypatch.setenv("FAKE_ERC_REPORT", str(ERC / "interf_u.json"))
    monkeypatch.setenv("FAKE_ERC_EXIT", "5")
    return KicadCli((sys.executable, str(FAKE), "tools"), temp_root=temp)


@pytest.fixture
def workspace(settings: Settings, fake_cli: KicadCli) -> Workspace:
    return Workspace(settings, cli=fake_cli)


def demo(root: Path) -> str:
    return str(root / "demo" / "demo.kicad_pro")


# Projects and paths


def test_projects_are_listed_under_the_allowed_roots(
    workspace: Workspace, root: Path
) -> None:
    (root / "empty").mkdir()
    (root / "empty" / "orphan.kicad_pro").write_text("{}", encoding="utf-8")
    listed = workspace.list_projects()
    assert listed["allowed_roots"] == [str(root.resolve())]
    assert listed["projects"] == [
        {
            "project": str(root.resolve() / "demo" / "demo.kicad_pro"),
            "has_root_sheet": True,
        },
        {
            "project": str(root.resolve() / "empty" / "orphan.kicad_pro"),
            "has_root_sheet": False,
        },
    ]


def test_a_project_search_skips_hidden_package_and_backup_folders(
    workspace: Workspace, root: Path
) -> None:
    for folder in (
        ".history",
        "node_modules/pkg",
        "demo-backups",
        "#auto_saved_files#",
    ):
        (root / folder).mkdir(parents=True)
        (root / folder / "copy.kicad_pro").write_text("{}", encoding="utf-8")
    listed = workspace.list_projects()
    assert [p["project"] for p in listed["projects"]] == [
        str(root.resolve() / "demo" / "demo.kicad_pro")
    ]
    assert listed["searched_all"] is True
    assert "note" not in listed


def test_a_project_search_stops_at_its_budget_and_says_so(root: Path) -> None:
    for name in ("a", "b", "c"):
        (root / name).mkdir()
        (root / name / f"{name}.kicad_pro").write_text("{}", encoding="utf-8")
    everything, complete = find_projects([root], 100)
    assert complete and len(everything) == 4
    # The root holds a, b, c and demo; each folder holds one or two files.
    some, complete = find_projects([root], 6)
    assert not complete and len(some) < 4


@pytest.mark.parametrize("how", ["project file", "root sheet", "folder"])
def test_a_project_can_be_named_three_ways(
    workspace: Workspace, root: Path, how: str
) -> None:
    named = {
        "project file": root / "demo" / "demo.kicad_pro",
        "root sheet": root / "demo" / "demo.kicad_sch",
        "folder": root / "demo",
    }[how]
    result = workspace.read_schematic(str(named))
    assert result["root_sheet"] == str(root.resolve() / "demo" / "demo.kicad_sch")


def test_a_path_outside_the_allowed_roots_is_refused(
    workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.kicad_pro").write_text("{}", encoding="utf-8")
    (outside / "x.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    with pytest.raises(ToolError, match="E_PATH_NOT_ALLOWED"):
        workspace.read_schematic(str(outside / "x.kicad_pro"))
    # Climbing out of a root with .. is the same thing.
    with pytest.raises(ToolError, match="E_PATH_NOT_ALLOWED"):
        workspace.run_erc(str(tmp_path / "root" / ".." / "elsewhere"))


def test_something_that_is_not_a_project_is_refused(
    workspace: Workspace, root: Path
) -> None:
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    for named in (root / "notes.txt", root / "missing.kicad_pro", root):
        with pytest.raises(ToolError, match="E_PROJECT_NOT_FOUND"):
            workspace.read_schematic(str(named))


def test_no_kicad_is_an_error_that_says_where_it_looked(
    settings: Settings, root: Path, tmp_path: Path
) -> None:
    missing = Settings(
        settings.allowed_roots, settings.cache_dir, kicad_cli=tmp_path / "no-kicad-cli"
    )
    with pytest.raises(ToolError, match=r"E_KICAD_UNAVAILABLE.*no-kicad-cli"):
        Workspace(missing).run_erc(demo(root))


# Schematic tools


def test_read_schematic_pages_parts_and_nets(workspace: Workspace, root: Path) -> None:
    result = workspace.read_schematic(demo(root))
    assert result["written_by"] == "Eeschema 10.0.6"
    assert (result["components_total"], result["nets_total"]) == (67, 93)
    assert result["components"][0] == {
        "ref": "AE1",
        "value": "Antenna",
        "footprint": "footprints:2.4GHz-meander-antenna-v2",
        "lib_id": "Device:Antenna",
        "sheet": "/",
    }
    assert "nets" not in result
    nets = workspace.read_schematic(demo(root), what="nets")
    assert (nets["components_total"], nets["nets_total"]) == (67, 93)
    assert {"name": "/ANT", "pin_count": 3} in nets["nets"]
    assert len(nets["nets"]) == 93 <= PAGE_SIZE
    assert "components" not in nets
    with pytest.raises(ToolError, match=r"E_PAGE_OUT_OF_RANGE.*1 to 1"):
        workspace.read_schematic(demo(root), page=2)


def test_every_net_can_be_reached_when_nets_outnumber_parts(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 67 parts and 93 nets: at 40 a page the parts end on page 2 and the nets
    # on page 3. One page number shared by both lists used to refuse page 3,
    # which on a real board left 700 of 1062 nets out of reach.
    monkeypatch.setattr("kairo.server.PAGE_SIZE", 40)
    names: list[str] = []
    for page in (1, 2, 3):
        result = workspace.read_schematic(demo(root), what="nets", page=page)
        assert result["nets_pages"] == 3
        names += [net["name"] for net in result["nets"]]
    assert len(names) == len(set(names)) == 93
    with pytest.raises(ToolError, match=r"components has pages 1 to 2"):
        workspace.read_schematic(demo(root), page=3)


def test_trace_net_gives_every_pin(workspace: Workspace, root: Path) -> None:
    result = workspace.trace_net(demo(root), "/ANT")
    assert result["net"] == "/ANT"
    assert result["pins"] == [
        {"ref": "AE1", "pin": "1", "pin_name": "A_1", "pin_type": "input"},
        {"ref": "C22", "pin": "2", "pin_name": "", "pin_type": "passive"},
        {"ref": "R11", "pin": "2", "pin_name": "", "pin_type": "passive"},
    ]


def test_an_unknown_net_fails_with_the_closest_names(
    workspace: Workspace, root: Path
) -> None:
    with pytest.raises(ToolError, match=r"E_NET_UNKNOWN.*'/ANT'"):
        workspace.trace_net(demo(root), "ANT")


def test_project_summary_counts_and_names_power_nets(
    workspace: Workspace, root: Path
) -> None:
    summary = workspace.project_summary(demo(root))
    assert summary["sheets"] == ["/"]
    assert (summary["component_count"], summary["net_count"]) == (67, 93)
    assert summary["power_net_count"] == len(summary["power_nets"]) == 10
    assert {"GND", "VBUS", "VBAT"} <= set(summary["power_nets"])
    assert summary["erc"] == {
        "status": "findings",
        "passed": False,
        "reason": "0 errors, 1 warnings",
    }
    skipped = workspace.project_summary(demo(root), run_erc=False)
    assert skipped["erc"] == {"status": "not run", "passed": False}


def test_the_netlist_is_exported_again_only_when_a_sheet_changes(
    settings: Settings, fake_cli: KicadCli, root: Path, tmp_path: Path
) -> None:
    workspace = Workspace(settings, cli=fake_cli)
    assert workspace.read_schematic(demo(root))["components_total"] == 67
    # With kicad-cli now writing nothing, an answer can only come from memory.
    workspace = Workspace(settings, cli=fake_cli)
    workspace.read_schematic(demo(root))
    workspace._cli = KicadCli(  # pyright: ignore[reportPrivateUsage]
        (sys.executable, str(FAKE), "silent"), temp_root=tmp_path / "temp"
    )
    assert workspace.read_schematic(demo(root))["components_total"] == 67
    sheet = root / "demo" / "demo.kicad_sch"
    stat = sheet.stat()
    os.utime(sheet, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    with pytest.raises(ToolError, match="E_NETLIST_UNAVAILABLE"):
        workspace.read_schematic(demo(root))


def test_a_netlist_kairo_cannot_read_is_an_error(
    settings: Settings, root: Path, tmp_path: Path
) -> None:
    cli = KicadCli((sys.executable, str(FAKE), "garbage"), temp_root=tmp_path)
    with pytest.raises(ToolError, match="E_NETLIST_MALFORMED"):
        Workspace(settings, cli=cli).read_schematic(demo(root))


# ERC


def test_run_erc_separates_library_findings_and_is_not_a_pass(
    workspace: Workspace, root: Path
) -> None:
    result = workspace.run_erc(demo(root))
    assert (result["status"], result["passed"]) == ("findings", False)
    assert (result["errors"], result["warnings"]) == (0, 1)
    assert result["by_type"] == {"footprint_link_issues": 1}
    assert (result["design_findings"], result["library_findings"]) == (0, 1)
    assert result["violations"][0]["type"] == "footprint_link_issues"
    assert result["kicad_version"] == "10.0.6"


def test_a_clean_erc_is_the_only_pass(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ERC_REPORT", str(ERC / "pic_programmer.json"))
    monkeypatch.setenv("FAKE_ERC_EXIT", "0")
    result = workspace.run_erc(demo(root))
    assert (result["status"], result["passed"]) == ("clean", True)
    # The same report with the exit code KiCad gives for violations is not
    # trusted, and so is not a pass.
    monkeypatch.setenv("FAKE_ERC_EXIT", "5")
    result = workspace.run_erc(demo(root))
    assert (result["status"], result["passed"]) == ("malformed", False)


def test_paging_erc_reads_one_run_and_page_1_runs_kicad_again(
    workspace: Workspace, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ERC_REPORT", str(ERC / "multichannel.json"))
    first = workspace.run_erc(demo(root))
    assert (first["violations_total"], first["violations_pages"]) == (36, 2)
    # With kicad-cli now writing nothing, a page can only come from that run.
    workspace._cli = KicadCli(  # pyright: ignore[reportPrivateUsage]
        (sys.executable, str(FAKE), "silent"), temp_root=tmp_path / "temp"
    )
    second = workspace.run_erc(demo(root), page=2)
    assert len(second["violations"]) == 16
    only = workspace.run_erc(demo(root), violation_type="pin_not_connected")
    assert (only["violations_total"], only["by_type"]) == (1, first["by_type"])
    # Once a project file changes, that run no longer stands for the project.
    sheet = root / "demo" / "demo.kicad_sch"
    stat = sheet.stat()
    os.utime(sheet, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    with pytest.raises(ToolError, match=r"E_PAGE_OUT_OF_RANGE.*unavailable"):
        workspace.run_erc(demo(root), page=2)
    os.utime(sheet, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    # Page 1 with no type always asks KiCad, which here gives nothing.
    assert workspace.run_erc(demo(root))["status"] == "unavailable"
    with pytest.raises(ToolError, match="E_PAGE_OUT_OF_RANGE"):
        workspace.run_erc(demo(root), page=2)


def test_a_second_run_that_disagrees_on_unchanged_files_says_so(
    workspace: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # KiCad 10.0.6 gives different warning counts from run to run on some
    # boards. Two reports stand in for two such runs here.
    monkeypatch.setenv("FAKE_ERC_REPORT", str(ERC / "video.json"))
    monkeypatch.setenv("FAKE_ERC_EXIT", "5")
    first = workspace.run_erc(demo(root))
    assert not any("previous run" in note for note in first["notes"])
    assert not any(
        "previous run" in note for note in workspace.run_erc(demo(root))["notes"]
    )
    monkeypatch.setenv("FAKE_ERC_REPORT", str(ERC / "sonde_xilinx.json"))
    (note,) = [n for n in workspace.run_erc(demo(root))["notes"] if "previous" in n]
    assert "footprint_link_issues (3 then 2)" in note
    # After a file changes, a different count is no surprise.
    sheet = root / "demo" / "demo.kicad_sch"
    stat = sheet.stat()
    os.utime(sheet, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    monkeypatch.setenv("FAKE_ERC_REPORT", str(ERC / "video.json"))
    changed = workspace.run_erc(demo(root))
    assert not any("previous run" in note for note in changed["notes"])


def test_erc_errors_come_first_and_one_type_can_be_asked_for() -> None:
    text = (ERC / "multichannel.json").read_bytes().decode("utf-8")
    result = classify_erc_report(text)
    # Turned round, so the four errors are last in what KiCad is taken to say.
    result = dataclasses.replace(result, violations=result.violations[::-1])
    report = erc_report(result)
    assert [v["severity"] for v in report["violations"][:5]] == ["error"] * 4 + [
        "warning"
    ]
    assert report["violation_type"] is None
    only = erc_report(result, violation_type="power_pin_not_driven")
    assert only["violations_total"] == 3 == len(only["violations"])
    assert only["by_type"] == report["by_type"]
    assert only["violation_type"] == "power_pin_not_driven"
    with pytest.raises(ToolError, match=r"E_VIOLATION_TYPE_UNKNOWN.*pin_not_connected"):
        erc_report(result, violation_type="pin_not_driven")


def test_erc_violations_are_paged_but_counts_are_whole() -> None:
    # CopperBench's recorded report for the mikoto board: 295 warnings.
    result = classify_erc_report(REAL_ERC.read_bytes().decode("utf-8"))
    first = erc_report(result)
    assert len(first["violations"]) == 20
    assert first["violations_total"] == sum(first["by_type"].values()) == 295
    assert first["violations_pages"] == 15
    assert len(erc_report(result, page=15)["violations"]) == 15
    with pytest.raises(ToolError, match="E_PAGE_OUT_OF_RANGE"):
        erc_report(result, page=16)


def test_lengths_in_descriptions_are_flagged_for_kicad_before_10_0_7() -> None:
    text = REAL_ERC.read_bytes().decode("utf-8")
    assert '"kicad_version": "10.0.6"' in text
    (note,) = erc_report(classify_erc_report(text))["notes"]
    assert "one hundredth" in note and "25582" in note
    fixed = classify_erc_report(text.replace('"10.0.6"', '"10.0.7"'))
    assert erc_report(fixed)["notes"] == []
    newer = classify_erc_report(text.replace('"10.0.6"', '"11.0.0"'))
    assert erc_report(newer)["notes"] == []
    unknown = classify_erc_report(text.replace('"10.0.6"', '"nightly"'))
    assert "not known" in erc_report(unknown)["notes"][0]


# Drawing


def test_render_puts_drawings_in_the_cache_not_the_project(
    workspace: Workspace, root: Path, settings: Settings
) -> None:
    before = sorted(p.name for p in (root / "demo").iterdir())
    result = workspace.render_schematic(demo(root))
    folder = settings.cache_dir / "renders" / "demo"
    assert result["files"] == [str(folder / "demo-Power.svg"), str(folder / "demo.svg")]
    assert sorted(p.name for p in (root / "demo").iterdir()) == before


# Libraries


@pytest.fixture
def library_workspace(settings: Settings, tmp_path: Path) -> Workspace:
    path = tmp_path / "Reg.kicad_sym"
    path.write_text(library(REG, DERIVED), encoding="utf-8")
    index = LibraryIndex()
    assert index.update([Library("symbol", "Reg", path)]).problems == ()
    return Workspace(settings, index=index)


def test_get_symbol_pins_gives_names_numbers_and_types_without_coordinates(
    library_workspace: Workspace,
) -> None:
    result = library_workspace.get_symbol_pins("Reg:REG-A")
    assert result["extends"] == "REG"
    assert result["unit_count"] == 2
    assert result["pins"] == [
        {
            "number": "3",
            "name": "VI",
            "type": "power_in",
            "unit": 1,
            "hidden": False,
            "alternates": [],
        },
        {
            "number": "2",
            "name": "VO",
            "type": "power_out",
            "unit": 1,
            "hidden": False,
            "alternates": ["SENSE"],
        },
        {
            "number": "1",
            "name": "GND",
            "type": "power_in",
            "unit": 1,
            "hidden": True,
            "alternates": [],
        },
        {
            "number": "4",
            "name": "",
            "type": "passive",
            "unit": 2,
            "hidden": False,
            "alternates": [],
        },
    ]


def test_an_unknown_symbol_fails_with_the_closest_names(
    library_workspace: Workspace,
) -> None:
    with pytest.raises(ToolError, match=r"E_SYMBOL_UNKNOWN.*'Reg:REG-A'"):
        library_workspace.get_symbol_pins("Reg:REG-AA")
    with pytest.raises(ToolError, match=r"E_SYMBOL_UNKNOWN.*No library.*'Reg'"):
        library_workspace.get_symbol_pins("Regs:REG")


def test_a_search_that_finds_nothing_says_which_word_was_in_the_way(
    library_workspace: Workspace,
) -> None:
    # A local model searched for the same unmatched words 20 times: an
    # empty list did not tell it which word the library lacks.
    found = library_workspace.find_symbol("first nonsense variant")
    assert [s["lib_id"] for s in found["symbols"]] == ["Reg:REG-A"]
    assert "every word but 'nonsense'" in found["notes"][0]
    nothing = library_workspace.find_symbol("utter nonsense")
    assert nothing["symbols"] == []
    assert "nor all but one" in nothing["notes"][0]
    # A search that finds something whole says nothing more.
    assert not any(
        "every word" in note
        for note in library_workspace.find_symbol("first variant")["notes"]
    )


def test_a_search_finds_a_part_by_the_name_it_gave_and_by_parts_of_a_name(
    library_workspace: Workspace,
) -> None:
    # A local model searched for a footprint by the very lib_id a search had
    # just given it, and for LED_SMD_0603, and found nothing either way.
    exact = library_workspace.find_symbol("Reg:REG-A")
    assert [s["lib_id"] for s in exact["symbols"]] == ["Reg:REG-A"]
    assert library_workspace.find_symbol("Reg:REG")["symbols"][0]["lib_id"] == (
        "Reg:REG"
    )
    joined = library_workspace.find_symbol("Reg_REG-A")
    assert "Reg:REG-A" in [s["lib_id"] for s in joined["symbols"]]
    assert "each of its parts" in joined["notes"][0]


def test_find_symbol_returns_summaries_and_checks_the_limit(
    library_workspace: Workspace,
) -> None:
    found = library_workspace.find_symbol("first variant")
    assert [s["lib_id"] for s in found["symbols"]] == ["Reg:REG-A"]
    assert found["symbols"][0]["pin_count"] == 4
    with pytest.raises(ToolError, match="E_LIMIT_OUT_OF_RANGE"):
        library_workspace.find_symbol("reg", limit=0)
    with pytest.raises(ToolError, match="E_LIMIT_OUT_OF_RANGE"):
        library_workspace.find_footprint("reg", limit=51)


# The server itself


def test_a_client_can_call_a_tool_and_read_an_error(
    settings: Settings, root: Path
) -> None:
    async def talk() -> tuple[Any, Any]:
        async with Client(build_server(settings)) as client:
            listed = await client.call_tool("list_projects", {})
            refused = await client.call_tool("read_schematic", {"project": "/nowhere"})
            return listed, refused

    listed, refused = asyncio.run(talk())
    assert not listed.is_error
    assert listed.structured_content["projects_total"] == 1
    assert refused.is_error
    # A refusal says what clears it, as data a model need not parse.
    assert refused.structured_content["ok"] is False
    assert refused.structured_content["code"] == "E_PATH_NOT_ALLOWED"
    assert "nowhere is not under an allowed root" in refused.structured_content["error"]
    assert "list_projects" in refused.structured_content["fix"]
    assert refused.structured_content["retryable"] is False
    assert json.loads(refused.content[0].text) == refused.structured_content
    # The text is the same data once, with no indentation to pay for.
    (content,) = listed.content
    assert "\n" not in content.text and '": ' not in content.text
    assert json.loads(content.text) == listed.structured_content


def test_a_long_call_tells_the_client_it_is_still_working(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(self: Workspace, page: int = 1) -> dict[str, Any]:
        progress.stage("looking through the roots")
        time.sleep(0.5)
        return {"projects": [], "page": page}

    monkeypatch.setattr(progress, "INTERVAL_S", 0.1)
    monkeypatch.setattr(Workspace, "list_projects", slow)
    heard: list[tuple[float, str | None]] = []

    async def note(progress: float, total: float | None, message: str | None) -> None:
        heard.append((progress, message))

    async def talk() -> Any:
        async with Client(build_server(settings)) as client:
            return await client.call_tool("list_projects", {}, progress_callback=note)

    result = asyncio.run(talk())
    assert not result.is_error
    assert len(heard) >= 2
    assert [done for done, _ in heard] == sorted({done for done, _ in heard})
    assert all(
        message and message.startswith("list_projects: looking through the roots (")
        for _, message in heard
    )


def test_a_tool_advertises_only_its_own_arguments(settings: Settings) -> None:
    async def talk() -> Any:
        async with Client(build_server(settings)) as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(talk()).tools}
    assert set(tools["list_projects"].input_schema["properties"]) == {"page"}
    assert all(
        "ctx" not in t.input_schema.get("properties", {}) for t in tools.values()
    )


def test_the_cache_folder_follows_the_platform(tmp_path: Path) -> None:
    if sys.platform == "win32":
        assert default_cache_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "kairo"
    else:
        assert (
            default_cache_dir({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "kairo"
        )
        assert default_cache_dir({}) == Path.home() / ".cache" / "kairo"


# Opening a project in the KiCad window


def test_a_project_is_opened_in_kicad_unless_it_is_open_already(
    settings: Settings, fake_cli: KicadCli, root: Path
) -> None:
    started: list[Path] = []
    workspace = Workspace(settings, cli=fake_cli, opener=started.append)
    project = root / "demo" / "demo.kicad_pro"
    first = workspace.open_in_kicad(demo(root))
    assert first["opened"] is True and started == [project]
    assert "does not reload" in first["notes"][1]
    # KiCad's own lock file, as KiCad 10.0.6 writes it.
    gui.lock_file(project).write_text(
        '{"hostname":"BENCH","username":"josh"}', encoding="utf-8"
    )
    again = workspace.open_in_kicad(demo(root))
    assert again["opened"] is False and again["already_open"] is True
    assert "josh on BENCH" in again["notes"][0]
    assert started == [project]


def test_opening_in_kicad_says_what_is_missing(
    settings: Settings, fake_cli: KicadCli, root: Path
) -> None:
    # The stand-in kicad-cli has no KiCad program beside it.
    with pytest.raises(ToolError, match="E_KICAD_GUI_NOT_FOUND"):
        Workspace(settings, cli=fake_cli).open_in_kicad(demo(root))
    (root / "demo" / "demo.kicad_pro").unlink()
    with pytest.raises(ToolError, match="E_NO_PROJECT_FILE"):
        Workspace(settings, cli=fake_cli).open_in_kicad(
            str(root / "demo" / "demo.kicad_sch")
        )


def test_kicad_is_looked_for_beside_kicad_cli(tmp_path: Path) -> None:
    cli = tmp_path / "kicad-cli.exe"
    assert gui.find_kicad(cli) is None
    (tmp_path / "kicad.exe").write_bytes(b"")
    assert gui.find_kicad(cli) == tmp_path / "kicad.exe"
    assert gui.find_kicad(tmp_path / "kicad-cli") is None
    assert gui.lock_file(tmp_path / "a.kicad_pro").name == "~a.kicad_pro.lck"
    assert gui.holder(tmp_path / "a.kicad_pro") is None
    gui.lock_file(tmp_path / "a.kicad_pro").write_text("not json", encoding="utf-8")
    assert gui.holder(tmp_path / "a.kicad_pro") == "someone"


# A model in a loop


def test_a_looping_client_is_told_to_stop(settings: Settings, root: Path) -> None:
    limited = dataclasses.replace(settings, calls_per_ten_minutes=8)

    async def talk() -> list[Any]:
        async with Client(build_server(limited)) as client:
            out = [await client.call_tool("list_projects", {}) for _ in range(5)]
            # A call that fails the same way each time is a loop as well.
            out += [
                await client.call_tool("read_schematic", {"project": "/nowhere"})
                for _ in range(3)
            ]
            out.append(await client.call_tool("find_symbol", {"query": "x"}))
            return out

    results = asyncio.run(talk())
    assert [r.is_error for r in results[:3]] == [False, False, False]
    for refused in results[3:5]:
        assert refused.is_error
        assert "E_REPEATED_CALL" in refused.content[0].text
        assert "tell the user" in refused.content[0].text
    # Eight calls were counted, so the ninth of any kind is refused.
    assert all("E_PATH_NOT_ALLOWED" in r.content[0].text for r in results[5:8])
    assert "E_TOO_MANY_CALLS" in results[8].content[0].text


def test_every_call_and_refusal_is_in_the_log_the_model_reads(
    settings: Settings, root: Path
) -> None:
    async def talk() -> Any:
        async with Client(build_server(settings)) as client:
            await client.call_tool("list_projects", {})
            await client.call_tool("read_schematic", {"project": "/nowhere"})
            return await client.call_tool("recent_calls", {"failed_only": True})

    answer = asyncio.run(talk()).structured_content
    (refused,) = answer["calls"]
    assert refused["tool"] == "read_schematic" and refused["outcome"] == "refused"
    assert refused["code"] == "E_PATH_NOT_ALLOWED"
    assert "/nowhere" in refused["arguments"]
    assert answer["by_tool"]["list_projects"]["calls"] == 1
    assert answer["by_tool"]["read_schematic"]["failures"] == {"E_PATH_NOT_ALLOWED": 1}
    assert (settings.cache_dir / "calls.jsonl").is_file()


def test_server_info_says_which_build_serves_and_what_it_found(
    settings: Settings,
) -> None:
    async def talk() -> Any:
        async with Client(build_server(settings)) as client:
            return await client.call_tool("server_info", {})

    info = asyncio.run(talk()).structured_content
    assert info["package"].endswith("kairo")
    assert info["restart_needed"] is False
    assert info["extension"] == {"status": "not_an_extension"}
    assert info["tier"] == settings.tier
    assert set(info["programs"]) == {"kicad_cli", "ngspice", "xyce"}
