"""The S-expression parser on files KiCad wrote: the demos and libraries in the install.

Every schematic, worksheet, rules file and library table in the install is
round-tripped, with a fixed sample of the much larger symbol libraries,
footprints and boards. Set ``KAIRO_FULL_CORPUS=1`` to round-trip all of
them (about 16,000 files and 670 MB on KiCad 10.0.6; a minute or two).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kairo.kicad.formats import (
    SCHEMATIC,
    SYMBOL_LIBRARY,
    UnsupportedFormatError,
    check_format,
)
from kairo.kicad.project import parse_project
from kairo.kicad.sexpr import SexprError, parse, parse_file
from kairo.process import run_process

pytestmark = pytest.mark.kicad

SUFFIXES = {
    ".kicad_sch",
    ".kicad_sym",
    ".kicad_mod",
    ".kicad_pcb",
    ".kicad_wks",
    ".kicad_dru",
}
TABLES = {"sym-lib-table", "fp-lib-table", "design-block-lib-table"}
# Sampled unless the full corpus is asked for: every Nth file of the kind.
SAMPLE_EVERY = {".kicad_sym": 10, ".kicad_mod": 100, ".kicad_pcb": 10}

# A demo footprint whose parentheses do not balance (line 198 reads
# "(curved_edges no)filter_ratio 0.9)"). KiCad 10.0.6 still loads it, which
# suggests its reader stops at the first complete list; Kairo refuses it.
KNOWN_MALFORMED = {
    "demos/royalblue54L_feather/lib/footprints/"
    "RoyalBlue54L-Feather-Connector_FFC-FPC.pretty/"
    "CONN-SMD_5P-P0.50_HCTL_HC-FPC-05-09-5RLTAG.kicad_mod"
}


@pytest.fixture(scope="module")
def share(kicad_demos: Path) -> Path:
    return kicad_demos.parent


def corpus(share: Path) -> list[Path]:
    full = os.environ.get("KAIRO_FULL_CORPUS") == "1"
    by_kind: dict[str, list[Path]] = {}
    for path in sorted(share.rglob("*")):
        if path.is_file() and (path.suffix in SUFFIXES or path.name in TABLES):
            by_kind.setdefault(path.suffix or path.name, []).append(path)
    chosen: list[Path] = []
    for kind, paths in by_kind.items():
        step = 1 if full else SAMPLE_EVERY.get(kind, 1)
        chosen.extend(paths[::step])
    return chosen


def test_kicad_files_round_trip_byte_for_byte(share: Path) -> None:
    files = corpus(share)
    assert len(files) > 100
    differ: list[str] = []
    refused: list[str] = []
    for path in files:
        relative = path.relative_to(share).as_posix()
        data = path.read_bytes()
        try:
            printed = parse(data.decode("utf-8")).to_text().encode("utf-8")
        except SexprError:
            if relative not in KNOWN_MALFORMED:
                refused.append(relative)
            continue
        if printed != data:
            differ.append(relative)
    assert refused == []
    assert differ == []


def test_the_known_malformed_footprint_is_refused(share: Path) -> None:
    for relative in KNOWN_MALFORMED:
        path = share / relative
        if not path.exists():
            pytest.skip(f"{relative} is not in this install")
        with pytest.raises(SexprError, match="unmatched"):
            parse_file(path)


def test_demo_schematic_versions_are_read_or_refused_clearly(share: Path) -> None:
    accepted: set[int] = set()
    for path in sorted((share / "demos").rglob("*.kicad_sch")):
        doc = parse_file(path)
        try:
            accepted.add(check_format(doc, SCHEMATIC))
        except UnsupportedFormatError as exc:
            assert "format" in str(exc) and "Kairo reads" in str(exc)
    # The demos include files from released KiCad 9 and 10 builds.
    assert 20250114 in accepted
    assert 20260101 in accepted


def test_kicad_10_symbol_libraries_are_read(share: Path) -> None:
    libraries = sorted((share / "symbols").glob("*.kicad_sym"))[::25]
    assert libraries
    for path in libraries:
        assert check_format(parse_file(path), SYMBOL_LIBRARY) == 20251024


def test_a_schematic_kicad_re_saves_round_trips(
    kicad_cli_path: Path, kicad_demos: Path, tmp_path: Path
) -> None:
    project = tmp_path / "pic_programmer"
    shutil.copytree(kicad_demos / "pic_programmer", project)
    schematic = project / "pic_programmer.kicad_sch"
    result = run_process(
        [str(kicad_cli_path), "sch", "upgrade", "--force", str(schematic)],
        timeout_s=120,
        cwd=project,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = schematic.read_bytes()
    doc = parse(data.decode("utf-8"))
    assert check_format(doc, SCHEMATIC) == 20260306
    assert doc.to_text().encode("utf-8") == data


def test_project_files_round_trip_byte_for_byte(share: Path) -> None:
    projects = sorted(share.rglob("*.kicad_pro"))
    assert projects
    for path in projects:
        data = path.read_bytes()
        assert parse_project(data.decode("utf-8")).to_text().encode("utf-8") == data
