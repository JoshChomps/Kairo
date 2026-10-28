"""Reading a datasheet's limit tables, on a PDF written here by hand.

No maker's datasheet is in the repository. The page below is laid out
like the electrical characteristics page of TI's TPS54202 datasheet, on
which the reader was tried when written: headings MIN, TYP, MAX and UNIT
over columns of figures, a parameter and its conditions to the left.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from minilib import make_index

from kairo.design_tools import DesignTools
from kairo.kicad.cli import KicadCli
from kairo.parts.datasheet import DatasheetError, Row, matching, read_rows
from kairo.server import Settings, Workspace

pytest.importorskip("pdfplumber")

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_kicad_cli.py"
# Where each column's text starts, in points from the left.
NAME, CONDITIONS, MIN, TYP, MAX, UNIT = 50, 230, 440, 472, 503, 532


def pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    """A PDF whose pages hold text at given places (x, and y from the top)."""
    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * n} 0 R" for n in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for number, texts in enumerate(pages):
        drawn = b"".join(
            f"BT /F1 8 Tf 1 0 0 1 {x} {792 - y} Tm ({text}) Tj ET\n".encode("latin-1")
            for x, y, text in texts
        )
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {5 + 2 * number} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(drawn)} >>\nstream\n".encode() + drawn + b"endstream"
        )
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{start}\n%%EOF\n"
    ).encode()
    return out


def heading(y: float, typical: bool = True) -> list[tuple[float, float, str]]:
    columns = [(NAME, "PARAMETER"), (CONDITIONS, "TEST CONDITIONS"), (MIN, "MIN")]
    if typical:
        columns.append((TYP, "TYP"))
    columns += [(MAX, "MAX"), (UNIT, "UNIT")]
    return [(x, y, text) for x, text in columns]


def row(
    y: float,
    name: str,
    conditions: str,
    figures: dict[float, str],
    unit: str,
) -> list[tuple[float, float, str]]:
    cells = [(NAME, y, name), (UNIT, y, unit)]
    if conditions:
        cells.append((CONDITIONS, y, conditions))
    return cells + [(x, y, text) for x, text in figures.items()]


ELECTRICAL = [
    (NAME, 80, "5.5 Electrical Characteristics"),
    *heading(100),
    *row(115, "Input voltage range", "", {MIN: "4.5", MAX: "28"}, "V"),
    *row(
        130, "Non switching quiescent current", "EN = 5V, VFB = 1V", {TYP: "45"}, "uA"
    ),
    *row(
        145,
        "Feedback voltage",
        "VIN = 12V",
        {MIN: "0.581", TYP: "0.596", MAX: "0.611"},
        "V",
    ),
    *row(160, "Enable threshold rising", "", {TYP: "1.21", MAX: "1.28"}, "V"),
    *row(175, "High side current limit", "", {MIN: "2.5", TYP: "3.2", MAX: "3.9"}, "A"),
    *row(
        190,
        "Centre switching frequency",
        "",
        {MIN: "390", TYP: "500", MAX: "630"},
        "kHz",
    ),
    # A dash where a cell holds nothing, as some makers print one.
    *row(198, "Soft-start time", "", {MIN: "-", TYP: "5", MAX: "-"}, "ms"),
    # Two figures under one heading: which is which cannot be told.
    *row(205, "Muddled row", "", {TYP: "1", TYP + 9: "2"}, "V"),
    # A footer whose year falls under a column, among other words.
    (NAME, 760, "4 Submit Document Feedback"),
    (MIN - 40, 760, "Copyright 2026 Texas Instruments Incorporated"),
]
RATINGS = [
    (NAME, 80, "5.1 Absolute Maximum Ratings"),
    *heading(100, typical=False),
    *row(115, "Input voltage, VIN", "", {MIN: "-0.3", MAX: "30"}, "V"),
    *row(130, "Operating junction temperature", "", {MIN: "-40", MAX: "150"}, "degC"),
    (NAME, 200, "No headings follow, so nothing below is a row: 1 2 3"),
]


@pytest.fixture
def sheet(tmp_path: Path) -> Path:
    path = tmp_path / "part.pdf"
    path.write_bytes(pdf([RATINGS, ELECTRICAL, [(NAME, 100, "A page with no table.")]]))
    return path


def test_each_figure_is_kept_under_its_own_heading(sheet: Path) -> None:
    rows = read_rows(sheet, [2])
    by_name = {r.text: r for r in rows}
    assert by_name["Feedback voltage VIN = 12V"] == Row(
        2, "Feedback voltage VIN = 12V", "0.581", "0.596", "0.611", "V"
    )
    # One figure, and it is the typical one: no limit at all.
    quiet = by_name["Non switching quiescent current EN = 5V, VFB = 1V"]
    assert (quiet.minimum, quiet.typical, quiet.maximum) == (None, "45", None)
    assert quiet.as_dict() == {
        "page": 2,
        "text": "Non switching quiescent current EN = 5V, VFB = 1V",
        "typical": "45",
        "unit": "uA",
    }
    enable = by_name["Enable threshold rising"]
    assert (enable.minimum, enable.typical, enable.maximum) == (None, "1.21", "1.28")
    span = by_name["Input voltage range"]
    assert (span.minimum, span.typical, span.maximum) == ("4.5", None, "28")
    assert by_name["Centre switching frequency"].minimum == "390"
    soft = by_name["Soft-start time"]
    assert (soft.minimum, soft.typical, soft.maximum) == (None, "5", None)
    # The muddled row and the footer are left out, not guessed at.
    assert len(rows) == 7
    assert not any("Muddled" in r.text or "Copyright" in r.text for r in rows)


def test_a_ratings_table_with_no_typical_column_is_read(sheet: Path) -> None:
    rows = read_rows(sheet, [1])
    assert [(r.text, r.minimum, r.typical, r.maximum, r.unit) for r in rows] == [
        ("Input voltage, VIN", "-0.3", None, "30", "V"),
        ("Operating junction temperature", "-40", None, "150", "degC"),
    ]


def test_every_page_is_read_and_rows_are_found_by_their_words(sheet: Path) -> None:
    rows = read_rows(sheet)
    assert [r.page for r in rows] == [1, 1, 2, 2, 2, 2, 2, 2, 2]
    assert [r.text for r in matching(rows, "current LIMIT")] == [
        "High side current limit"
    ]
    assert matching(rows, "feedback current") == []
    assert read_rows(sheet, [3]) == []
    with pytest.raises(DatasheetError, match="pages 1 to 3"):
        read_rows(sheet, [4])
    with pytest.raises(DatasheetError, match="E_DATASHEET_UNREADABLE"):
        read_rows(FAKE)


def test_the_tool_reads_a_datasheet_under_an_allowed_root(
    sheet: Path, tmp_path: Path
) -> None:
    libraries = tmp_path / "libraries"
    libraries.mkdir()
    cache = tmp_path / "cache"
    (cache / "datasheets").mkdir(parents=True)
    kept = cache / "datasheets" / "kept.pdf"
    kept.write_bytes(sheet.read_bytes())
    outside = tmp_path.parent / f"{tmp_path.name}-outside.pdf"
    outside.write_bytes(sheet.read_bytes())
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "part.pdf"
    inside.write_bytes(sheet.read_bytes())
    settings = Settings((root,), cache, tier="read")
    cli = KicadCli((sys.executable, str(FAKE), "silent"), temp_root=tmp_path)
    tools = DesignTools(Workspace(settings, cli=cli, index=make_index(libraries)))

    found = tools.read_datasheet(str(inside), search="feedback")
    assert found["file"] == "part.pdf" and found["rows_found"] == 1
    assert found["rows"] == [
        {
            "page": 2,
            "text": "Feedback voltage VIN = 12V",
            "minimum": "0.581",
            "typical": "0.596",
            "maximum": "0.611",
            "unit": "V",
        }
    ]
    assert "typical is not a limit" in found["notes"][0]
    # A file kept in Kairo's own datasheets folder is found by its name.
    assert tools.read_datasheet("kept.pdf", pages=[1])["rows_found"] == 2
    nothing = tools.read_datasheet(str(inside), pages=[3])
    assert nothing["rows"] == [] and "No table" in nothing["notes"][-1]
    try:
        with pytest.raises(ToolError, match="E_PATH_NOT_ALLOWED"):
            tools.read_datasheet(str(outside))
    finally:
        outside.unlink()
    with pytest.raises(ToolError, match="E_DATASHEET_UNREADABLE"):
        tools.read_datasheet(str(inside), pages=[9])
