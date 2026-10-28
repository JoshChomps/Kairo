from __future__ import annotations

import re
from pathlib import Path

import pytest

from kairo.kicad.emit import copy, lay_out, lst, number, sym
from kairo.kicad.sexpr import SList, parse, to_text

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
VERSION = re.compile(rb"\(version (\d+)\)")


def saved_by_kicad_9_or_10(path: Path) -> bool:
    found = VERSION.search(path.read_bytes()[:200])
    return found is not None and int(found.group(1)) >= 20250114


# Sheets and symbol libraries in formats the KiCad 9 and 10 programs write.
CURRENT = sorted(
    path
    for path in REAL.rglob("*")
    if path.suffix in (".kicad_sch", ".kicad_sym") and saved_by_kicad_9_or_10(path)
)


def relaid(text: str) -> str:
    document = parse(text)
    lay_out(document.root, newline="\r\n" if "\r\n" in text else "\n")
    return document.to_text()


def test_numbers_are_written_as_kicad_writes_lengths() -> None:
    assert [number(v).raw for v in (1.27, -3.81, 0, -0.0, 177.8, 2.54000001, 100)] == [
        "1.27",
        "-3.81",
        "0",
        "0",
        "177.8",
        "2.54",
        "100",
    ]


def test_a_built_tree_is_laid_out_like_a_kicad_file() -> None:
    wire = lst(
        "wire",
        lst("pts", lst("xy", 82.55, 55.88), lst("xy", 82.55, 60.96)),
        lst("stroke", lst("width", 0), lst("type", sym("default"))),
        lst("uuid", "0737d15b-aa6a-4dc7-b0c5-0852260648c2"),
    )
    assert to_text(lay_out(wire, depth=1)) == (
        "(wire\n\t\t(pts\n\t\t\t(xy 82.55 55.88) (xy 82.55 60.96)\n\t\t)\n"
        "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
        '\t\t(uuid "0737d15b-aa6a-4dc7-b0c5-0852260648c2")\n\t)'
    )


def test_a_copy_shares_nothing_with_the_original() -> None:
    original = parse('(a (b "x") c)').root
    duplicate = copy(original)
    assert isinstance(duplicate, SList)
    assert to_text(duplicate) == to_text(original)
    lay_out(duplicate)
    assert to_text(original) == '(a (b "x") c)'


def test_real_files_come_back_byte_for_byte_except_what_the_rules_leave_out() -> None:
    same = 0
    for path in CURRENT:
        text = path.read_bytes().decode("utf-8")
        if relaid(text) == text:
            same += 1
        else:
            # Embedded images and long bus-alias member lists wrap runs of
            # quoted strings, which lay_out does not do.
            assert "(data " in text or "(members " in text, path
    assert len(CURRENT) >= 149
    assert same >= 114


@pytest.mark.parametrize("limit_breaker", [40])
def test_a_long_run_of_points_wraps_at_the_column_limit(limit_breaker: int) -> None:
    points = [lst("xy", 100 + i, 200 + i) for i in range(limit_breaker)]
    text = to_text(lay_out(lst("pts", *points), depth=3))
    lines = text.split("\n")[1:-1]
    assert len(lines) > 1
    # A point is added while the line is under 99 characters, so a line can
    # pass 99 by at most one point.
    assert all(len(line) < 99 + len(" (xy 139 239)") for line in lines)
    assert all(len(line) >= 99 for line in lines[:-1])
