"""Scoring the reference designs from saved records, with no KiCad and no model."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
RUNS = sorted(p for p in (ROOT / "evals" / "results").glob("*-scripted") if p.is_dir())


def harness() -> Any:
    spec = importlib.util.spec_from_file_location(
        "reference", ROOT / "evals" / "reference.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_reference_design_has_a_spec_a_design_and_an_expectation() -> None:
    folders = sorted(p for p in (ROOT / "evals" / "reference_designs").iterdir())
    assert len(folders) >= 4
    outcomes: set[str] = set()
    for folder in folders:
        assert (folder / "spec.md").read_text(encoding="utf-8").startswith("# ")
        assert (folder / "design.json").is_file()
        expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        outcomes.add(expected["outcome"])
        if expected["outcome"] == "design":
            assert expected["netlist"] and "unverified" in expected
        else:
            assert expected["refused_for"]
    # Both kinds are present: a suite of designs alone would reward a tool
    # that never refuses.
    assert outcomes == {"design", "refusal"}


@pytest.mark.parametrize("run", RUNS, ids=[p.name for p in RUNS])
def test_every_recorded_verdict_is_reproduced_offline(
    run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness().rescore(run) == 0
    assert "every verdict reproduced" in capsys.readouterr().out


@pytest.mark.skipif(not RUNS, reason="no recorded run in evals/results")
def test_the_latest_run_passes_every_design() -> None:
    summary = json.loads((RUNS[-1] / "summary.json").read_text(encoding="utf-8"))
    assert summary["kind"] == "scripted baseline; no model was called"
    assert all(result["passed"] for result in summary["designs"].values())
    assert (
        summary["strict_pass"]
        == f"{len(summary['designs'])} of {len(summary['designs'])}"
    )


@pytest.mark.skipif(not RUNS, reason="no recorded run in evals/results")
def test_a_changed_record_changes_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "run"
    shutil.copytree(RUNS[-1], copy)
    module = harness()
    # A netlist with one pin moved is no longer the expected circuit.
    netlist = copy / "led_indicator" / "netlist.net"
    text = netlist.read_bytes().decode("utf-8")
    moved, count = re.subn(r'(\(ref "D1"\)\s*\(pin )"2"', r'\1"9"', text)
    assert count == 1
    netlist.write_bytes(moved.encode("utf-8"))
    assert module.rescore(copy) == 1
    assert "DIFFERS  led_indicator: fail" in capsys.readouterr().out
    # A refusal that wrote a file is not a refusal.
    shutil.rmtree(copy)
    shutil.copytree(RUNS[-1], copy)
    record_file = copy / "ldo_overloaded" / "record.json"
    record = json.loads(record_file.read_text(encoding="utf-8"))
    record["files"] = ["ldo_overloaded.kicad_sch"]
    record_file.write_text(json.dumps(record), encoding="utf-8")
    assert module.rescore(copy) == 1


def test_a_verdict_names_each_check() -> None:
    module = harness()
    expected = {"outcome": "refusal", "refused_for": "max_junction_temp"}
    good = {
        "outcome": "refusal",
        "failed_requirements": ["max_junction_temp"],
        "files": [],
    }
    assert module.verdict(expected, good, None) == {
        "passed": True,
        "checks": {
            "outcome is as expected": True,
            "refused for the expected requirement": True,
            "nothing was written": True,
        },
    }
    wrong = dict(good, failed_requirements=["rail_voltage"])
    result = module.verdict(expected, wrong, None)
    assert result["passed"] is False
    assert result["checks"]["refused for the expected requirement"] is False
