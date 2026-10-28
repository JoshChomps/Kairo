"""A worst-case search whose run fails says why, in the simulator's words."""

from __future__ import annotations

from pathlib import Path

import pytest
from minilib import make_index

import kairo.spice.sweep as sweep
from kairo.ir import Design
from kairo.library.grounding import ground
from kairo.spice.engines import ACCURATE, Profile, Run, Simulator
from kairo.spice.request import SimRequest

KILLED = "killed after 120 s without finishing"


def test_a_failed_run_keeps_its_reason_for_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = make_index(tmp_path)
    grounded = ground(
        Design.model_validate(
            {
                "design": "rc",
                "nets": {
                    "VIN": {"role": "power", "voltage": "5V"},
                    "GND": {"role": "ground"},
                },
                "components": {
                    "C1": {"lib_id": "Device:C", "value": "10uF"},
                    "R1": {"lib_id": "Device:R", "value": "100"},
                },
                "connections": {"VIN": ["C1.1", "R1.1"], "GND": ["C1.2", "R1.2"]},
            }
        ),
        index,
    )
    simulator = Simulator("ngspice", tmp_path / "ngspice", "47")

    def killed(simulator: Simulator, text: str, profile: Profile, **_: object) -> Run:
        return Run(simulator, profile, None, KILLED, (), 120.0)

    # No simulator runs: the run is a stand-in that was killed.
    monkeypatch.setattr(sweep, "run_netlist", killed)
    failures: list[str] = []
    one = sweep.evaluator(
        grounded,
        SimRequest.model_validate({"analysis": {"kind": "op"}}),
        simulator,
        profile=ACCURATE,
        failures=failures,
    )
    assert one({}) is None
    assert failures == [KILLED]
    assert sweep._why(failures) == f" ({KILLED})"  # pyright: ignore[reportPrivateUsage]
    assert sweep._why([]) == ""  # pyright: ignore[reportPrivateUsage]
