from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from kairo.kicad import cli as kicad_cli
from kairo.kicad.cli import (
    Discovery,
    ErcStatus,
    KicadCli,
    KicadVersion,
    NetlistStatus,
    RenderStatus,
    classify_erc_report,
    find_kicad_cli,
    run_erc,
)
from kairo.process import process_alive

TESTS = Path(__file__).resolve().parent.parent
ERC = TESTS / "fixtures" / "kicad10" / "erc"
FAKE = TESTS / "fakes" / "fake_kicad_cli.py"


def fixture_text(name: str) -> str:
    return (ERC / f"{name}.json").read_text(encoding="utf-8")


def fixture_data(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(fixture_text(name))
    return data


def fake(mode: str, temp_root: Path) -> KicadCli:
    return KicadCli((sys.executable, str(FAKE), mode), temp_root=temp_root)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "demo.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    return folder / "demo.kicad_sch"


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "temp"
    root.mkdir()
    return root


# Version


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.0.6\n", (10, 0, 6)),
        ("9.0.1", (9, 0, 1)),
        ("10.0.7-rc2\n", (10, 0, 7)),
    ],
)
def test_version_parses(text: str, expected: tuple[int, int, int]) -> None:
    version = KicadVersion.parse(text)
    assert version is not None
    assert (version.major, version.minor, version.patch) == expected
    assert version.raw == text.strip()


@pytest.mark.parametrize("text", ["", "KiCad", "10.0", "\n\n"])
def test_version_refuses_what_is_not_a_version(text: str) -> None:
    assert KicadVersion.parse(text) is None


# Classifying real reports


def test_clean_report_is_clean() -> None:
    result = classify_erc_report(fixture_text("pic_programmer"), exit_code=0)
    assert result.status is ErcStatus.CLEAN
    assert result.reason == "no violations"
    assert result.kicad_version == "10.0.6"
    assert "footprint_filter" in result.ignored_checks


def test_multi_sheet_clean_report_is_clean() -> None:
    result = classify_erc_report(fixture_text("complex_hierarchy"), exit_code=0)
    assert result.status is ErcStatus.CLEAN


def test_warning_report_has_findings() -> None:
    result = classify_erc_report(fixture_text("interf_u"), exit_code=5)
    assert result.status is ErcStatus.FINDINGS
    assert (result.error_count, result.warning_count) == (0, 1)
    assert result.reason == "0 errors, 1 warnings"
    violation = result.violations[0]
    assert violation.type == "footprint_link_issues"
    assert violation.sheet == "/"
    assert violation.items[0].description == "Symbol P1 [DB25_Female]"
    assert violation.items[0].x == pytest.approx(3.8227)


def test_error_report_counts_errors_and_warnings() -> None:
    result = classify_erc_report(fixture_text("multichannel"), exit_code=5)
    assert result.status is ErcStatus.FINDINGS
    assert (result.error_count, result.warning_count) == (4, 32)
    types = {v.type for v in result.violations if v.severity == "error"}
    assert types == {"power_pin_not_driven", "pin_not_connected"}


def test_violations_on_many_sheets_are_all_read() -> None:
    result = classify_erc_report(fixture_text("video"), exit_code=5)
    assert result.warning_count == 3


def test_saved_report_classifies_without_an_exit_code() -> None:
    assert classify_erc_report(fixture_text("interf_u")).status is ErcStatus.FINDINGS
    assert classify_erc_report(fixture_text("pic_programmer")).status is ErcStatus.CLEAN


def test_excluded_violations_do_not_make_findings() -> None:
    data = fixture_data("interf_u")
    data["sheets"][0]["violations"][0]["excluded"] = True
    result = classify_erc_report(json.dumps(data))
    assert result.status is ErcStatus.CLEAN
    assert result.excluded_count == 1
    assert result.reason == "no active violations (0 errors, 0 warnings, 1 excluded)"


# Malformed reports: each starts from a real one and breaks one thing.


