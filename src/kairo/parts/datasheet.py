# pdfplumber gives each word as a plain dictionary and has no type stubs
# for it, which a strict type checker cannot follow.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""Read the limit tables of a datasheet PDF, keeping minimum, typical and maximum apart.

A chip's limits are in its datasheet's tables and nowhere else that a
program can reach: the feedback voltage's range, the least current
limit, the least switching frequency. This module reads such a table
into rows, each with the figures under MIN, TYP and MAX kept in their
own columns, the unit, and the page.

Which column a figure is in matters more than the figure. A typical
value is not a limit, and a row that gives one figure gives no hint, by
its text alone, of which it is. So the columns are found from where the
headings MIN, TYP and MAX sit on the page, and each figure on a row goes
to the heading it is nearest. A row where two figures would land under
one heading, or that has words where the figures go, is left out, not
guessed at.

What comes out is for a person or a model to read against the page,
not to be used unseen: the parameter's name is the words to the left of
the columns as they lie on the line, so a subscript is lost (``VFB``
reads ``V``) and a name on two lines is in two pieces. The figures and
their columns are what this reads well.

Tried on four datasheets when written. On TI's TPS54202 (SLVSD26C,
pages 4 and 5) every row's figures landed in the right column, among
them the feedback voltage ``0.581 0.596 0.611``, a quiescent current
given as typical only, and an enable threshold given as typical and
maximum. On Microchip's MCP1700 and Advanced Monolithic's AMS1117 the
rows looked at agree with the page's own text. On Diodes
Incorporated's AP63205, whose table stacks several parts in one cell,
whether each figure is paired with the right name was not
established. Four datasheets are not a survey of how makers lay
tables out.

Reading PDFs needs pdfplumber, which is not installed with Kairo by
default: ``pip install kairo[datasheets]``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Words on one line of text lie within this many points of each other
#: in height.
_LINE = 2.5
#: A table has ended where the next line of text is further below the
#: last than this, in points: a page's footer sits well clear of its
#: tables. A table's own rows are 10 to 25 points apart.
_GAP = 45.0
# A figure as printed; a minus may be a hyphen, a minus sign or an en dash.
_NUMBER = re.compile("[-+\u2212\u2013]?\\d+(?:\\.\\d+)?")
#: Marks printed in a cell that holds no figure.
_DASHES = "-\u2012\u2013\u2014\u2015"
_HEADINGS = {"MIN": "minimum", "TYP": "typical", "NOM": "typical", "MAX": "maximum"}


class DatasheetError(ValueError):
    """A datasheet cannot be read: the message begins with a code.

    ``E_DATASHEET_READER_MISSING`` if pdfplumber is not installed,
    ``E_DATASHEET_UNREADABLE`` if the file is not a PDF it can open or a
    page asked for is not in it.
    """


@dataclass(frozen=True, slots=True)
class Row:
    """One row of a limits table.

    Attributes:
        page: The page it is on, from 1.
        text: The words to the left of the figures: the parameter and
            its conditions, as they lie on the line.
        minimum: The figure under MIN, as printed, or ``None``.
        typical: The figure under TYP or NOM, or ``None``.
        maximum: The figure under MAX, or ``None``.
        unit: The words to the right of the figures.
    """

    page: int
    text: str
    minimum: str | None
    typical: str | None
    maximum: str | None
    unit: str

    def as_dict(self) -> dict[str, Any]:
        """The row as plain data, with only the columns it has."""
        out: dict[str, Any] = {"page": self.page, "text": self.text}
        for name in ("minimum", "typical", "maximum"):
            figure = getattr(self, name)
            if figure is not None:
                out[name] = figure
        out["unit"] = self.unit
        return out


