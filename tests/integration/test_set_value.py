"""Changing a part's value in a real, human-drawn board, checked by KiCad."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from kairo.kicad.edit import EditError, set_value, sheet_files
from kairo.server import Settings, build_server

pytestmark = pytest.mark.kicad

BOARD = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "real"
    / "git4dcc-rtb-c13"
    / "tree"
)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    copy = tmp_path / "c13"
    shutil.copytree(BOARD, copy)
    return copy


def test_only_the_value_changes_in_the_file(board: Path) -> None:
    root = board / "C13.kicad_sch"
    assert [p.name for p in sheet_files(root)] == [
        "C13.kicad_sch",
        "C13cpu.kicad_sch",
        "C13rcm.kicad_sch",
        "C13con.kicad_sch",
        "C13pwr.kicad_sch",
    ]
    before = (board / "C13rcm.kicad_sch").read_bytes()
    edit = set_value(root, "R34", "200k")
    assert edit.old == ("220k",) and edit.symbols == 1
    assert list(edit.files) == ["C13rcm.kicad_sch"]
    after = edit.files["C13rcm.kicad_sch"].encode("utf-8")
    # One string differs and every other byte is the author's.
    assert after == before.replace(
        b'(property "Value" "220k"', b'(property "Value" "200k"', 1
    )
    assert after.count(b"200k") == before.count(b"200k") + 1
    # Nothing is written until a transaction writes it.
    assert (board / "C13rcm.kicad_sch").read_bytes() == before
    with pytest.raises(EditError, match="E_REF_NOT_FOUND"):
        set_value(root, "R9999", "1k")
    # A value with a quote and a backslash is escaped as KiCad escapes it.
    odd = set_value(root, "R34", 'a "b" \\ c')
    assert rb'"a \"b\" \\ c"' in odd.files["C13rcm.kicad_sch"].encode("utf-8")


def test_the_tool_changes_one_line_and_kicad_confirms_it(
    board: Path, kicad_cli_path: Path, tmp_path: Path
) -> None:
    settings = Settings((board,), tmp_path / "cache", kicad_cli_path, tier="write")
    before = (board / "C13rcm.kicad_sch").read_bytes()

    async def talk() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(settings)) as client:
            for name, arguments in (
                ("set_value", {"project": str(board), "ref": "R34", "value": "200k"}),
                ("undo", {"folder": str(board)}),
            ):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, (name, result.content)
                out.append(result.structured_content)
                out.append((board / "C13rcm.kicad_sch").read_bytes())
        return out

    changed, on_disk, undone, restored = asyncio.run(talk())
    assert changed["written"] is True and changed["old_value"] == ["220k"]
    assert [f["changed_lines"] for f in changed["files"]] == [1]
    assert "only R34's value different" in changed["confirmed"]
    assert on_disk == before.replace(
        b'(property "Value" "220k"', b'(property "Value" "200k"', 1
    )
    assert undone["undone"] is True and restored == before
