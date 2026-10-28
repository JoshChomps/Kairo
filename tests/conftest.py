"""Shared test fixtures.

Tests marked ``kicad`` get the real kicad-cli from `kairo.kicad.cli`'s own
search and are skipped, with the places searched, when it is not found.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairo.kicad.cli import KicadCli

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def kicad_cli_path() -> Path:
    cli, discovery = KicadCli.find()
    if cli is None:
        pytest.skip(f"kicad-cli not found (searched: {'; '.join(discovery.searched)})")
    return Path(cli.command[0])


@pytest.fixture(scope="session")
def kicad_demos(kicad_cli_path: Path) -> Path:
    # Both the Windows install (<root>/bin/kicad-cli.exe) and Linux packages
    # (/usr/bin/kicad-cli) keep the demos at ../share/kicad/demos.
    demos = kicad_cli_path.parent.parent / "share" / "kicad" / "demos"
    if not demos.is_dir():
        pytest.skip(f"KiCad demo projects not found at {demos}")
    return demos
