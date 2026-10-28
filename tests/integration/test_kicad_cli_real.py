"""The kicad-cli runner against a real KiCad, on copies of KiCad's demo projects.

The demos are copied first, so kicad-cli's lock files never land in the
KiCad install.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from kairo.kicad.cli import ErcStatus, KicadCli, NetlistStatus
from kairo.process import process_alive

pytestmark = pytest.mark.kicad


@pytest.fixture
def cli(kicad_cli_path: Path, tmp_path: Path) -> KicadCli:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    return KicadCli((str(kicad_cli_path),), temp_root=temp_root)


def copy_demo(demos: Path, name: str, schematic: str, into: Path) -> Path:
    target = into / name
    shutil.copytree(demos / name, target)
    return target / schematic


def test_version_is_supported(cli: KicadCli) -> None:
    version = cli.version()
    assert version is not None
    assert version.major >= 9


def test_clean_demo_is_clean(cli: KicadCli, kicad_demos: Path, tmp_path: Path) -> None:
    sch = copy_demo(kicad_demos, "pic_programmer", "pic_programmer.kicad_sch", tmp_path)
    result = cli.run_erc(sch)
    assert result.status is ErcStatus.CLEAN, result.reason
    version = cli.version()
    assert version is not None
    assert result.kicad_version == version.raw
    assert list(sch.parent.glob("~*.lck")) == []


def test_demo_with_errors_has_findings(
    cli: KicadCli, kicad_demos: Path, tmp_path: Path
) -> None:
    sch = copy_demo(
        kicad_demos, "multichannel", "multichannel_mixer.kicad_sch", tmp_path
    )
    result = cli.run_erc(sch)
    assert result.status is ErcStatus.FINDINGS, result.reason
    # These errors come from the design itself, not from which footprint
    # libraries happen to be installed, so they appear on any machine.
    errors = {v.type for v in result.violations if v.severity == "error"}
    assert {"power_pin_not_driven", "pin_not_connected"} <= errors


def test_unloadable_schematic_is_unavailable(cli: KicadCli, tmp_path: Path) -> None:
    sch = tmp_path / "broken.kicad_sch"
    sch.write_text("this is not a schematic", encoding="utf-8")
    result = cli.run_erc(sch)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "could not load" in result.reason


def test_netlist_export(cli: KicadCli, kicad_demos: Path, tmp_path: Path) -> None:
    sch = copy_demo(kicad_demos, "pic_programmer", "pic_programmer.kicad_sch", tmp_path)
    result = cli.export_netlist(sch)
    assert result.status is NetlistStatus.OK, result.reason
    assert result.text is not None
    assert result.text.startswith("(export")


def test_forced_timeout_leaves_nothing_behind(
    cli: KicadCli, kicad_demos: Path, tmp_path: Path
) -> None:
    # This demo takes about 2 s; half a second is well inside that.
    sch = copy_demo(
        kicad_demos, "multichannel", "multichannel_mixer.kicad_sch", tmp_path
    )
    result = cli.run_erc(sch, timeout_s=0.5)
    assert result.status is ErcStatus.UNAVAILABLE
    assert "timed out" in result.reason
    assert result.process is not None
    deadline = time.monotonic() + 10
    while process_alive(result.process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_alive(result.process.pid)
    assert cli.temp_root is not None
    assert list(cli.temp_root.iterdir()) == []
    # KiCad takes its project lock within the first half second, so the
    # runner must have found and removed it, not merely found none.
    assert any("stale lock" in note for note in result.notes)
    assert list(sch.parent.glob("~*.lck")) == []
