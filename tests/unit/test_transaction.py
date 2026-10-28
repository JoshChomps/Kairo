from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from kairo.kicad.transaction import (
    BACKUP_FOLDER,
    WRITTEN_RECORD,
    Session,
    TransactionError,
)


def names(folder: Path) -> list[str]:
    """The files in a project, leaving out Kairo's record of its writes."""
    return sorted(
        str(p.relative_to(folder)).replace("\\", "/")
        for p in folder.rglob("*")
        if p.is_file() and p != folder / WRITTEN_RECORD
    )


def test_new_files_are_written_exactly_and_reported(tmp_path: Path) -> None:
    session = Session(tmp_path / "project")
    report = session.write({"a.kicad_sch": "(a)\r\n(b)\r\n", "a.kicad_pro": "{}\n"})
    assert report.written and report.backup is None
    assert (tmp_path / "project" / "a.kicad_sch").read_bytes() == b"(a)\r\n(b)\r\n"
    first, second = report.changes
    assert (first.path.name, first.created, first.changed_lines, first.ratio) == (
        "a.kicad_sch",
        True,
        2,
        None,
    )
    assert second.path.name == "a.kicad_pro"
    assert names(tmp_path / "project") == ["a.kicad_pro", "a.kicad_sch"]


def test_a_replaced_file_is_backed_up_and_its_changed_lines_counted(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path)
    old = "".join(f"line {n}\n" for n in range(100))
    session.write({"s.kicad_sch": old})
    new = old.replace("line 10\n", "LINE TEN\n").replace("line 50\n", "") + "added\n"
    report = session.write({"s.kicad_sch": new})
    (change,) = report.changes
    # One line replaced, one removed, one added.
    assert (change.created, change.previous_lines, change.changed_lines) == (
        False,
        100,
        3,
    )
    assert change.ratio == pytest.approx(0.03)
    assert report.backup == tmp_path / BACKUP_FOLDER / "0002"
    assert report.backup is not None
    assert (report.backup / "s.kicad_sch").read_text(encoding="utf-8") == old
    assert (tmp_path / "s.kicad_sch").read_text(encoding="utf-8") == new


def test_a_file_edited_outside_the_session_is_never_written_over(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path)
    session.write({"s.kicad_sch": "(kairo wrote this)"})
    sheet = tmp_path / "s.kicad_sch"
    sheet.write_text("(the user changed it in KiCad)", encoding="utf-8")
    with pytest.raises(TransactionError, match="E_FILE_CHANGED") as caught:
        session.write({"s.kicad_sch": "(kairo again)"})
    assert caught.value.code == "E_FILE_CHANGED"
    assert sheet.read_text(encoding="utf-8") == "(the user changed it in KiCad)"
    # Once it has been read again, the write goes through.
    assert session.read("s.kicad_sch") == "(the user changed it in KiCad)"
    assert session.write({"s.kicad_sch": "(kairo again)"}).written

    # A file that exists, was never read in this session and was not
    # written by Kairo is the same case.
    (tmp_path / "theirs.kicad_sch").write_text("(drawn by hand)", encoding="utf-8")
    other = Session(tmp_path)
    with pytest.raises(TransactionError, match="E_FILE_CHANGED"):
        other.write({"theirs.kicad_sch": "(a second session)"})


def test_a_later_session_may_write_over_what_kairo_wrote_and_no_one_touched(
    tmp_path: Path,
) -> None:
    # A design rebuilt from a new process, as the BMS board was on
    # 2026-10-16: the files are exactly what the first build left.
    first = Session(tmp_path)
    first.write({"s.kicad_sch": "(first build)"})
    first.write({"s.kicad_sch": "(second build)"})
    later = Session(tmp_path)
    report = later.write({"s.kicad_sch": "(a new process)"})
    assert report.written
    # Its backup is numbered on from the first session's, not over them.
    assert report.backup == tmp_path / BACKUP_FOLDER / "0003"
    kept = tmp_path / BACKUP_FOLDER / "0002" / "s.kicad_sch"
    assert kept.read_text(encoding="utf-8") == "(first build)"

    # Once the user edits the file, a session that has not read it is refused.
    (tmp_path / "s.kicad_sch").write_text("(edited in KiCad)", encoding="utf-8")
    with pytest.raises(TransactionError, match="E_FILE_CHANGED"):
        Session(tmp_path).write({"s.kicad_sch": "(yet another process)"})


