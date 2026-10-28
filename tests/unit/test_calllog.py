"""Tests for kairo.calllog: the record of tool calls a model can read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairo import calllog
from kairo.calllog import CallLog


def test_calls_are_kept_newest_last_and_counted_by_tool(tmp_path: Path) -> None:
    log = CallLog(tmp_path)
    log.record("calc", 0.2, "ok", arguments='{"name": "x"}')
    log.record("calc", 0.4, "refused", code="E_CALC_UNKNOWN", message="no such")
    log.record("calc", 0.6, "refused", code="E_CALC_UNKNOWN", message="no such")
    log.record("go", 0.0, "ok")
    answer = log.recent(limit=2)
    assert [c["tool"] for c in answer["calls"]] == ["calc", "go"]
    assert answer["matching"] == 4
    assert answer["by_tool"]["calc"] == {
        "calls": 3,
        "median_seconds": 0.4,
        "failures": {"E_CALC_UNKNOWN": 2},
    }
    failed = log.recent(failed_only=True, tool="calc")
    assert [c["code"] for c in failed["calls"]] == ["E_CALC_UNKNOWN"] * 2
    assert set(failed["by_tool"]) == {"calc"}


def test_each_call_is_a_json_line_in_the_cache_folder(tmp_path: Path) -> None:
    log = CallLog(tmp_path)
    log.record("calc", 1.0, "error", code="ValueError", message="m" * 1000)
    lines = (tmp_path / calllog.FILE_NAME).read_text(encoding="utf-8").splitlines()
    (record,) = (json.loads(line) for line in lines)
    assert record["outcome"] == "error" and record["code"] == "ValueError"
    assert len(record["message"]) == calllog.SHOWN


def test_a_full_log_is_started_afresh_keeping_one_older_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calllog, "MAX_BYTES", 10)
    log = CallLog(tmp_path)
    log.record("a", 0, "ok")
    log.record("b", 0, "ok")
    assert (tmp_path / "calls.jsonl.1").read_text(encoding="utf-8").count("\n") == 1
    assert '"b"' in (tmp_path / "calls.jsonl").read_text(encoding="utf-8")


def test_a_log_that_cannot_be_written_never_fails_a_call(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    log = CallLog(blocker)  # a file where the folder should be
    log.record("go", 0, "ok")
    log.record("go", 0, "ok")
    assert log.recent()["matching"] == 2
    assert log.recent()["log_file"] is None