def read_rows(pdf: Path, pages: Sequence[int] | None = None) -> list[Row]:
    """Read every limits table on some pages of a datasheet.

    Args:
        pdf: The datasheet.
        pages: The pages to read, from 1; every page if ``None``.

    Returns:
        The rows, in the order they are printed. A page with no line
        that holds the headings MIN and MAX, with or without TYP (or
        NOM) between them, gives none.

    Raises:
        DatasheetError: With one of the codes the class lists.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise DatasheetError(
            "E_DATASHEET_READER_MISSING: reading a PDF needs pdfplumber, which "
            "is not installed. Install it with: pip install kairo[datasheets]"
        ) from exc
    rows: list[Row] = []
    try:
        with pdfplumber.open(pdf) as document:
            count = len(document.pages)
            wanted = list(pages) if pages is not None else list(range(1, count + 1))
            for number in wanted:
                if not 1 <= number <= count:
                    raise DatasheetError(
                        f"E_DATASHEET_UNREADABLE: {pdf.name} has pages 1 to {count}; "
                        f"page {number} was asked for"
                    )
                words = document.pages[number - 1].extract_words(
                    x_tolerance=1.5, y_tolerance=2
                )
                rows += _page_rows(number, words)
    except DatasheetError:
        raise
    except Exception as exc:  # pdfplumber raises many kinds for a bad file
        raise DatasheetError(
            f"E_DATASHEET_UNREADABLE: {pdf.name} could not be read as a PDF: {exc}"
        ) from exc
    return rows


def matching(rows: Sequence[Row], words: str) -> list[Row]:
    """The rows whose text holds every word asked for, in any letter case.

    Args:
        rows: The rows to look through.
        words: One or more words, such as ``feedback voltage``.

    Returns:
        The matching rows, in order.
    """
    wanted = words.lower().split()
    return [row for row in rows if all(word in row.text.lower() for word in wanted)]


def _centre(word: dict[str, Any]) -> float:
    return (float(word["x0"]) + float(word["x1"])) / 2


def _lines(words: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group a page's words into lines of text, top to bottom."""
    lines: list[list[dict[str, Any]]] = []
    top = None
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if top is None or float(word["top"]) - top > _LINE:
            lines.append([])
            top = float(word["top"])
        lines[-1].append(word)
    return [sorted(line, key=lambda w: float(w["x0"])) for line in lines]


def _headings(line: Sequence[dict[str, Any]]) -> dict[str, float] | None:
    """Where MIN, TYP and MAX sit on a line, if it is a table's heading line."""
    found: dict[str, float] = {}
    unit = None
    for word in line:
        name = str(word["text"]).upper().strip(".")
        if name in _HEADINGS and _HEADINGS[name] not in found:
            found[_HEADINGS[name]] = _centre(word)
        elif name in ("UNIT", "UNITS"):
            unit = _centre(word)
    # Ratings tables have no typical column.
    if not {"minimum", "maximum"} <= set(found):
        return None
    in_order = [found[n] for n in ("minimum", "typical", "maximum") if n in found]
    if in_order != sorted(in_order) or len(set(in_order)) != len(in_order):
        return None
    if unit is not None and unit > found["maximum"]:
        found["unit"] = unit
    return found


def _page_rows(page: int, words: Sequence[dict[str, Any]]) -> list[Row]:
    """The rows under each heading line of a page."""
    rows: list[Row] = []
    columns: dict[str, float] | None = None
    last = 0.0
    for line in _lines(words):
        top = float(line[0]["top"])
        heading = _headings(line)
        if heading is not None:
            columns, last = heading, top
            continue
        if columns is not None and top - last > _GAP:
            columns = None
        last = top
        if columns is None:
            continue
        names = [n for n in ("minimum", "typical", "maximum") if n in columns]
        places = {name: columns[name] for name in names}
        pitch = (places["maximum"] - places["minimum"]) / (len(names) - 1)
        left = places["minimum"] - pitch / 2
        right = (
            (places["maximum"] + columns["unit"]) / 2
            if "unit" in columns
            else places["maximum"] + pitch / 2
        )
        cells: dict[str, str] = {}
        clash = False
        for word in line:
            x = _centre(word)
            text = str(word["text"])
            if left <= x <= right and not text.strip(_DASHES):
                # Some makers print a dash in a cell that holds nothing.
                continue
            if left <= x <= right and not _NUMBER.fullmatch(text):
                # Words where the figures go: running text, such as a
                # page's footer, and not a row of the table.
                clash = True
            elif left <= x <= right:
                nearest = min(names, key=lambda name, x=x: abs(places[name] - x))
                clash = clash or nearest in cells
                cells[nearest] = text.replace("\u2212", "-").replace("\u2013", "-")
        if not cells or clash:
            continue
        rows.append(
            Row(
                page,
                " ".join(str(w["text"]) for w in line if _centre(w) < left),
                cells.get("minimum"),
                cells.get("typical"),
                cells.get("maximum"),
                " ".join(str(w["text"]) for w in line if _centre(w) > right),
            )
        )
    return rows
