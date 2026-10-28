"""The library index on KiCad's own libraries, as installed.

Every official symbol library is indexed, with a fixed sample of the
footprint libraries. Set ``KAIRO_FULL_CORPUS=1`` to index every footprint
library too.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kairo.kicad.cli import KicadCli
from kairo.library import (
    Library,
    LibraryIndex,
    Pad,
    Pin,
    install_variables,
    read_library_table,
)

pytestmark = pytest.mark.kicad

FOOTPRINT_SAMPLE_EVERY = 10


@pytest.fixture(scope="module")
def libraries(kicad_cli_path: Path, kicad_demos: Path) -> list[Library]:
    share = kicad_demos.parent
    version = KicadCli((str(kicad_cli_path),)).version()
    assert version is not None
    # The environment is left out so a variable set on this machine cannot
    # point the test at some other library set.
    variables = install_variables(share, version.major, {})
    symbols, symbol_problems = read_library_table(
        share / "template" / "sym-lib-table", "symbol", variables
    )
    footprints, footprint_problems = read_library_table(
        share / "template" / "fp-lib-table", "footprint", variables
    )
    assert symbol_problems == []
    assert footprint_problems == []
    assert len(symbols) > 200
    assert len(footprints) > 100
    if os.environ.get("KAIRO_FULL_CORPUS") != "1":
        wanted = {"Resistor_SMD", "Package_TO_SOT_SMD"}
        footprints = [
            lib
            for n, lib in enumerate(footprints)
            if n % FOOTPRINT_SAMPLE_EVERY == 0 or lib.nickname in wanted
        ]
    return [*symbols, *footprints]


@pytest.fixture(scope="module")
def index(libraries: list[Library]) -> LibraryIndex:
    built = LibraryIndex()
    report = built.update(libraries)
    assert report.problems == ()
    assert report.read > 300
    return built


def test_every_official_library_file_indexes_without_a_problem(
    index: LibraryIndex, libraries: list[Library]
) -> None:
    assert index.problems() == []
    assert index.update(libraries).read == 0


def test_a_resistor_has_exactly_its_two_pins(index: LibraryIndex) -> None:
    resistor = index.symbol("Device:R")
    assert resistor is not None
    assert resistor.pins == (
        Pin("1", "", "passive", "line", 1, 1, 0.0, 3.81, 270.0, 1.27, False),
        Pin("2", "", "passive", "line", 1, 1, 0.0, -3.81, 90.0, 1.27, False),
    )
    assert (resistor.reference, resistor.unit_count) == ("R", 1)


def test_a_derived_regulator_has_its_parents_pins(index: LibraryIndex) -> None:
    regulator = index.symbol("Regulator_Linear:AMS1117-3.3")
    assert regulator is not None
    assert regulator.extends is not None
    assert regulator.pins == (
        Pin("1", "GND", "power_in", "line", 1, 1, 0.0, -7.62, 90.0, 2.54, False),
        Pin("2", "VO", "power_out", "line", 1, 1, 7.62, 0.0, 180.0, 2.54, False),
        Pin("3", "VI", "power_in", "line", 1, 1, -7.62, 0.0, 0.0, 2.54, False),
    )
    assert regulator.footprint == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"


def test_a_multi_unit_symbol_keeps_its_units_apart(index: LibraryIndex) -> None:
    opamp = index.symbol("Amplifier_Operational:LM358")
    assert opamp is not None
    assert opamp.unit_count == 3
    by_unit = {
        unit: sorted(p.number for p in opamp.pins if p.unit == unit)
        for unit in (1, 2, 3)
    }
    assert by_unit == {1: ["1", "2", "3"], 2: ["5", "6", "7"], 3: ["4", "8"]}


def test_power_symbols_are_marked(index: LibraryIndex) -> None:
    ground = index.symbol("power:GND")
    assert ground is not None
    assert ground.is_power
    assert [p.electrical_type for p in ground.pins] == ["power_in"]


def test_a_footprints_pads_are_found(index: LibraryIndex) -> None:
    resistor = index.footprint("Resistor_SMD:R_0603_1608Metric")
    assert resistor is not None
    assert resistor.pads == (Pad("1", "smd", "roundrect"), Pad("2", "smd", "roundrect"))
    sot223 = index.footprint("Package_TO_SOT_SMD:SOT-223-3_TabPin2")
    assert sot223 is not None
    assert sorted(p.number for p in sot223.pads) == ["1", "2", "2", "3"]


def test_search_finds_a_known_symbol(index: LibraryIndex) -> None:
    assert index.find_symbols("AMS1117-3.3")[0].lib_id == "Regulator_Linear:AMS1117-3.3"
    found = index.find_symbols("ldo 3.3V sot-223", limit=50)
    assert "Regulator_Linear:AMS1117-3.3" in [s.lib_id for s in found]


def test_the_malformed_demo_footprint_is_reported_not_skipped(
    kicad_demos: Path,
) -> None:
    project = kicad_demos / "royalblue54L_feather"
    bad = "CONN-SMD_5P-P0.50_HCTL_HC-FPC-05-09-5RLTAG.kicad_mod"
    table = project / "fp-lib-table"
    if not table.is_file():
        pytest.skip("the royalblue54L_feather demo is not in this install")
    libraries, _ = read_library_table(table, "footprint", {"KIPRJMOD": str(project)})
    index = LibraryIndex()
    report = index.update(libraries)
    reasons = {p.path.name: p.reason for p in report.problems}
    assert "SexprError" in reasons[bad]
    assert report.read > len(report.problems)
