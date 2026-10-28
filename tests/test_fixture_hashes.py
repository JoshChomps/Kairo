"""Every fixture tree matches the hash recorded for it, so none changes unnoticed.

To add or deliberately change a fixture tree, update its entry in
``fixture_hashes.json``; the failure message prints the new hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairo.hashing import tree_sha256

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
RECORDED: dict[str, str] = json.loads(
    (TESTS / "fixture_hashes.json").read_text(encoding="utf-8")
)


def fixture_trees() -> list[str]:
    """Folders directly holding files, relative to the fixtures folder."""
    folders = {p.parent for p in FIXTURES.rglob("*") if p.is_file()}
    return sorted(f.relative_to(FIXTURES).as_posix() for f in folders)


def test_every_fixture_tree_is_recorded() -> None:
    assert fixture_trees() == sorted(RECORDED)


@pytest.mark.parametrize("tree", sorted(RECORDED))
def test_fixture_tree_is_unchanged(tree: str) -> None:
    actual = tree_sha256(FIXTURES / tree)
    assert actual == RECORDED[tree], f"{tree} changed; its hash is now {actual}"
