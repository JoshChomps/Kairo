"""Keep NOTICE in step with uv.lock and the compiled loops' Cargo.lock.

Every locked package has to appear in NOTICE at its locked version. Bumping
a dependency without touching NOTICE fails here, which forces someone to
look at the new version's licence.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def locked_packages() -> set[tuple[str, str]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages: list[dict[str, Any]] = lock["package"]
    # Kairo itself is installed editable; its compiled loops are part of it.
    return {
        (p["name"], p["version"])
        for p in packages
        if "editable" not in p.get("source", {})
    }


def locked_crates() -> set[tuple[str, str]]:
    lock = tomllib.loads(
        (ROOT / "rust" / "kairo_fast" / "Cargo.lock").read_text(encoding="utf-8")
    )
    packages: list[dict[str, Any]] = lock["package"]
    return {(p["name"], p["version"]) for p in packages if p["name"] != "kairo_fast"}


def listed(start: str, end: str) -> set[tuple[str, str]]:
    """The name and version of each line of a NOTICE section."""
    text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    section = text.split(start, 1)[1].split(end, 1)[0]
    found: set[tuple[str, str]] = set()
    for line in section.splitlines():
        if " | " not in line:
            continue
        name, version = line.split(" | ", 1)[0].split()
        found.add((name, version))
    return found


def notice_packages() -> set[tuple[str, str]]:
    return listed("Python packages (uv.lock)", "External programs")


def notice_crates() -> set[tuple[str, str]]:
    return listed("Rust crates (rust/kairo_fast/Cargo.lock)", "Test data")


def test_every_locked_package_is_in_notice() -> None:
    missing = locked_packages() - notice_packages()
    assert not missing, f"add these to NOTICE with their licence: {sorted(missing)}"


def test_notice_lists_no_package_that_is_not_locked() -> None:
    stale = notice_packages() - locked_packages()
    assert not stale, f"remove or update these NOTICE entries: {sorted(stale)}"


def test_every_locked_crate_is_in_notice() -> None:
    missing = locked_crates() - notice_crates()
    assert not missing, f"add these to NOTICE with their licence: {sorted(missing)}"


def test_notice_lists_no_crate_that_is_not_locked() -> None:
    stale = notice_crates() - locked_crates()
    assert not stale, f"remove or update these NOTICE entries: {sorted(stale)}"
