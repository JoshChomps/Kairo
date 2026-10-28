from __future__ import annotations

from pathlib import Path

from kairo.library import Library, install_variables, read_library_table


def entry(name: str, uri: str, kind: str = "KiCad", extra: str = "") -> str:
    return (
        f'(lib (name "{name}") (type "{kind}") (uri "{uri}") '
        f'(options "") (descr "") {extra})'
    )


def table(path: Path, *entries: str, head: str = "sym_lib_table") -> Path:
    path.write_text(f"({head} (version 7) {' '.join(entries)})", encoding="utf-8")
    return path


def test_both_variable_forms_are_expanded(tmp_path: Path) -> None:
    libs = tmp_path.as_posix()
    path = table(
        tmp_path / "sym-lib-table",
        entry("A", "${LIBS}/a.kicad_sym"),
        entry("B", "$(LIBS)/b.kicad_sym"),
    )
    found, problems = read_library_table(path, "symbol", {"LIBS": libs})
    assert problems == []
    assert found == [
        Library("symbol", "A", tmp_path / "a.kicad_sym"),
        Library("symbol", "B", tmp_path / "b.kicad_sym"),
    ]


def test_a_table_entry_brings_in_another_table(tmp_path: Path) -> None:
    inner = table(tmp_path / "inner", entry("A", f"{tmp_path.as_posix()}/a.kicad_sym"))
    outer = table(tmp_path / "outer", entry("KiCad", inner.as_posix(), "Table"))
    found, problems = read_library_table(outer, "symbol", {})
    assert problems == []
    assert [lib.nickname for lib in found] == ["A"]


def test_a_table_that_includes_itself_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "loop"
    table(path, entry("Self", path.as_posix(), "Table"))
    found, problems = read_library_table(path, "symbol", {})
    assert found == []
    assert [p.reason for p in problems] == ["the table includes itself"]


def test_entries_kairo_cannot_use_are_reported_not_dropped(tmp_path: Path) -> None:
    here = tmp_path.as_posix()
    path = table(
        tmp_path / "sym-lib-table",
        entry("Good", f"{here}/good.kicad_sym"),
        entry("NoVar", "${KICAD6_SYMBOL_DIR}/x.kicad_sym"),
        entry("Old", f"{here}/old.lib", "Legacy"),
        entry("Off", f"{here}/off.kicad_sym", extra="(disabled)"),
        entry("Rel", "libs/rel.kicad_sym"),
        entry("Good", f"{here}/second.kicad_sym"),
    )
    found, problems = read_library_table(path, "symbol", {})
    assert found == [Library("symbol", "Good", tmp_path / "good.kicad_sym")]
    reasons = {p.nickname: p.reason for p in problems}
    assert "KICAD6_SYMBOL_DIR" in reasons["NoVar"]
    assert "'Legacy'" in reasons["Old"]
    assert "(disabled" in reasons["Off"]
    assert "not absolute" in reasons["Rel"]
    assert "repeats a nickname" in reasons["Good"]
    assert len(problems) == 5


def test_a_table_of_the_wrong_kind_is_reported(tmp_path: Path) -> None:
    path = table(tmp_path / "fp-lib-table", head="fp_lib_table")
    found, problems = read_library_table(path, "symbol", {})
    assert found == []
    assert "not a 'sym_lib_table'" in problems[0].reason


def test_a_missing_or_malformed_table_is_reported(tmp_path: Path) -> None:
    for path in (tmp_path / "absent", tmp_path / "broken"):
        if path.name == "broken":
            path.write_text("(sym_lib_table", encoding="utf-8")
        found, problems = read_library_table(path, "symbol", {})
        assert found == []
        assert "could not be read" in problems[0].reason


def test_install_variables_point_at_the_install_unless_the_environment_does(
    tmp_path: Path,
) -> None:
    assert install_variables(tmp_path, 10, {}) == {
        "KICAD10_SYMBOL_DIR": str(tmp_path / "symbols"),
        "KICAD10_FOOTPRINT_DIR": str(tmp_path / "footprints"),
    }
    overridden = install_variables(tmp_path, 10, {"KICAD10_SYMBOL_DIR": "/elsewhere"})
    assert overridden["KICAD10_SYMBOL_DIR"] == "/elsewhere"
    assert overridden["KICAD10_FOOTPRINT_DIR"] == str(tmp_path / "footprints")