def test_a_damaged_record_trusts_nothing(tmp_path: Path) -> None:
    Session(tmp_path).write({"s.kicad_sch": "(kairo wrote this)"})
    (tmp_path / WRITTEN_RECORD).write_text("not json", encoding="utf-8")
    with pytest.raises(TransactionError, match="E_FILE_CHANGED"):
        Session(tmp_path).write({"s.kicad_sch": "(again)"})


def test_a_failed_check_puts_everything_back(tmp_path: Path) -> None:
    session = Session(tmp_path)
    session.write({"keep.kicad_sch": "(original)"})
    seen: list[str] = []

    def check(folder: Path) -> list[str]:
        # The check sees the new files in place.
        seen.append((folder / "keep.kicad_sch").read_text(encoding="utf-8"))
        return ["the netlist differs"]

    report = session.write(
        {"keep.kicad_sch": "(changed)", "new.kicad_sch": "(new)"}, check
    )
    assert not report.written
    assert report.problems == ("the netlist differs",) and report.changes == ()
    assert seen == ["(changed)"]
    assert names(tmp_path) == ["keep.kicad_sch"]
    assert (tmp_path / "keep.kicad_sch").read_text(encoding="utf-8") == "(original)"
    # The session still knows the file, so the next write is not refused.
    assert session.write({"keep.kicad_sch": "(second try)"}).written


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
def test_a_write_that_fails_part_way_leaves_the_originals_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    session = Session(tmp_path)
    session.write({"one.kicad_sch": "(one)", "two.kicad_sch": "(two)"})
    real = os.replace
    calls = {"n": 0}

    def failing(source: Any, target: Any) -> None:
        calls["n"] += 1
        if calls["n"] == fail_at:
            raise OSError("disk full")
        real(source, target)

    # Each replaced file takes two moves: its backup, then the file itself.
    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(TransactionError, match=r"E_WRITE_FAILED.*disk full"):
        session.write(
            {
                "one.kicad_sch": "(ONE)",
                "two.kicad_sch": "(TWO)",
                "three.kicad_sch": "(3)",
            }
        )
    monkeypatch.setattr(os, "replace", real)
    assert names(tmp_path) == ["one.kicad_sch", "two.kicad_sch"]
    assert (tmp_path / "one.kicad_sch").read_text(encoding="utf-8") == "(one)"
    assert (tmp_path / "two.kicad_sch").read_text(encoding="utf-8") == "(two)"
    assert session.write({"one.kicad_sch": "(after)"}).written


def test_undo_restores_the_last_write_and_then_the_one_before(tmp_path: Path) -> None:
    session = Session(tmp_path)
    session.write({"s.kicad_sch": "(v1)"})
    session.write({"s.kicad_sch": "(v2)", "extra.kicad_sch": "(x)"})
    session.write({"s.kicad_sch": "(v3)"})
    assert [p.name for p in session.undo()] == ["s.kicad_sch"]
    assert (tmp_path / "s.kicad_sch").read_text(encoding="utf-8") == "(v2)"
    assert sorted(p.name for p in session.undo()) == ["extra.kicad_sch", "s.kicad_sch"]
    assert (tmp_path / "s.kicad_sch").read_text(encoding="utf-8") == "(v1)"
    assert not (tmp_path / "extra.kicad_sch").exists()
    session.undo()
    assert names(tmp_path) == []
    with pytest.raises(TransactionError, match="E_NOTHING_TO_UNDO"):
        session.undo()
    # After undoing, writing again works from the restored state.
    assert session.write({"s.kicad_sch": "(v1 again)"}).written


def test_undo_does_not_discard_an_edit_made_since(tmp_path: Path) -> None:
    session = Session(tmp_path)
    session.write({"s.kicad_sch": "(v1)"})
    session.write({"s.kicad_sch": "(v2)"})
    (tmp_path / "s.kicad_sch").write_text("(edited in KiCad)", encoding="utf-8")
    with pytest.raises(TransactionError, match="E_FILE_CHANGED"):
        session.undo()
    assert (tmp_path / "s.kicad_sch").read_text(encoding="utf-8") == "(edited in KiCad)"


def test_a_name_that_leaves_the_project_is_refused(tmp_path: Path) -> None:
    session = Session(tmp_path / "project")
    for name in ("../escape.kicad_sch", "", "."):
        with pytest.raises(TransactionError, match="E_BAD_NAME"):
            session.write({name: "(x)"})
    assert not (tmp_path / "escape.kicad_sch").exists()
    assert session.write({"kairo/accuracy-report.md": "# ok\n"}).written
