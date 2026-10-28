"""The writer's two measured rules, on everything KiCad installed.

`lay_out` must give back every installed symbol library byte for byte,
and `embedded_symbol` must reproduce what the KiCad 10 program embedded
in the real boards for symbols the installed libraries still hold.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kairo.kicad.emit import lay_out
from kairo.kicad.sexpr import Atom, Node, SList, parse
from kairo.library.embed import embedded_symbol

pytestmark = pytest.mark.kicad

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
VERSION = re.compile(rb"\(version (\d+)\)")


def test_every_installed_symbol_library_is_laid_out_as_kicad_wrote_it(
    kicad_demos: Path,
) -> None:
    libraries = sorted((kicad_demos.parent / "symbols").glob("*.kicad_sym"))
    assert len(libraries) > 200
    for path in libraries:
        text = path.read_bytes().decode("utf-8")
        document = parse(text)
        lay_out(document.root, newline="\r\n" if "\r\n" in text else "\n")
        assert document.to_text() == text, path.name


def tokens(node: Node) -> object:
    if isinstance(node, Atom):
        return node.raw
    return tuple(tokens(item) for item in node.items)


def test_embedding_reproduces_what_kicad_10_embedded(kicad_demos: Path) -> None:
    installed = kicad_demos.parent / "symbols"
    roots: dict[str, SList | None] = {}
    same = differs = 0
    derived_same: set[str] = set()
    for sheet in sorted(REAL.rglob("*.kicad_sch")):
        data = sheet.read_bytes()
        found = VERSION.search(data[:200])
        if found is None or int(found.group(1)) < 20260306:
            continue
        embedded_all = parse(data.decode("utf-8")).root.first("lib_symbols")
        assert embedded_all is not None
        for embedded in embedded_all.lists("symbol"):
            name_atom = embedded.items[1]
            assert isinstance(name_atom, Atom)
            nickname, _, name = name_atom.value.partition(":")
            if nickname not in roots:
                path = installed / f"{nickname}.kicad_sym"
                roots[nickname] = (
                    parse(path.read_bytes().decode("utf-8")).root
                    if path.is_file()
                    else None
                )
            root = roots[nickname]
            if root is None:
                continue
            names = {
                node.items[1].value: node
                for node in root.lists("symbol")
                if isinstance(node.items[1], Atom)
            }
            if name not in names:
                continue
            if tokens(embedded_symbol(root, name, nickname)) == tokens(embedded):
                same += 1
                if names[name].first("extends") is not None:
                    derived_same.add(name_atom.value)
            else:
                differs += 1
    # Measured on KiCad 10.0.6 with its libraries: 76 the same, 13 not. The
    # 13 are copies the designer edited or took from an older library.
    assert same + differs >= 80
    assert same >= 70
    assert len(derived_same) >= 6
    # 1N4148 extends 1N4001 and only the base has the simulation fields,
    # which KiCad 10 embedded with the derived symbol.
    assert "Diode:1N4148" in derived_same