def broken(change: str) -> str:
    data = fixture_data("interf_u")
    violation = data["sheets"][0]["violations"][0]
    match change:
        case "schema":
            data["$schema"] = "https://schemas.kicad.org/erc.v2.json"
        case "units":
            data["coordinate_units"] = "mils"
        case "no sheets":
            del data["sheets"]
        case "no version":
            del data["kicad_version"]
        case "severity":
            violation["severity"] = "info"
        case "position":
            violation["items"][0]["pos"]["x"] = True
        case "uuid":
            del violation["items"][0]["uuid"]
        case "excluded":
            violation["excluded"] = "yes"
        case "violations not a list":
            data["sheets"][0]["violations"] = {}
        case _:
            raise AssertionError(change)
    return json.dumps(data)


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "units",
        "no sheets",
        "no version",
        "severity",
        "position",
        "uuid",
        "excluded",
        "violations not a list",
    ],
)
def test_broken_report_is_malformed(change: str) -> None:
    result = classify_erc_report(broken(change), exit_code=5)
    assert result.status is ErcStatus.MALFORMED
    assert result.reason


def test_text_that_is_not_json_is_malformed() -> None:
    result = classify_erc_report("Found 0 violations", exit_code=0)
    assert result.status is ErcStatus.MALFORMED


def test_exit_zero_with_violations_is_malformed() -> None:
    result = classify_erc_report(fixture_text("interf_u"), exit_code=0)
    assert result.status is ErcStatus.MALFORMED


def test_exit_five_with_no_violations_is_malformed() -> None:
    result = classify_erc_report(fixture_text("pic_programmer"), exit_code=5)
    assert result.status is ErcStatus.MALFORMED


def test_unexpected_exit_code_is_malformed() -> None:
    result = classify_erc_report(fixture_text("pic_programmer"), exit_code=1)
    assert result.status is ErcStatus.MALFORMED


# Finding kicad-cli


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


CLI_NAME = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"


def test_explicit_path_wins(tmp_path: Path) -> None:
    exe = make_executable(tmp_path / CLI_NAME)
    assert find_kicad_cli(exe, environ={}).path == exe


def test_missing_explicit_path_stops_the_search(tmp_path: Path) -> None:
    on_path = make_executable(tmp_path / "bin" / CLI_NAME)
    found = find_kicad_cli(tmp_path / "nope", environ={"PATH": str(on_path.parent)})
    assert found.path is None


def test_environment_variable_is_used(tmp_path: Path) -> None:
    exe = make_executable(tmp_path / CLI_NAME)
    assert find_kicad_cli(environ={kicad_cli.ENV_VAR: str(exe)}).path == exe


def test_missing_environment_path_does_not_fall_back_to_path(tmp_path: Path) -> None:
    on_path = make_executable(tmp_path / "bin" / CLI_NAME)
    environ = {kicad_cli.ENV_VAR: str(tmp_path / "gone"), "PATH": str(on_path.parent)}
    found = find_kicad_cli(environ=environ)
    assert found.path is None
    assert kicad_cli.ENV_VAR in found.searched[0]


def test_found_on_path(tmp_path: Path) -> None:
    exe = make_executable(tmp_path / "bin" / CLI_NAME)
    environ = {"PATH": str(exe.parent), "PATHEXT": os.environ.get("PATHEXT", ".EXE")}
    found = find_kicad_cli(environ=environ)
    assert found.path is not None
    assert found.path.resolve() == exe.resolve()


def test_no_kicad_means_unavailable_never_clean(
    monkeypatch: pytest.MonkeyPatch, project: Path
) -> None:
    def find_nothing(
        explicit: Path | None = None, *, temp_root: Path | None = None
    ) -> tuple[KicadCli | None, Discovery]:
        return None, Discovery(None, ("PATH",))

    monkeypatch.setattr(KicadCli, "find", staticmethod(find_nothing))
    result = run_erc(project)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "not found" in result.reason
    assert "PATH" in result.reason


