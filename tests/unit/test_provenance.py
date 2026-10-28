"""Tests for kairo.provenance: which build is serving, and whether it is current."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kairo import provenance


@pytest.fixture
def package(tmp_path: Path) -> Path:
    folder = tmp_path / "kairo"
    folder.mkdir()
    (folder / "a.py").write_text("A = 1\n", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "b.py").write_text("B = 1\n", encoding="utf-8")
    return folder


def _bump(path: Path) -> None:
    # Later than the stamp taken at start, whatever the clock's grain.
    stamp = path.stat().st_mtime_ns + 10_000_000_000
    os.utime(path, ns=(stamp, stamp))


def test_a_folder_outside_a_checkout_has_no_commit_and_none_is_guessed(
    package: Path,
) -> None:
    start = provenance.capture(package)
    if shutil.which("git") is not None:
        # tmp_path is not inside a repository on any machine the tests run on.
        assert start.commit is None
    assert start.uncommitted is None
    assert provenance.describe(start)["commit"] == "unknown"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_checkout_gives_its_commit_and_counts_files_not_committed(
    package: Path,
) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=package, check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    clean = provenance.capture(package)
    assert clean.commit is not None and len(clean.commit) == 40
    assert clean.uncommitted == 0
    (package / "a.py").write_text("A = 2\n", encoding="utf-8")
    (package / "c.py").write_text("C = 1\n", encoding="utf-8")
    assert provenance.capture(package).uncommitted == 2


def test_code_changed_after_start_means_a_restart_is_needed(package: Path) -> None:
    start = provenance.capture(package)
    assert provenance.describe(start)["restart_needed"] is False
    _bump(package / "sub" / "b.py")
    (package / "new.py").write_text("", encoding="utf-8")
    (package / "a.py").unlink()
    answer = provenance.describe(start)
    assert answer["restart_needed"] is True
    assert answer["changed_since_start"] == ["a.py", "new.py", "sub/b.py"]


def test_an_extension_manifest_is_compared_with_this_builds_tools(
    tmp_path: Path,
) -> None:
    assert provenance.bundle_check(None, ["go"], "1")["status"] == "not_an_extension"
    assert provenance.bundle_check(tmp_path, ["go"], "1")["status"] == "unreadable"
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": "1", "tools": [{"name": "go"}, {"name": "old"}]}),
        encoding="utf-8",
    )
    stale = provenance.bundle_check(tmp_path, ["go", "new"], "1")
    assert stale["status"] == "rebuild"
    assert stale["missing_from_manifest"] == ["new"]
    assert stale["only_in_manifest"] == ["old"]
    assert provenance.bundle_check(tmp_path, ["go", "old"], "2")["status"] == "rebuild"
    assert provenance.bundle_check(tmp_path, ["go", "old"], "1")["status"] == "current"
