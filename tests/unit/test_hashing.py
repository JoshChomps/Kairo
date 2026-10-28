from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kairo.hashing import tree_sha256


def make(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_matches_the_stated_scheme(tmp_path: Path) -> None:
    root = make(tmp_path / "t", {"b.txt": b"bb", "a/c.txt": b"c"})
    expected = hashlib.sha256(b"a/c.txt\x001\x00c" + b"b.txt\x002\x00bb").hexdigest()
    assert tree_sha256(root) == expected


def test_any_change_changes_the_hash(tmp_path: Path) -> None:
    files = {"x.kicad_sch": b"(kicad_sch)\r\n", "y/z.json": b"{}"}
    base = tree_sha256(make(tmp_path / "base", files))
    content = tree_sha256(make(tmp_path / "content", files | {"y/z.json": b"{ }"}))
    renamed = tree_sha256(
        make(
            tmp_path / "renamed",
            {"x2.kicad_sch": files["x.kicad_sch"], "y/z.json": b"{}"},
        )
    )
    line_endings = tree_sha256(
        make(tmp_path / "eol", files | {"x.kicad_sch": b"(kicad_sch)\n"})
    )
    assert len({base, content, renamed, line_endings}) == 4


def test_empty_folders_do_not_count(tmp_path: Path) -> None:
    root = make(tmp_path / "t", {"a.txt": b"a"})
    before = tree_sha256(root)
    (root / "empty").mkdir()
    assert tree_sha256(root) == before


def test_not_a_folder(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        tree_sha256(tmp_path / "missing")