# Running a stand-in kicad-cli


def test_run_classifies_the_report(project: Path, temp_root: Path) -> None:
    cli = fake(f"report:{ERC / 'multichannel.json'}:5", temp_root)
    result = cli.run_erc(project)
    assert result.status is ErcStatus.FINDINGS
    assert result.error_count == 4
    assert result.process is not None
    argv = result.process.argv
    for flag in ("--format", "json", "--severity-all", "--exit-code-violations"):
        assert flag in argv
    assert argv[argv.index("--units") + 1] == "mm"
    assert list(temp_root.iterdir()) == []


def test_run_notes_a_version_mismatch(
    project: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_KICAD_VERSION", "10.0.7")
    result = fake(f"report:{ERC / 'pic_programmer.json'}:0", temp_root).run_erc(project)
    assert result.status is ErcStatus.CLEAN
    assert any("10.0.6" in note and "10.0.7" in note for note in result.notes)


def test_garbage_report_is_malformed(project: Path, temp_root: Path) -> None:
    result = fake("garbage", temp_root).run_erc(project)
    assert result.status is ErcStatus.MALFORMED
    assert list(temp_root.iterdir()) == []


def test_no_report_is_unavailable(project: Path, temp_root: Path) -> None:
    result = fake("silent", temp_root).run_erc(project)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "no report" in result.reason


def test_load_failure_is_unavailable(project: Path, temp_root: Path) -> None:
    result = fake("load-fail", temp_root).run_erc(project)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "could not load" in result.reason
    assert "Failed to load schematic" in result.reason


def test_old_kicad_is_unavailable(
    project: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_KICAD_VERSION", "8.0.4")
    result = fake(f"report:{ERC / 'pic_programmer.json'}:0", temp_root).run_erc(project)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "older" in result.reason


def test_unreadable_version_is_unavailable(
    project: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_KICAD_VERSION", "KiCad")
    result = fake(f"report:{ERC / 'pic_programmer.json'}:0", temp_root).run_erc(project)
    assert result.status is ErcStatus.UNAVAILABLE


def test_missing_executable_is_unavailable(project: Path, temp_root: Path) -> None:
    cli = KicadCli(("kairo-no-such-kicad-cli",), temp_root=temp_root)
    assert cli.run_erc(project).status is ErcStatus.UNAVAILABLE


def test_missing_schematic_is_unavailable(tmp_path: Path, temp_root: Path) -> None:
    cli = fake(f"report:{ERC / 'pic_programmer.json'}:0", temp_root)
    result = cli.run_erc(tmp_path / "absent.kicad_sch")
    assert result.status is ErcStatus.UNAVAILABLE
    assert "not found" in result.reason


def wait_for_dead(pids: list[int]) -> list[int]:
    deadline = time.monotonic() + 10
    alive = pids
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if process_alive(pid)]
        time.sleep(0.05)
    return alive


def test_timeout_leaves_no_process_temp_folder_or_lock(
    project: Path, temp_root: Path
) -> None:
    result = fake("hang", temp_root).run_erc(project, timeout_s=3)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "timed out" in result.reason
    folder = project.parent
    pids = [int(p) for p in (folder / "pids.txt").read_text(encoding="utf-8").split()]
    assert wait_for_dead(pids) == []
    assert list(temp_root.iterdir()) == []
    assert list(folder.glob("~*.lck")) == []
    assert any("stale lock" in note for note in result.notes)


def test_timeout_keeps_a_lock_that_was_there_before(
    project: Path, temp_root: Path
) -> None:
    lock = project.parent / "~demo.kicad_pro.lck"
    lock.write_text('{"hostname": "OTHER", "username": "someone"}', encoding="utf-8")
    fake("hang", temp_root).run_erc(project, timeout_s=3)
    assert lock.exists()


def test_timeout_keeps_a_new_lock_naming_someone_else(
    project: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOCK_USER", "someone-else")
    fake("hang", temp_root).run_erc(project, timeout_s=3)
    assert (project.parent / "~demo.kicad_pro.lck").exists()


# Netlist export


def test_netlist_is_returned_as_written(project: Path, temp_root: Path) -> None:
    source = ERC / "pic_programmer.json"
    result = fake(f"report:{source}:0", temp_root).export_netlist(project)
    assert result.status is NetlistStatus.OK
    assert result.text == source.read_bytes().decode("utf-8")
    assert result.process is not None
    argv = result.process.argv
    assert argv[argv.index("--format") + 1] == "kicadsexpr"
    assert list(temp_root.iterdir()) == []


def test_netlist_failure_is_unavailable(project: Path, temp_root: Path) -> None:
    source = ERC / "pic_programmer.json"
    result = fake(f"report:{source}:1", temp_root).export_netlist(project)
    assert result.status is NetlistStatus.UNAVAILABLE
    assert result.text is None


def test_netlist_with_no_output_is_unavailable(project: Path, temp_root: Path) -> None:
    result = fake("silent", temp_root).export_netlist(project)
    assert result.status is NetlistStatus.UNAVAILABLE


def test_unknown_netlist_format_is_refused(project: Path, temp_root: Path) -> None:
    with pytest.raises(ValueError):
        fake("silent", temp_root).export_netlist(project, fmt="eagle")


def test_netlist_keeps_the_line_endings_kicad_wrote(
    project: Path, temp_root: Path, tmp_path: Path
) -> None:
    source = tmp_path / "netlist.net"
    source.write_bytes(b"(export\r\n  (version E))\r\n")
    result = fake(f"report:{source}:0", temp_root).export_netlist(project)
    assert result.text == "(export\r\n  (version E))\r\n"


def test_report_that_is_not_utf8_is_unavailable(
    project: Path, temp_root: Path, tmp_path: Path
) -> None:
    source = tmp_path / "erc.json"
    source.write_bytes(b'{"source": "caf\xe9"}')
    result = fake(f"report:{source}:0", temp_root).run_erc(project)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "could not read" in result.reason


def test_timeout_keeps_the_lock_when_the_user_cannot_be_named(
    project: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_user() -> str:
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(getpass, "getuser", no_user)
    result = fake("hang", temp_root).run_erc(project, timeout_s=3)
    assert result.status is ErcStatus.UNAVAILABLE
    assert (project.parent / "~demo.kicad_pro.lck").exists()


class _FakeKey:
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> _FakeKey:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows registry")
def test_a_registry_entry_without_a_location_does_not_hide_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "bin" / "kicad-cli.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    entries: dict[str, dict[str, str]] = {
        "KiCad 9.0": {},
        "KiCad 10.0": {"InstallLocation": str(tmp_path), "DisplayVersion": "10.0.6"},
    }

    def open_key(parent: object, name: str) -> _FakeKey:
        if isinstance(parent, _FakeKey):
            return _FakeKey(name)
        if parent == "HKLM" and "WOW6432Node" not in name:
            return _FakeKey("uninstall")
        raise FileNotFoundError(name)

    def query_value(key: _FakeKey, value: str) -> tuple[str, int]:
        if value not in entries[key.name]:
            raise FileNotFoundError(value)
        return entries[key.name][value], 1

    def enum_key(key: _FakeKey, index: int) -> str:
        return list(entries)[index]

    def query_info(key: _FakeKey) -> tuple[int, int, int]:
        return len(entries), 0, 0

    winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        HKEY_CURRENT_USER="HKCU",
        OpenKey=open_key,
        QueryInfoKey=query_info,
        EnumKey=enum_key,
        QueryValueEx=query_value,
    )
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    assert find_kicad_cli(environ={}).path == exe


# Things kicad-cli leaves in the project folder


def test_a_settings_file_kicad_cli_created_is_removed_and_noted(
    project: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_WRITES_SETTINGS", "1")
    source = ERC / "pic_programmer.json"
    result = fake(f"report:{source}:0", temp_root).run_erc(project)
    assert result.status is ErcStatus.CLEAN
    assert not (project.parent / "demo.kicad_prl").exists()
    assert any("demo.kicad_prl" in note for note in result.notes)


def test_a_settings_file_that_was_already_there_is_kept(
    project: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_WRITES_SETTINGS", "1")
    settings = project.parent / "demo.kicad_prl"
    settings.write_text('{"mine": true}', encoding="utf-8")
    source = ERC / "pic_programmer.json"
    result = fake(f"report:{source}:0", temp_root).run_erc(project)
    assert settings.exists()
    assert result.notes == ()


# Drawing


def test_svg_export_moves_one_file_per_sheet_into_the_folder(
    project: Path, temp_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "renders" / "demo"
    result = fake("svg", temp_root).export_svg(project, folder)
    assert result.status is RenderStatus.OK
    assert result.files == (folder / "demo-Power.svg", folder / "demo.svg")
    assert all(path.read_text(encoding="utf-8") == "<svg/>" for path in result.files)
    assert list(temp_root.iterdir()) == []
    # A second run replaces the files rather than failing on them.
    assert fake("svg", temp_root).export_svg(project, folder).files == result.files


def test_svg_export_with_nothing_drawn_is_unavailable(
    project: Path, temp_root: Path, tmp_path: Path
) -> None:
    result = fake("silent", temp_root).export_svg(project, tmp_path / "renders")
    assert result.status is RenderStatus.UNAVAILABLE
    assert result.files == ()


# Reports from real boards (see the README beside them)


def test_excluded_violations_in_a_real_report_are_read_and_not_counted() -> None:
    result = classify_erc_report(fixture_text("artix-dc-scm"), exit_code=5)
    assert result.status is ErcStatus.FINDINGS
    assert (result.error_count, result.warning_count) == (0, 1413)
    assert result.excluded_count == 15
    assert result.reason == "0 errors, 1413 warnings, 15 excluded"


def test_an_excluded_error_is_not_an_error() -> None:
    result = classify_erc_report(fixture_text("baseboard_4slots"), exit_code=5)
    assert (result.error_count, result.warning_count) == (1, 221)
    assert result.excluded_count == 3
    excluded = [v for v in result.violations if v.excluded]
    assert sorted(v.severity for v in excluded) == ["error", "error", "warning"]


def test_a_violation_of_a_switched_off_check_has_no_severity_and_is_read() -> None:
    result = classify_erc_report(fixture_text("polarfire-som"), exit_code=5)
    assert result.status is ErcStatus.FINDINGS
    assert "pin_to_pin" in result.ignored_checks
    (ignored,) = [v for v in result.violations if v.severity == "ignored"]
    assert (ignored.type, ignored.excluded) == ("pin_to_pin", True)
    assert (result.error_count, result.warning_count) == (0, 1331)
    assert result.excluded_count == 19


def test_an_empty_severity_on_a_check_that_is_on_is_malformed() -> None:
    data = fixture_data("polarfire-som")
    data["ignored_checks"] = [
        c for c in data["ignored_checks"] if c["key"] != "pin_to_pin"
    ]
    result = classify_erc_report(json.dumps(data))
    assert result.status is ErcStatus.MALFORMED
    assert "severity ''" in result.reason


def test_reported_positions_are_kept_as_written_not_called_mm() -> None:
    # In the schematic this label is at (189.23, 121.92) mm; kicad-cli 10.0.6
    # reports one hundredth of that while calling the units mm.
    result = classify_erc_report(fixture_text("artix-dc-scm"))
    label = next(
        item
        for violation in result.violations
        for item in violation.items
        if item.uuid == "a4d1354d-c475-439f-9b8c-1b4936c6193f"
    )
    assert (label.x, label.y) == (1.8923, 1.2192)
