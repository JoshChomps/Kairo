"""Whether a project opens the same on another computer."""

from __future__ import annotations

from pathlib import Path

from kairo.kicad.portability import check_portability

TABLE = """(fp_lib_table
  (version 7)
  (lib (name "Mine")(type "KiCad")(uri "${KIPRJMOD}/libs/Mine.pretty")
    (options "")(descr ""))
  (lib (name "Shared")(type "KiCad")(uri "${KIPRJMOD}/../shared/Shared.pretty")
    (options "")(descr ""))
)
"""


def project(tmp_path: Path, models: list[str]) -> tuple[Path, Path]:
    stock = tmp_path / "kicad" / "footprints"
    (stock / "Resistor_SMD.pretty").mkdir(parents=True)
    folder = tmp_path / "board"
    (folder / "libs" / "Mine.pretty").mkdir(parents=True)
    (folder / "fp-lib-table").write_text(TABLE, encoding="utf-8")
    board = "(kicad_pcb\n" + "".join(f'  (model "{m}")\n' for m in models) + ")\n"
    (folder / "board.kicad_pcb").write_text(board, encoding="utf-8")
    return folder, stock


def test_a_project_with_stock_and_its_own_libraries_travels(tmp_path: Path) -> None:
    folder, stock = project(
        tmp_path,
        [
            "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603.step",
            "${KIPRJMOD}/models/part.step",
        ],
    )
    found = check_portability(
        folder, ["Resistor_SMD:R_0603", "Mine:Part", ""], stock, {}
    )
    assert found.status == "portable"
    assert (found.footprint_libraries, found.models) == (2, 2)


def test_what_does_not_travel_is_named_and_why(tmp_path: Path) -> None:
    folder, stock = project(
        tmp_path,
        [
            "C:/Users/someone/models/part.step",
            "${MY_MODELS}/part.step",
            "models/relative.step",
        ],
    )
    found = check_portability(
        folder, ["Shared:Part", "Elsewhere:Part", "Resistor_SMD:R_0603"], stock, {}
    )
    assert found.status == "not_portable"
    assert [m.split(":")[0] for m in found.missing[:2]] == [
        "footprint library Elsewhere",
        "footprint library Shared",
    ]
    assert "outside its folder" in found.missing[1]
    assert any("an absolute path" in m for m in found.missing)
    assert any("variable another computer may lack" in m for m in found.missing)
    # A relative path's resolution by KiCad was not checked, so it is
    # reported, never passed or failed.
    assert found.unverified == ("3D model models/relative.step",)
    assert found.as_dict()["missing_count"] == 4


def test_only_unchecked_forms_leave_the_project_unverified(tmp_path: Path) -> None:
    folder, stock = project(tmp_path, ["models/relative.step"])
    assert check_portability(folder, [], stock, {}).status == "unverified"
