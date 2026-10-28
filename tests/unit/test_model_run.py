from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "evals" / "model_run.py"


def harness() -> Any:
    spec = importlib.util.spec_from_file_location("model_run", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


same_circuit = harness().same_circuit
verdict = harness().verdict

PARTS = {"R1": "Device:R", "C1": "Device:C", "J1": "Connector:Conn_01x02_Pin"}
NETS = {
    "/IN": ["J1.1", "R1.1"],
    "/OUT": ["R1.2", "C1.1"],
    "GND": ["J1.2", "C1.2"],
}


def netlist(parts: dict[str, str], nets: dict[str, list[str]]) -> str:
    """A netlist laid out as kicad-cli 10 writes one, cut down to what is read."""
    comps = " ".join(
        f'(comp (ref "{ref}") (value "x") (libsource (lib "{lib.split(":")[0]}") '
        f'(part "{lib.split(":")[1]}") (description "")) (sheetpath (names "/")))'
        for ref, lib in parts.items()
    )
    listed = " ".join(
        f'(net (code "{i}") (name "{name}") (class "Default") '
        + " ".join(
            f'(node (ref "{pin.rsplit(".", 1)[0]}") (pin "{pin.rsplit(".", 1)[1]}") '
            '(pintype "passive"))'
            for pin in pins
        )
        + ")"
        for i, (name, pins) in enumerate(nets.items())
    )
    return (
        '(export (version "E") (design (tool "test") (sheet (number "1") '
        f'(name "/"))) (components {comps}) (nets {listed}))'
    )


def test_the_same_circuit_under_other_names_is_the_same() -> None:
    renamed_parts = {"R7": "Device:R", "C9": "Device:C", "P1": PARTS["J1"]}
    renamed = {
        "SIG": ["P1.1", "R7.1"],
        "FILTERED": ["R7.2", "C9.1"],
        "0V": ["P1.2", "C9.2"],
    }
    assert same_circuit(PARTS, NETS, netlist(renamed_parts, renamed))


def test_a_different_circuit_is_not_the_same() -> None:
    # The capacitor moved to the input side of the resistor.
    moved = {**NETS, "/IN": ["J1.1", "R1.1", "C1.1"], "/OUT": ["R1.2"]}
    assert not same_circuit(PARTS, NETS, netlist(PARTS, moved))
    # The connector wired the other way round: pin numbers matter.
    swapped = {**NETS, "/IN": ["J1.2", "R1.1"], "GND": ["J1.1", "C1.2"]}
    assert not same_circuit(PARTS, NETS, netlist(PARTS, swapped))
    # Another symbol in the capacitor's place.
    other = {**PARTS, "C1": "Device:R"}
    assert not same_circuit(PARTS, NETS, netlist(other, NETS))


def saved_run(folder: Path, expected: dict[str, Any], calls: list[Any]) -> Path:
    folder.mkdir()
    (folder / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    (folder / "reference.json").write_text(
        json.dumps({"components": {r: {"lib_id": s} for r, s in PARTS.items()}}),
        encoding="utf-8",
    )
    (folder / "transcript.json").write_text(json.dumps(calls), encoding="utf-8")
    return folder


def test_a_verdict_is_worked_out_from_saved_files_alone(tmp_path: Path) -> None:
    def write(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool": "apply_design",
            "arguments": {},
            "error": False,
            "result": result,
        }

    design = {"outcome": "design", "netlist": NETS}
    built = saved_run(tmp_path / "a", design, [write({"written": True})])
    (built / "netlist.net").write_text(netlist(PARTS, NETS), encoding="utf-8")
    assert verdict(built)["passed"]
    # A write that was not accepted, or no netlist, is a miss.
    assert not verdict(saved_run(tmp_path / "b", design, [write({"written": False})]))[
        "passed"
    ]
    assert not verdict(saved_run(tmp_path / "c", design, []))["passed"]

    refusal = {"outcome": "refusal", "refused_for": "min_attenuation"}
    refused = write(
        {
            "written": False,
            "code": "E_REQUIREMENT_UNSATISFIABLE",
            "failed_requirements": [{"type": "min_attenuation"}],
        }
    )
    assert verdict(saved_run(tmp_path / "d", refusal, [refused]))["passed"]
    # Loosening the requirement until something is written is a miss.
    loosened = saved_run(tmp_path / "e", refusal, [refused, write({"written": True})])
    assert not verdict(loosened)["passed"]


def test_ollamas_own_reason_for_a_failure_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 27b model that ran out of memory while loading was reported as
    # "HTTP Error 500" alone; Ollama's body said why.
    module = harness()
    said = b'{"error":"llama-server process has terminated: GGML_ASSERT"}'

    def refuse(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 500, "Internal Server Error", Message(), io.BytesIO(said)
        )

    monkeypatch.setattr(module.urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError, match=r"500.*GGML_ASSERT"):
        module._chat("some-model", [], [])  # pyright: ignore[reportPrivateUsage]
