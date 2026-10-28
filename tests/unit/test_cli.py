from __future__ import annotations

import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from kairo import __version__
from kairo.cli import build_parser, main, settings_from


def test_help_exits_zero_and_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert "usage: kairo" in capsys.readouterr().out


def test_version_matches_package_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"kairo {__version__}"


def test_with_no_arguments_the_current_folder_is_the_only_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = settings_from(build_parser().parse_args([]))
    assert settings.allowed_roots == (Path.cwd(),)
    assert settings.kicad_cli is None
    assert settings.cache_dir.name == "kairo"


def test_roots_cache_and_kicad_cli_can_be_given(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--allowed-root",
            str(tmp_path / "a"),
            "--allowed-root",
            str(tmp_path / "b"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--kicad-cli",
            str(tmp_path / "kicad-cli"),
        ]
    )
    settings = settings_from(args)
    assert settings.allowed_roots == (tmp_path / "a", tmp_path / "b")
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.kicad_cli == tmp_path / "kicad-cli"


def test_console_script_points_at_main() -> None:
    (script,) = entry_points(group="console_scripts", name="kairo")
    assert script.value == "kairo.cli:main"


def test_module_entry_runs_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kairo", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    assert "usage: kairo" in result.stdout
