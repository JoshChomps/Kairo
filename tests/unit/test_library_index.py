from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from test_library_reader import DERIVED, FOOTPRINT, REG, library

from kairo.kicad.sexpr import parse
from kairo.library import Library, LibraryIndex, Pad, read_symbol_library


def symbol_library(folder: Path, name: str, *symbols: str) -> Library:
    path = folder / f"{name}.kicad_sym"
    path.write_text(library(*symbols), encoding="utf-8")
    return Library("symbol", name, path)


def footprint_library(folder: Path, name: str, *footprints: str) -> Library:
    path = folder / f"{name}.pretty"
    path.mkdir()
    for footprint in footprints:
        text = FOOTPRINT.replace("R_0603", footprint)
        (path / f"{footprint}.kicad_mod").write_text(text, encoding="utf-8")
    return Library("footprint", name, path)


def touch_later(path: Path) -> None:
    """Move a file's modification time on, as an edit would."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))


@pytest.fixture
def libs(tmp_path: Path) -> list[Library]:
    return [
        symbol_library(tmp_path, "Reg", REG, DERIVED),
        footprint_library(tmp_path, "Passive", "R_0603", "R_0805"),
    ]


def test_a_symbol_comes_back_exactly_as_the_reader_read_it(libs: list[Library]) -> None:
    index = LibraryIndex()
    report = index.update(libs)
    assert (report.read, report.unchanged, report.removed) == (3, 0, 0)
    assert report.problems == ()
    for expected in read_symbol_library(parse(libs[0].path.read_text("utf-8"))):
        assert index.symbol(f"Reg:{expected.name}") == expected


def test_unknown_names_are_none(libs: list[Library]) -> None:
    index = LibraryIndex()
    index.update(libs)
    assert index.symbol("Reg:NOPE") is None
    assert index.symbol("Other:REG") is None
    assert index.symbol("reg:REG") is None
    assert index.footprint("Passive:R_1206") is None


def test_a_footprint_comes_back_with_its_pads_in_order(libs: list[Library]) -> None:
    index = LibraryIndex()
    index.update(libs)
    footprint = index.footprint("Passive:R_0805")
    assert footprint is not None
    assert footprint.name == "R_0805"
    assert footprint.pads == (
        Pad("1", "smd", "roundrect"),
        Pad("2", "smd", "roundrect"),
        Pad("", "np_thru_hole", "circle"),
    )


def test_unchanged_files_are_not_read_again(libs: list[Library]) -> None:
    index = LibraryIndex()
    index.update(libs)
    report = index.update(libs)
    assert (report.read, report.unchanged, report.removed) == (0, 3, 0)


def test_a_changed_file_is_read_again(libs: list[Library]) -> None:
    index = LibraryIndex()
    index.update(libs)
    path = libs[0].path
    path.write_text(library(REG.replace('"VI"', '"VIN"')), encoding="utf-8")
    touch_later(path)
    report = index.update(libs)
    assert (report.read, report.unchanged) == (1, 2)
    reg = index.symbol("Reg:REG")
    assert reg is not None
    assert reg.pins[0].name == "VIN"
    # The symbols that were only in the old file are gone with it.
    assert index.symbol("Reg:REG-A") is None


def test_a_library_no_longer_listed_is_removed(libs: list[Library]) -> None:
    index = LibraryIndex()
    index.update(libs)
    report = index.update(libs[:1])
    assert (report.read, report.unchanged, report.removed) == (0, 1, 2)
    assert index.footprint("Passive:R_0603") is None
    assert index.symbol("Reg:REG") is not None


def test_a_deleted_footprint_file_is_removed(libs: list[Library]) -> None:
    index = LibraryIndex()
    index.update(libs)
    (libs[1].path / "R_0603.kicad_mod").unlink()
    assert index.update(libs).removed == 1
    assert index.footprint("Passive:R_0603") is None
    assert index.footprint("Passive:R_0805") is not None


def test_a_file_that_cannot_be_read_is_reported_and_the_rest_indexed(
    tmp_path: Path, libs: list[Library]
) -> None:
    bad = symbol_library(tmp_path, "Bad", '(symbol "X" (extends "Missing"))')
    unbalanced = Library("symbol", "Unbalanced", tmp_path / "u.kicad_sym")
    unbalanced.path.write_text("(kicad_symbol_lib (version 20251024)", encoding="utf-8")
    absent = Library("footprint", "Absent", tmp_path / "Absent.pretty")
    wrong_name = footprint_library(tmp_path, "Renamed", "R_0603")
    (wrong_name.path / "R_0603.kicad_mod").rename(wrong_name.path / "Other.kicad_mod")
    index = LibraryIndex()
    report = index.update([*libs, bad, unbalanced, absent, wrong_name])
    reasons = {p.nickname: p.reason for p in report.problems}
    assert "not in the library" in reasons["Bad"]
    assert "SexprError" in reasons["Unbalanced"]
    assert "Absent" in reasons
    assert "inside a file named 'Other'" in reasons["Renamed"]
    assert len(report.problems) == 4
    assert index.symbol("Bad:X") is None
    assert index.symbol("Reg:REG") is not None
    # The problems are still listed after an update that reads nothing new.
    again = index.update([*libs, bad, unbalanced, absent, wrong_name])
    assert again.read == 0
    assert again.removed == 0
    assert again.problems == report.problems


def test_a_repaired_file_stops_being_a_problem(tmp_path: Path) -> None:
    bad = symbol_library(tmp_path, "Lib", '(symbol "X" (extends "Missing"))')
    index = LibraryIndex()
    assert len(index.update([bad]).problems) == 1
    bad.path.write_text(library(REG), encoding="utf-8")
    touch_later(bad.path)
    assert index.update([bad]).problems == ()
    assert index.symbol("Lib:REG") is not None


def test_two_libraries_with_one_nickname_are_refused(tmp_path: Path) -> None:
    first = symbol_library(tmp_path, "Same", REG)
    second = Library("symbol", "Same", tmp_path / "elsewhere.kicad_sym")
    with pytest.raises(ValueError, match="'Same'"):
        LibraryIndex().update([first, second])


def test_the_index_is_kept_on_disk(tmp_path: Path, libs: list[Library]) -> None:
    database = tmp_path / "index.db"
    first = LibraryIndex(database)
    first.update(libs)
    first.close()
    second = LibraryIndex(database)
    assert second.symbol("Reg:REG-B") is not None
    assert second.update(libs).read == 0
    second.close()


def test_an_index_from_other_code_is_rebuilt(
    tmp_path: Path, libs: list[Library]
) -> None:
    database = tmp_path / "index.db"
    old = sqlite3.connect(database)
    old.execute("CREATE TABLE files (something_else TEXT)")
    old.execute("PRAGMA user_version = 999")
    old.commit()
    old.close()
    index = LibraryIndex(database)
    assert index.update(libs).read == 3
    index.close()


def test_worker_processes_give_the_same_index(tmp_path: Path) -> None:
    names = [f"R_{n:04d}" for n in range(40)]
    libs = [
        symbol_library(tmp_path, "Reg", REG, DERIVED),
        footprint_library(tmp_path, "Many", *names),
    ]
    alone, pooled = LibraryIndex(), LibraryIndex()
    assert alone.update(libs, workers=1).problems == ()
    assert pooled.update(libs, workers=2).problems == ()
    for name in ("REG", "REG-B", "GND"):
        assert pooled.symbol(f"Reg:{name}") == alone.symbol(f"Reg:{name}")
    for name in names:
        assert pooled.footprint(f"Many:{name}") == alone.footprint(f"Many:{name}")


def test_search_needs_every_word_and_puts_the_exact_name_first(
    tmp_path: Path, libs: list[Library]
) -> None:
    extra = symbol_library(
        tmp_path,
        "More",
        '(symbol "REG-LONGER" (property "Description" "Another regulator"))',
        '(symbol "RXG" (property "Description" "Not a regulator, a decoy"))',
    )
    index = LibraryIndex()
    index.update([*libs, extra])
    found = [s.lib_id for s in index.find_symbols("reg")]
    assert found[0] == "Reg:REG"
    assert set(found) == {
        "Reg:REG",
        "Reg:REG-A",
        "Reg:REG-B",
        "More:REG-LONGER",
        "More:RXG",
        # Matched by its library's name.
        "Reg:GND",
    }
    assert [s.lib_id for s in index.find_symbols("first variant")] == ["Reg:REG-A"]
    assert index.find_symbols("regulator nonsense") == []
    assert index.find_symbols("   ") == []
    assert len(index.find_symbols("reg", limit=2)) == 2
    summary = index.find_symbols("REG-B")[0]
    assert (summary.pin_count, summary.unit_count) == (4, 2)


def test_search_treats_wildcard_characters_as_text(tmp_path: Path) -> None:
    lib = symbol_library(
        tmp_path, "W", '(symbol "R_X")', '(symbol "RAX")', '(symbol "50%")'
    )
    index = LibraryIndex()
    index.update([lib])
    assert [s.lib_id for s in index.find_symbols("R_")] == ["W:R_X"]
    assert [s.lib_id for s in index.find_symbols("%")] == ["W:50%"]


def test_footprints_are_searched_by_name_and_tags(libs: list[Library]) -> None:
    index = LibraryIndex()
    index.update(libs)
    found = index.find_footprints("0805 resistor")
    assert [f.lib_id for f in found] == ["Passive:R_0805"]
    assert found[0].pad_count == 3
